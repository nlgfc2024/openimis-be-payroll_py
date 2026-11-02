from types import SimpleNamespace
from datetime import date
from decimal import Decimal
from io import BytesIO
from unittest.mock import MagicMock, patch

import pandas as pd

from django.test import SimpleTestCase, TestCase
from django.core.exceptions import ValidationError

from tasks_management.models import Task
from payroll.apps import PayrollConfig
from payroll import signals
from payroll.models import (
    BenefitConsumption,
    BenefitConsumptionStatus,
    ConsolidatedPayrollMember,
    CsvReconciliationUpload,
    Payroll,
    PayrollBenefitConsumption,
    PayrollStatus,
    ReconciliationUploadRow,
)
from payroll.services import CsvReconciliationService
from core.test_helpers import LogInHelper
from individual.models import Individual
from individual.tests.data import service_add_individual_payload


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

    def test_consolidated_export_uses_source_payroll_micro_catchment(self):
        source_payroll = SimpleNamespace(id="source-id", name="Source payroll")
        consolidated_payroll = SimpleNamespace(
            id="consolidated-id",
            payroll_type=Payroll.PayrollType.CONSOLIDATED,
        )
        benefit = SimpleNamespace(id="benefit-id", code="CODE-1", json_ext={})
        record = {
            key: "value" for key in PayrollConfig.csv_reconciliation_field_mapping
        }
        record["code"] = benefit.code
        benefits = MagicMock()
        benefits.values.return_value = [record]
        benefits.select_related.return_value = [benefit]
        service = CsvReconciliationService(None)

        with (
            patch.object(service, "_resolve_payroll", return_value=consolidated_payroll),
            patch.object(service, "_get_benefit_consumption_qs", return_value=benefits),
            patch.object(
                service, "_get_source_payrolls_by_benefit",
                return_value={benefit.id: source_payroll},
            ),
            patch.object(
                service, "_get_payroll_micro_catchment", return_value="Source catchment",
            ),
            patch.object(service, "_get_micro_catchment", return_value="Recipient catchment"),
        ):
            exported = pd.read_csv(BytesIO(
                service.download_reconciliation("consolidated-id").getvalue()
            ))

        self.assertEqual(exported.loc[0, "Micro Catchment"], "Source catchment")

    def test_consolidated_upload_context_uses_source_payroll_micro_catchment(self):
        source_payroll = SimpleNamespace(id="source-id")
        consolidated_payroll = SimpleNamespace(
            id="consolidated-id",
            payroll_type=Payroll.PayrollType.CONSOLIDATED,
        )
        benefit = SimpleNamespace(
            id="benefit-id",
            code="CODE-1",
            individual=SimpleNamespace(location_id="location-id", location=None),
        )
        benefit_query = MagicMock()
        benefit_query.select_related.return_value.distinct.return_value = [benefit]
        service = CsvReconciliationService(None)

        with (
            patch("payroll.services.BenefitConsumption.objects.filter", return_value=benefit_query),
            patch.object(
                service, "_get_source_payrolls_by_benefit",
                return_value={benefit.id: source_payroll},
            ),
            patch.object(
                service, "_get_payroll_micro_catchment", return_value="Source catchment",
            ),
        ):
            benefits_by_code, contexts = service._load_upload_context(
                consolidated_payroll, pd.Series(["CODE-1"])
            )

        self.assertIs(benefits_by_code["CODE-1"], benefit)
        self.assertEqual(contexts[benefit.id]["micro_catchment"], "Source catchment")

    def test_upload_with_unmatched_rows_cannot_be_applied(self):
        upload = SimpleNamespace(
            id="upload-id",
            status=CsvReconciliationUpload.Status.WAITING_FOR_VERIFICATION,
            unmatched_records=1,
        )
        with patch("payroll.services.CsvReconciliationUpload.objects.select_for_update") as lock_upload:
            lock_upload.return_value.get.return_value = upload
            with self.assertRaises(ValidationError):
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
            patch("payroll.services.ensure_payroll_is_not_consolidated_source"),
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

    def test_completed_offline_reconciliation_rechecks_source_membership(self):
        user = SimpleNamespace(id="user-id")
        payroll = SimpleNamespace(payment_method="offline")
        with patch("payroll.signals.bind_service_signal") as bind_service_signal:
            signals.bind_service_signals()
        handler = next(
            call.args[1]
            for call in bind_service_signal.call_args_list
            if call.args[1].__name__ == "on_task_complete_payroll_reconcilation"
        )

        with (
            patch("payroll.signals.User.objects.get", return_value=user),
            patch("payroll.signals.Payroll.objects.get", return_value=payroll),
            patch(
                "payroll.signals.ensure_payroll_is_not_consolidated_source",
                side_effect=ValidationError("Payroll belongs to an active consolidation"),
            ) as ensure_not_consolidated,
            patch("payroll.signals.PaymentMethodStorage.get_chosen_payment_method") as get_strategy,
        ):
            handler(result={
                "success": True,
                "data": {
                    "user": {"id": user.id},
                    "task": {
                        "business_event": PayrollConfig.payroll_reconciliation_event,
                        "status": Task.Status.COMPLETED,
                        "entity_id": "payroll-id",
                        "data": {},
                    },
                },
            })

        ensure_not_consolidated.assert_called_once_with(payroll, "reconcile")
        get_strategy.assert_not_called()


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

    def test_completed_source_reconciles_before_consolidated_payroll(self):
        individual = Individual(**service_add_individual_payload)
        individual.save(username=self.user.login_name)
        source_a = Payroll(name="Completed source", status=PayrollStatus.APPROVE_FOR_PAYMENT)
        source_a.save(username=self.user.login_name)
        source_b = Payroll(name="Pending source", status=PayrollStatus.APPROVE_FOR_PAYMENT)
        source_b.save(username=self.user.login_name)
        consolidated_payroll = Payroll(
            name="Consolidated reconciliation test",
            status=PayrollStatus.APPROVE_FOR_PAYMENT,
            payroll_type=Payroll.PayrollType.CONSOLIDATED,
        )
        consolidated_payroll.save(username=self.user.login_name)
        ConsolidatedPayrollMember(
            consolidated_payroll=consolidated_payroll,
            source_payroll=source_a,
        ).save(username=self.user.login_name)
        ConsolidatedPayrollMember(
            consolidated_payroll=consolidated_payroll,
            source_payroll=source_b,
        ).save(username=self.user.login_name)
        benefit_a = BenefitConsumption(
            individual=individual,
            code="completed-source-code",
            amount=Decimal("100"),
            type="Cash Transfer",
            status=BenefitConsumptionStatus.ACCEPTED,
        )
        benefit_a.save(username=self.user.login_name)
        benefit_b = BenefitConsumption(
            individual=individual,
            code="pending-source-code",
            amount=Decimal("100"),
            type="Cash Transfer",
            status=BenefitConsumptionStatus.ACCEPTED,
        )
        benefit_b.save(username=self.user.login_name)
        for payroll, benefit in (
            (source_a, benefit_a),
            (source_b, benefit_b),
            (consolidated_payroll, benefit_a),
            (consolidated_payroll, benefit_b),
        ):
            PayrollBenefitConsumption(payroll=payroll, benefit=benefit).save(
                username=self.user.login_name,
            )
        upload = CsvReconciliationUpload(
            payroll=consolidated_payroll,
            status=CsvReconciliationUpload.Status.WAITING_FOR_VERIFICATION,
            total_records=1,
            matched_records=1,
            paid_records=1,
        )
        upload.save(username=self.user.login_name)
        ReconciliationUploadRow(
            upload=upload,
            row_number=2,
            benefit=benefit_a,
            code=benefit_a.code,
            submitted_data={"Paid": "Yes", "receipt": "source-a-receipt"},
            status=ReconciliationUploadRow.Status.VALID,
        ).save(username=self.user.login_name)

        CsvReconciliationService(self.user).apply_upload(upload)

        source_a.refresh_from_db()
        source_b.refresh_from_db()
        consolidated_payroll.refresh_from_db()
        self.assertEqual(source_a.status, PayrollStatus.RECONCILED)
        self.assertEqual(source_b.status, PayrollStatus.APPROVE_FOR_PAYMENT)
        self.assertEqual(consolidated_payroll.status, PayrollStatus.APPROVE_FOR_PAYMENT)

    def test_invalid_upload_batch_does_not_apply_its_valid_rows(self):
        individual = Individual(**service_add_individual_payload)
        individual.save(username=self.user.login_name)
        payroll = Payroll(
            name="All-or-nothing reconciliation test",
            status=PayrollStatus.APPROVE_FOR_PAYMENT,
        )
        payroll.save(username=self.user.login_name)
        benefit = BenefitConsumption(
            individual=individual,
            code="all-or-nothing-valid-code",
            amount=Decimal("100"),
            type="Cash Transfer",
            status=BenefitConsumptionStatus.ACCEPTED,
        )
        benefit.save(username=self.user.login_name)
        PayrollBenefitConsumption(payroll=payroll, benefit=benefit).save(
            username=self.user.login_name,
        )
        upload = CsvReconciliationUpload(
            payroll=payroll,
            status=CsvReconciliationUpload.Status.WAITING_FOR_VERIFICATION,
            total_records=2,
            matched_records=1,
            paid_records=1,
            unmatched_records=1,
        )
        upload.save(username=self.user.login_name)
        ReconciliationUploadRow(
            upload=upload,
            row_number=2,
            benefit=benefit,
            code=benefit.code,
            submitted_data={"Paid": "Yes", "receipt": "valid-row-receipt"},
            status=ReconciliationUploadRow.Status.VALID,
        ).save(username=self.user.login_name)
        ReconciliationUploadRow(
            upload=upload,
            row_number=3,
            code="invalid-row-code",
            mismatch_reasons=["national_id_mismatch"],
            submitted_data={"Paid": "Yes", "receipt": "invalid-row-receipt"},
            status=ReconciliationUploadRow.Status.INVALID,
        ).save(username=self.user.login_name)

        with self.assertRaises(ValidationError):
            CsvReconciliationService(self.user).apply_upload(upload)

        benefit.refresh_from_db()
        payroll.refresh_from_db()
        upload.refresh_from_db()
        self.assertEqual(benefit.status, BenefitConsumptionStatus.ACCEPTED)
        self.assertIsNone(benefit.receipt)
        self.assertEqual(payroll.status, PayrollStatus.APPROVE_FOR_PAYMENT)
        self.assertEqual(upload.status, CsvReconciliationUpload.Status.WAITING_FOR_VERIFICATION)
