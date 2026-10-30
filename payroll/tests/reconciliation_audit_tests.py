from types import SimpleNamespace
from datetime import date
from decimal import Decimal
from io import BytesIO
from unittest.mock import MagicMock, patch

import pandas as pd

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

    def test_uploaded_national_id_must_match_beneficiary(self):
        payroll = SimpleNamespace(name="Payroll", status=PayrollStatus.APPROVE_FOR_PAYMENT)
        individual = SimpleNamespace(
            first_name="Ada",
            last_name="Lovelace",
            dob=date(1815, 12, 10),
            json_ext={"national_id": "ID-123"},
        )
        benefit = SimpleNamespace(
            id="benefit-id",
            individual=individual,
            status="ACCEPTED",
            amount=Decimal("100"),
            type="CASH",
        )
        row = pd.Series({
            "payrollbenefitconsumption__payroll__name": "Payroll",
            "payrollbenefitconsumption__payroll__status": PayrollStatus.APPROVE_FOR_PAYMENT,
            "individual__first_name": "Ada",
            "individual__last_name": "Lovelace",
            "individual__json_ext__national_id": " ID-999 ",
            "individual__dob": "1815-12-10",
            "status": "ACCEPTED",
            "amount": "100",
            "type": "CASH",
            "Paid": "Yes",
            "receipt": "receipt-1",
        })

        _, reasons = CsvReconciliationService(None)._validate_upload_row(
            payroll, row, "CODE-1", "ID-999", set(), set(),
            {"CODE-1": benefit}, {benefit.id: {}},
        )

        self.assertIn("national_id_mismatch", reasons)
        self.assertTrue(CsvReconciliationService._has_blocking_reason(reasons))

    def test_uploaded_national_id_matching_ignores_surrounding_whitespace(self):
        self.assertTrue(CsvReconciliationService._matches_text(" ID-123 ", "ID-123"))

    def test_upload_with_unmatched_rows_cannot_be_applied(self):
        upload = SimpleNamespace(
            id="upload-id",
            status=CsvReconciliationUpload.Status.WAITING_FOR_VERIFICATION,
            unmatched_records=1,
        )
        with patch("payroll.services.CsvReconciliationUpload.objects.select_for_update") as lock_upload:
            lock_upload.return_value.get.return_value = upload
            CsvReconciliationService(None).apply_upload(upload)

        self.assertEqual(upload.status, CsvReconciliationUpload.Status.WAITING_FOR_VERIFICATION)

    def test_upload_with_mismatches_fails_without_creating_review_task(self):
        payroll = SimpleNamespace(
            id="payroll-id",
            payroll_type=Payroll.PayrollType.STANDARD,
        )
        upload = SimpleNamespace(
            id="upload-id",
            file_name="payments.csv",
            save=MagicMock(),
        )
        headers = list(PayrollConfig.csv_reconciliation_field_mapping.values()) + ["Paid"]
        contents = ",".join(headers) + "\n" + ",".join(["value"] * len(headers))
        file = BytesIO(contents.encode())
        user = SimpleNamespace(login_name="reconciliation-test-user")

        with (
            patch("payroll.services.Payroll.objects.filter") as find_payroll,
            patch("payroll.services.Payroll.objects.select_for_update") as lock_payroll,
            patch("payroll.services.CsvReconciliationUpload.objects.filter") as find_uploads,
            patch("payroll.services.ReconciliationUploadRow") as upload_row_model,
            patch("payroll.services.bulk_create_with_history"),
            patch("payroll.services.TaskService") as task_service,
        ):
            find_payroll.return_value.first.return_value = payroll
            lock_payroll.return_value.get.return_value = payroll
            find_uploads.return_value.exclude.return_value.exclude.return_value \
                .order_by.return_value.first.return_value = None
            service = CsvReconciliationService(user)
            with (
                patch.object(service, "_load_upload_context", return_value=({}, {})),
                patch.object(
                    service, "_validate_upload_row",
                    return_value=(None, ["national_id_mismatch"]),
                ),
            ):
                _, errors, summary = service.upload_reconciliation("payroll-id", file, upload)

        self.assertEqual(upload.status, CsvReconciliationUpload.Status.FAIL)
        self.assertEqual(errors, {"2": ["national_id_mismatch"]})
        self.assertEqual(summary["unmatched_records"], 1)
        task_service.assert_not_called()
        upload_row_model.assert_called_once()

    def test_consolidated_task_district_falls_back_to_source_beneficiary_locations(self):
        source_payrolls = [
            SimpleNamespace(id="source-1", name="Payroll One", json_ext={}),
            SimpleNamespace(id="source-2", name="Payroll Two", json_ext={}),
        ]
        members = [
            SimpleNamespace(source_payroll_id=payroll.id, source_payroll=payroll)
            for payroll in source_payrolls
        ]
        consolidated_payroll = SimpleNamespace(
            id="consolidated-id",
            name="Consolidated",
            payroll_type=Payroll.PayrollType.CONSOLIDATED,
            payment_plan=None,
        )

        def beneficiary_link(payroll_id, district_name):
            district = SimpleNamespace(
                id=f"district-{district_name}",
                type="R",
                name=district_name,
                parent=None,
            )
            village = SimpleNamespace(
                id=f"village-{district_name}",
                type="V",
                name="Village",
                parent=district,
            )
            return SimpleNamespace(
                payroll_id=payroll_id,
                benefit=SimpleNamespace(
                    individual=SimpleNamespace(location=village),
                ),
            )

        memberships = [
            beneficiary_link("source-1", "Karonga"),
            beneficiary_link("source-2", "Mzimba"),
            beneficiary_link("source-2", "Karonga"),
        ]
        member_query = MagicMock()
        member_query.select_related.return_value.order_by.return_value = members
        benefits_query = MagicMock()
        benefits_query.select_related.return_value = memberships

        with (
            patch(
                "payroll.services.ConsolidatedPayrollMember.objects.filter",
                return_value=member_query,
            ),
            patch(
                "payroll.services.apps.get_model",
                return_value=SimpleNamespace(objects=SimpleNamespace()),
            ),
            patch(
                "payroll.services.PayrollBenefitConsumption.objects.filter",
                return_value=benefits_query,
            ),
        ):
            context = CsvReconciliationService._reconciliation_task_context(consolidated_payroll)

        self.assertEqual(context["district"], "Karonga, Mzimba")
        self.assertEqual(
            [item["name"] for item in context["included_payrolls"]],
            ["Payroll One", "Payroll Two"],
        )

    def test_task_failure_handler_has_task_service_available(self):
        # The reconciliation completion signal uses this service to change a
        # checker task to FAILED when applying its upload raises an error.
        self.assertIsNotNone(signals.TaskService)


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
