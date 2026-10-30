from io import BytesIO
from types import SimpleNamespace

import pandas as pd
from openpyxl import load_workbook

from django.test import SimpleTestCase, TestCase

from payroll.apps import PayrollConfig
from payroll import signals
from payroll.models import (
    CsvReconciliationUpload,
    Payroll,
    PayrollStatus,
    ReconciliationUploadRow,
)
from payroll.services import CsvReconciliationService
from core.test_helpers import LogInHelper


class ReconciliationAuditPolicyTests(SimpleTestCase):
    """Fast unit coverage for the configurable audit policy and API summary."""

    def setUp(self):
        self.original_blocking = PayrollConfig.csv_reconciliation_blocking_mismatch_reasons
        self.original_warnings = PayrollConfig.csv_reconciliation_warning_mismatch_reasons
        self.original_tolerance = PayrollConfig.csv_reconciliation_amount_tolerance

    def tearDown(self):
        PayrollConfig.csv_reconciliation_blocking_mismatch_reasons = self.original_blocking
        PayrollConfig.csv_reconciliation_warning_mismatch_reasons = self.original_warnings
        PayrollConfig.csv_reconciliation_amount_tolerance = self.original_tolerance

    def test_default_amount_comparison_is_exact(self):
        PayrollConfig.csv_reconciliation_amount_tolerance = "0"
        self.assertTrue(CsvReconciliationService._amounts_match("10.00", "10"))
        self.assertFalse(CsvReconciliationService._amounts_match("10.01", "10"))

    def test_warning_reason_keeps_row_applicable(self):
        PayrollConfig.csv_reconciliation_blocking_mismatch_reasons = ["missing_code"]
        PayrollConfig.csv_reconciliation_warning_mismatch_reasons = ["amount_mismatch"]
        self.assertFalse(CsvReconciliationService._has_blocking_reason(["amount_mismatch"]))
        self.assertTrue(CsvReconciliationService._has_blocking_reason(["missing_code"]))
        self.assertTrue(CsvReconciliationService._has_blocking_reason(["new_unconfigured_reason"]))

    def test_only_data_mismatches_can_be_configured_as_warnings(self):
        original_warnings = PayrollConfig.csv_reconciliation_warning_mismatch_reasons
        original_blocking = PayrollConfig.csv_reconciliation_blocking_mismatch_reasons
        try:
            PayrollConfig.csv_reconciliation_warning_mismatch_reasons = ["code_not_in_payroll"]
            PayrollConfig.csv_reconciliation_blocking_mismatch_reasons = []
            with self.assertRaises(ValueError):
                PayrollConfig._PayrollConfig__validate_reconciliation_config()
        finally:
            PayrollConfig.csv_reconciliation_warning_mismatch_reasons = original_warnings
            PayrollConfig.csv_reconciliation_blocking_mismatch_reasons = original_blocking

    def test_upload_summary_uses_audited_record_keys(self):
        upload = SimpleNamespace(
            id="upload-id",
            status="WAITING_FOR_VERIFICATION",
            total_records=7,
            matched_records=5,
            paid_records=3,
            unpaid_records=2,
            unmatched_records=2,
        )
        self.assertEqual(CsvReconciliationService._upload_summary(upload), {
            "upload_id": "upload-id",
            "status": "WAITING_FOR_VERIFICATION",
            "total_records": 7,
            "matched_records": 5,
            "paid_records": 3,
            "unpaid_records": 2,
            "unmatched_records": 2,
        })

    def test_task_failure_handler_has_task_service_available(self):
        # The reconciliation completion signal uses this service to change a
        # checker task to FAILED when applying its upload raises an error.
        self.assertIsNotNone(signals.TaskService)

    def test_reconciliation_template_locks_all_but_paid_and_receipt(self):
        dataframe = pd.DataFrame([{
            "Payroll Name": "PAYROLL-001",
            "First Name": "Jane",
            "Receipt": "",
            "Paid": "",
        }])

        content = CsvReconciliationService._protected_reconciliation_workbook(dataframe).getvalue()
        worksheet = load_workbook(BytesIO(content)).active

        self.assertTrue(worksheet.protection.sheet)
        self.assertTrue(worksheet["A2"].protection.locked)
        self.assertFalse(worksheet["C2"].protection.locked)
        self.assertFalse(worksheet["D2"].protection.locked)
        self.assertTrue(worksheet["A1"].protection.locked)
        validation = worksheet.data_validations.dataValidation[0]
        self.assertEqual(validation.type, "list")
        self.assertEqual(validation.formula1, '"Yes,No"')
        self.assertEqual(str(validation.sqref), "D2")

    def test_changed_locked_value_is_rejected(self):
        individual = SimpleNamespace(
            first_name="Jane",
            last_name="Doe",
            dob=None,
            json_ext={},
            location=None,
        )
        benefit = SimpleNamespace(
            id="benefit-001",
            individual=individual,
            status="APPROVE_FOR_PAYMENT",
            amount="100",
            type="Cash Transfer",
            json_ext={},
        )
        payroll = SimpleNamespace(name="PAYROLL-001", status="APPROVE_FOR_PAYMENT")
        row = pd.Series({
            "code": "benefit-001",
            "individual__first_name": "Changed name",
            "receipt": "PSP-001",
            "Paid": "Yes",
        })

        _, reasons = CsvReconciliationService(None)._validate_upload_row(
            payroll,
            row,
            "benefit-001",
            None,
            set(),
            set(),
            {"benefit-001": benefit},
            {"benefit-001": {"micro_catchment": None}},
        )

        self.assertIn("first_name_mismatch", reasons)

    def test_missing_protected_extra_info_column_is_rejected(self):
        individual = SimpleNamespace(
            first_name="Jane",
            last_name="Doe",
            dob=None,
            json_ext={},
            location=None,
        )
        benefit = SimpleNamespace(
            id="benefit-001",
            individual=individual,
            status="APPROVE_FOR_PAYMENT",
            amount="100",
            type="Cash Transfer",
            json_ext={"extra_info": {"Programme": "Cash Plus"}},
        )
        payroll = SimpleNamespace(name="PAYROLL-001", status="APPROVE_FOR_PAYMENT")
        row = pd.Series({
            "code": "benefit-001",
            "receipt": "PSP-001",
            "Paid": "Yes",
            "Micro Catchment": None,
        })

        _, reasons = CsvReconciliationService(None)._validate_upload_row(
            payroll,
            row,
            "benefit-001",
            None,
            set(),
            set(),
            {"benefit-001": benefit},
            {"benefit-001": {"micro_catchment": None}},
        )

        self.assertIn("extra_info_mismatch:Programme", reasons)


class ReconciliationPayrollStatusTests(TestCase):
    def setUp(self):
        self.user = LogInHelper().get_or_create_user_api(username='recon-status-user')

    def test_approved_upload_with_only_unpaid_rows_does_not_reconcile_payroll(self):
        payroll = Payroll(name='Reconciliation status test', status=PayrollStatus.APPROVE_FOR_PAYMENT)
        payroll.save(username=self.user.login_name)
        upload = CsvReconciliationUpload(
            payroll=payroll,
            status=CsvReconciliationUpload.Status.WAITING_FOR_VERIFICATION,
            total_records=1,
            matched_records=1,
            unpaid_records=1,
        )
        upload.save(username=self.user.login_name)
        row = ReconciliationUploadRow(
            upload=upload,
            row_number=2,
            code='unpaid-row',
            submitted_data={'Paid': 'No'},
            status=ReconciliationUploadRow.Status.VALID,
        )
        row.save(username=self.user.login_name)

        CsvReconciliationService(self.user).apply_upload(upload)

        payroll.refresh_from_db()
        upload.refresh_from_db()
        self.assertEqual(upload.status, CsvReconciliationUpload.Status.SUCCESS)
        self.assertEqual(payroll.status, PayrollStatus.APPROVE_FOR_PAYMENT)
