from types import SimpleNamespace

from django.test import SimpleTestCase

from payroll.apps import PayrollConfig
from payroll import signals
from payroll.services import CsvReconciliationService


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
