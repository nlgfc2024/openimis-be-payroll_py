from types import SimpleNamespace
from datetime import date
from decimal import Decimal
from io import BytesIO
from unittest.mock import MagicMock, patch

import pandas as pd

from django.test import TestCase
from django.core.exceptions import PermissionDenied, ValidationError

from core.service_signals import ServiceSignalBindType
from tasks_management.models import Task
from payroll.apps import DEFAULT_CONFIG, KYC_PAYMENT_DETAILS_FIELD_MAPPING, PayrollConfig
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
from payroll.services import CsvReconciliationService, PayrollService
from payroll.strategies.strategy_of_payments_interface import StrategyOfPaymentInterface
from payroll.gql_queries import BenefitConsumptionGQLType
from core.test_helpers import LogInHelper
from individual.models import Individual
from individual.tests.data import service_add_individual_payload


class ReconciliationAuditPolicyTests(TestCase):
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

    def test_national_id_mismatch_only_blocks_consolidated_payrolls(self):
        self.assertFalse(CsvReconciliationService._has_blocking_reason(
            ["national_id_mismatch"],
            Payroll.PayrollType.STANDARD,
        ))
        self.assertTrue(CsvReconciliationService._has_blocking_reason(
            ["national_id_mismatch"],
            Payroll.PayrollType.CONSOLIDATED,
        ))

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

    def test_reconciliation_requires_payment_approval(self):
        service = CsvReconciliationService(None)

        with self.assertRaisesRegex(ValidationError, "approved for payment"):
            service._ensure_payroll_ready_for_reconciliation(SimpleNamespace(
                status=PayrollStatus.PENDING_APPROVAL,
            ))

        service._ensure_payroll_ready_for_reconciliation(SimpleNamespace(
            status=PayrollStatus.APPROVE_FOR_PAYMENT,
        ))
        # Consolidated payrolls follow the same approval lifecycle as standard
        # payrolls, so their approved status must also be accepted.
        service._ensure_payroll_ready_for_reconciliation(SimpleNamespace(
            status=PayrollStatus.APPROVE_FOR_PAYMENT,
            payroll_type=Payroll.PayrollType.CONSOLIDATED,
        ))

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
        self.assertTrue(CsvReconciliationService._has_blocking_reason(
            reasons,
            Payroll.PayrollType.CONSOLIDATED,
        ))

    def test_uploaded_national_id_matching_ignores_surrounding_whitespace(self):
        self.assertTrue(CsvReconciliationService._matches_text(" ID-123 ", "ID-123"))

    def test_kyc_export_preserves_configured_headers_without_duplicates(self):
        original_mapping = PayrollConfig.csv_reconciliation_field_mapping
        try:
            PayrollConfig.csv_reconciliation_field_mapping = {
                "individual__json_ext__sex": "Gender",
                "code": "Code",
            }

            mapping = CsvReconciliationService._reconciliation_field_mapping()
            export_mapping = CsvReconciliationService._reconciliation_export_field_mapping()
            columns = CsvReconciliationService._reconciliation_export_columns()

            self.assertEqual(mapping["individual__json_ext__sex"], "Gender")
            self.assertEqual(list(mapping.values()).count("Gender"), 1)
            self.assertEqual(export_mapping["individual__json_ext__sex"], "Gender")
            self.assertEqual(columns.count("Gender"), 1)
        finally:
            PayrollConfig.csv_reconciliation_field_mapping = original_mapping

    def test_code_mapping_is_retained_when_omitted_from_configuration(self):
        original_mapping = PayrollConfig.csv_reconciliation_field_mapping
        try:
            PayrollConfig.csv_reconciliation_field_mapping = {"amount": "Amount"}

            mapping = CsvReconciliationService._reconciliation_field_mapping()

            self.assertEqual(mapping["code"], "Code")
        finally:
            PayrollConfig.csv_reconciliation_field_mapping = original_mapping

    def test_configured_type_header_is_preserved(self):
        original_mapping = PayrollConfig.csv_reconciliation_field_mapping
        try:
            PayrollConfig.csv_reconciliation_field_mapping = {
                "code": "Code",
                "type": "Payment type",
            }

            mapping = CsvReconciliationService._reconciliation_field_mapping()

            self.assertEqual(mapping["type"], "Payment type")
        finally:
            PayrollConfig.csv_reconciliation_field_mapping = original_mapping

    def test_existing_kyc_mapping_reads_the_benefit_snapshot(self):
        original_mapping = PayrollConfig.csv_reconciliation_field_mapping
        try:
            PayrollConfig.csv_reconciliation_field_mapping = {
                "individual__json_ext__account_number": "Account Number",
                "code": "Code",
            }

            mapping = CsvReconciliationService._reconciliation_field_mapping()

            self.assertEqual(
                mapping["json_ext__kyc_payment_details__account_number"],
                "Account Number",
            )
            self.assertNotIn("individual__json_ext__account_number", mapping)
        finally:
            PayrollConfig.csv_reconciliation_field_mapping = original_mapping

    def test_legacy_upload_does_not_require_kyc_columns_with_default_config(self):
        original_mapping = PayrollConfig.csv_reconciliation_field_mapping
        try:
            default_mapping = DEFAULT_CONFIG["csv_reconciliation_field_mapping"]
            PayrollConfig.csv_reconciliation_field_mapping = default_mapping
            dataframe = pd.DataFrame([{
                **{
                    header: "value"
                    for field, header in default_mapping.items()
                    if field not in KYC_PAYMENT_DETAILS_FIELD_MAPPING
                },
                CsvReconciliationService._micro_catchment_column(): "Catchment A",
                PayrollConfig.csv_reconciliation_paid_extra_field: "No",
            }])

            CsvReconciliationService(None)._validate_dataframe(dataframe)
        finally:
            PayrollConfig.csv_reconciliation_field_mapping = original_mapping

    def test_receipt_is_optional_even_when_legacy_mapping_contains_it(self):
        original_mapping = PayrollConfig.csv_reconciliation_field_mapping
        try:
            PayrollConfig.csv_reconciliation_field_mapping = {
                "code": "Code",
                "amount": "Amount",
                "status": "Status",
                "receipt": "Receipt",
            }
            mapping = CsvReconciliationService._reconciliation_field_mapping()
            dataframe = pd.DataFrame([{
                header: "value"
                for field, header in mapping.items()
                if field != "receipt"
            } | {
                "Code": "BENEFIT-1",
                "Amount": "10.00",
                "Status": BenefitConsumptionStatus.ACCEPTED,
                CsvReconciliationService._micro_catchment_column(): "Catchment A",
                "Paid": "Yes",
            }])

            CsvReconciliationService(None)._validate_dataframe(dataframe)
        finally:
            PayrollConfig.csv_reconciliation_field_mapping = original_mapping

    def test_reconciliation_export_hides_receipt_but_keeps_code(self):
        original_mapping = PayrollConfig.csv_reconciliation_field_mapping
        PayrollConfig.csv_reconciliation_field_mapping = {
            "code": "Code",
            "amount": "Amount",
            "receipt": "Receipt",
            "type": "Payment type",
        }
        payroll = SimpleNamespace(
            id="standard-id",
            name="Standard payroll",
            status=PayrollStatus.APPROVE_FOR_PAYMENT,
            payroll_type=Payroll.PayrollType.STANDARD,
        )
        benefit = SimpleNamespace(
            id="benefit-id",
            code="CODE-1",
            amount=Decimal("10.00"),
            withdrawal_fee=None,
            withdrawal_fee_prepared_at=None,
            json_ext={},
        )
        benefits = MagicMock()
        benefits.values.return_value = [{
            "code": benefit.code,
            "amount": "10.00",
            "type": "CASH",
            "status": BenefitConsumptionStatus.RECONCILED,
        }]
        benefits.select_related.return_value = [benefit]
        service = CsvReconciliationService(None)

        try:
            with (
                patch.object(service, "_resolve_payroll", return_value=payroll),
                patch("payroll.services.ensure_payroll_is_not_consolidated_source"),
                patch.object(service, "_get_benefit_consumption_qs", return_value=benefits),
                patch.object(service, "_get_source_payrolls_by_benefit", return_value={}),
                patch.object(service, "_get_payroll_micro_catchment", return_value=None),
                patch.object(service, "_get_micro_catchment", return_value=None),
                patch(
                    "payroll.services.WithdrawalFeeConfigService.build_fee_preview_catalog",
                    return_value={},
                ) as build_fee_catalog,
                patch(
                    "payroll.services.WithdrawalFeeConfigService.preview_fee_for_benefit",
                    return_value=(Decimal("2.50"), None),
                ),
            ):
                exported = pd.read_csv(BytesIO(
                    service.download_reconciliation("standard-id").getvalue()
                ))
        finally:
            PayrollConfig.csv_reconciliation_field_mapping = original_mapping

        fee_header = service._withdrawal_fee_header()
        amount_to_pay_header = service._amount_to_pay_header()
        self.assertIn("Code", exported.columns)
        self.assertNotIn("Receipt", exported.columns)
        self.assertIn("Payment type", exported.columns)
        self.assertEqual(list(exported.columns), [
            "Source Payroll",
            "Payroll Name",
            "Payroll Status",
            "District Name",
            "Micro Catchment",
            "Form Number",
            "Phone Number",
            "First Name",
            "Last Name",
            "National ID",
            "Gender",
            "Account Number",
            "Account Name",
            "Bank",
            "Code",
            "Status",
            "Amount",
            fee_header,
            amount_to_pay_header,
            "Payment type",
            "Paid",
        ])
        self.assertTrue(pd.isna(exported.loc[0, "Source Payroll"]))
        self.assertEqual(exported.loc[0, "Payroll Name"], payroll.name)
        self.assertEqual(exported.loc[0, "Payroll Status"], payroll.status)
        self.assertEqual(exported.loc[0, fee_header], 2.5)
        self.assertEqual(exported.loc[0, amount_to_pay_header], 12.5)
        self.assertEqual(exported.loc[0, "Payment type"], "CASH")
        self.assertEqual(exported.loc[0, "Paid"], "Yes")
        build_fee_catalog.assert_called_once()

    def test_custom_configured_headers_are_used_for_export_and_upload_validation(self):
        original_mapping = PayrollConfig.csv_reconciliation_field_mapping
        original_status = PayrollConfig.csv_reconciliation_status_column
        original_paid = PayrollConfig.csv_reconciliation_paid_extra_field
        original_micro_catchment = PayrollConfig.csv_reconciliation_micro_catchment_column
        try:
            configured_mapping = dict(DEFAULT_CONFIG["csv_reconciliation_field_mapping"])
            configured_mapping.update({
                "individual__first_name": "Beneficiary first name",
                "individual__json_ext__national_id": "Beneficiary national ID",
                "amount": "Payout amount",
                "type": "Payment type",
            })
            PayrollConfig.csv_reconciliation_field_mapping = configured_mapping
            PayrollConfig.csv_reconciliation_status_column = "Benefit status"
            PayrollConfig.csv_reconciliation_paid_extra_field = "Payment completed"
            PayrollConfig.csv_reconciliation_micro_catchment_column = "Project catchment"

            service = CsvReconciliationService(None)
            columns = service._reconciliation_export_columns()
            dataframe = pd.DataFrame([{column: "value" for column in columns}])

            self.assertIn("Beneficiary first name", columns)
            self.assertIn("Beneficiary national ID", columns)
            self.assertIn("Payout amount", columns)
            self.assertIn("Payment completed", columns)
            self.assertIn("Project catchment", columns)
            service._validate_dataframe(dataframe)

            with self.assertRaisesRegex(ValueError, "Project catchment"):
                service._validate_dataframe(dataframe.drop(columns=["Project catchment"]))
        finally:
            PayrollConfig.csv_reconciliation_field_mapping = original_mapping
            PayrollConfig.csv_reconciliation_status_column = original_status
            PayrollConfig.csv_reconciliation_paid_extra_field = original_paid
            PayrollConfig.csv_reconciliation_micro_catchment_column = original_micro_catchment

    def test_upload_validation_rejects_a_code_and_paid_only_file(self):
        dataframe = pd.DataFrame([{"Code": "BENEFIT-1", "Paid": "Yes"}])

        with self.assertRaisesRegex(ValueError, "Missing required columns"):
            CsvReconciliationService(None)._validate_dataframe(dataframe)

    def test_micro_catchment_is_required_for_every_upload(self):
        service = CsvReconciliationService(None)
        columns = service._reconciliation_export_columns()
        dataframe = pd.DataFrame([{
            column: "Yes" if column == service._paid_column() else "value"
            for column in columns
            if column != service._micro_catchment_column()
        }])

        with self.assertRaisesRegex(ValueError, service._micro_catchment_column()):
            service._validate_dataframe(dataframe)

    def test_review_download_preserves_uploaded_columns_and_audit_defaults(self):
        upload = SimpleNamespace(id="upload-id", payroll_id="payroll-id")
        contents = (
            b"Code,Date of Birth,receipt,Custom field,Paid\n"
            b"BENEFIT-1,1990-01-01,private-reference,shown,Yes\n"
            b"BENEFIT-2,1991-01-01,other-reference,also shown,No\n"
        )
        rows = MagicMock()
        rows.values_list.return_value = [
            (2, ReconciliationUploadRow.Status.INVALID, ["amount_mismatch", "type_mismatch"]),
        ]
        service = CsvReconciliationService(None)

        with patch("payroll.services.get_authorized_payroll"), patch(
            "payroll.services.ReconciliationUploadRow.objects.filter", return_value=rows,
        ):
            review = pd.read_csv(service.download_upload_review(upload, contents), dtype=str)

        self.assertIn("Date of Birth", review.columns)
        self.assertIn("Custom field", review.columns)
        self.assertNotIn("receipt", review.columns)
        self.assertEqual(review.loc[0, "Validation Status"], ReconciliationUploadRow.Status.INVALID)
        self.assertEqual(review.loc[0, "Mismatch Reasons"], "amount_mismatch; type_mismatch")
        self.assertEqual(review.loc[1, "Validation Status"], "NOT_VALIDATED")
        self.assertTrue(pd.isna(review.loc[1, "Mismatch Reasons"]))

    def test_reconciliation_does_not_store_review_columns_as_extra_info(self):
        service = CsvReconciliationService(SimpleNamespace(login_name="tester"))
        benefit = SimpleNamespace(
            status=BenefitConsumptionStatus.ACCEPTED,
            receipt=None,
            json_ext={},
            withdrawal_fee=None,
            save=MagicMock(),
        )
        row = pd.Series({
            "Validation Status": "INVALID",
            "Mismatch Reasons": "amount_mismatch; type_mismatch",
            "Field officer note": "Keep this",
        })

        with patch("payroll.services.Bill.objects.filter") as bill_filter:
            bill_filter.return_value.first.return_value = None
            service._reconcile_bc(row, benefit, set())

        self.assertEqual(
            benefit.json_ext["extra_info"],
            {"Field officer note": "Keep this"},
        )

    def test_reconciliation_without_receipt_preserves_legacy_reference(self):
        service = CsvReconciliationService(SimpleNamespace(login_name="tester"))
        benefit = SimpleNamespace(
            status=BenefitConsumptionStatus.ACCEPTED,
            receipt="old-reference",
            json_ext={},
            withdrawal_fee=None,
            save=MagicMock(),
        )
        with patch("payroll.services.Bill.objects.filter") as bill_filter:
            bill_filter.return_value.first.return_value = None

            service._reconcile_bc(pd.Series({"code": "BENEFIT-1"}), benefit, {"code"})

        self.assertEqual(benefit.receipt, "old-reference")

    def test_reconciliation_updates_receipt_when_supplied(self):
        service = CsvReconciliationService(SimpleNamespace(login_name="tester"))
        benefit = SimpleNamespace(
            status=BenefitConsumptionStatus.ACCEPTED,
            receipt="old-reference",
            json_ext={},
            withdrawal_fee=None,
            save=MagicMock(),
        )
        with patch("payroll.services.Bill.objects.filter") as bill_filter:
            bill_filter.return_value.first.return_value = None

            service._reconcile_bc(
                pd.Series({"code": "BENEFIT-1", "receipt": "new-reference"}),
                benefit,
                {"code", "receipt"},
            )

        self.assertEqual(benefit.receipt, "new-reference")

    def test_project_name_uses_source_payroll_not_consolidated_link(self):
        project = SimpleNamespace(id="project-id", name="Source project")
        source_payroll = SimpleNamespace(
            json_ext={"filter_criteria": {"project_ids": ["project-id"]}},
        )
        source_link = SimpleNamespace(payroll=source_payroll)
        link_manager = MagicMock()
        link_manager.filter.return_value.select_related.return_value.order_by.return_value.first.return_value = (
            source_link
        )
        individual = SimpleNamespace(
            active_beneficiaries=[
                SimpleNamespace(
                    active_project_enrollments=[
                        SimpleNamespace(project=project),
                    ],
                ),
            ],
        )
        benefit = SimpleNamespace(
            active_payroll_links=[],
            payrollbenefitconsumption_set=link_manager,
            individual=individual,
        )

        info = SimpleNamespace(context=SimpleNamespace(user=SimpleNamespace()))
        with patch(
            "payroll.gql_queries.filter_payroll_queryset",
            side_effect=lambda queryset, *_args: queryset,
        ):
            result = BenefitConsumptionGQLType.resolve_project_name(benefit, info)

        self.assertEqual(result, "Source project")
        link_manager.filter.assert_called_once_with(
            is_deleted=False,
            payroll__is_deleted=False,
            payroll__payroll_type=Payroll.PayrollType.STANDARD,
        )

    def test_consolidated_export_uses_source_payroll_micro_catchment(self):
        source_payroll = SimpleNamespace(
            id="source-id",
            name="Source payroll",
        )
        consolidated_payroll = SimpleNamespace(
            id="consolidated-id",
            name="Consolidated payroll",
            status=PayrollStatus.APPROVE_FOR_PAYMENT,
            payroll_type=Payroll.PayrollType.CONSOLIDATED,
        )
        benefit = SimpleNamespace(
            id="benefit-id",
            code="CODE-1",
            json_ext={"extra_info": {"Dynamic field": "not exported"}},
        )
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
        self.assertEqual(exported.loc[0, "Source Payroll"], source_payroll.name)
        self.assertEqual(exported.loc[0, "Payroll Name"], consolidated_payroll.name)
        self.assertEqual(exported.loc[0, "Payroll Status"], consolidated_payroll.status)
        self.assertTrue({
            "Gender", "Account Number", "Account Name", "Bank",
        }.issubset(exported.columns))
        self.assertNotIn("Date of Birth", exported.columns)
        self.assertNotIn("Dynamic field", exported.columns)
        self.assertEqual(len(exported.columns), 21)

    def test_standard_export_includes_approved_kyc_payment_details(self):
        payroll = SimpleNamespace(
            id="standard-id",
            name="Standard payroll",
            status=PayrollStatus.APPROVE_FOR_PAYMENT,
            payroll_type=Payroll.PayrollType.STANDARD,
        )
        benefit = SimpleNamespace(
            id="benefit-id",
            code="CODE-1",
            json_ext={"extra_info": {"Dynamic field": "not exported"}},
        )
        record = {
            key: "value" for key in PayrollConfig.csv_reconciliation_field_mapping
        }
        record.update({
            "code": benefit.code,
            "json_ext__kyc_payment_details__gender": "Female",
            "json_ext__kyc_payment_details__account_number": "00123456",
            "json_ext__kyc_payment_details__account_name": "Jane Banda",
            "json_ext__kyc_payment_details__bank": "Mpamba",
        })
        benefits = MagicMock()
        benefits.values.return_value = [record]
        benefits.select_related.return_value = [benefit]
        service = CsvReconciliationService(None)

        with (
            patch.object(service, "_resolve_payroll", return_value=payroll),
            patch("payroll.services.ensure_payroll_is_not_consolidated_source"),
            patch.object(service, "_get_benefit_consumption_qs", return_value=benefits),
            patch.object(service, "_get_source_payrolls_by_benefit", return_value={}),
            patch.object(service, "_get_payroll_micro_catchment", return_value=None),
            patch.object(service, "_get_micro_catchment", return_value=None),
        ):
            exported = pd.read_csv(BytesIO(
                service.download_reconciliation("standard-id").getvalue()
            ), dtype=str)

        self.assertEqual(exported.loc[0, "Gender"], "Female")
        self.assertEqual(exported.loc[0, "Account Number"], "00123456")
        self.assertEqual(exported.loc[0, "Account Name"], "Jane Banda")
        self.assertEqual(exported.loc[0, "Bank"], "Mpamba")
        self.assertTrue(pd.isna(exported.loc[0, "Source Payroll"]))
        self.assertEqual(exported.loc[0, "Payroll Name"], payroll.name)
        self.assertEqual(exported.loc[0, "Payroll Status"], payroll.status)
        self.assertNotIn("Date of Birth", exported.columns)
        self.assertNotIn("Dynamic field", exported.columns)
        self.assertEqual(len(exported.columns), 21)

    def test_reconciliation_export_uses_fixed_columns_with_custom_mapping(self):
        original_mapping = PayrollConfig.csv_reconciliation_field_mapping
        PayrollConfig.csv_reconciliation_field_mapping = {
            "code": "Code",
            "json_ext__kyc_payment_details__account_number": "Account Number",
            "json_ext__kyc_payment_details__gender": "Gender",
            "json_ext__kyc_payment_details__account_name": "Account Name",
            "json_ext__kyc_payment_details__bank": "Bank",
            "individual__first_name": "First Name",
        }
        payroll = SimpleNamespace(
            id="standard-id",
            name="Standard payroll",
            status=PayrollStatus.APPROVE_FOR_PAYMENT,
            payroll_type=Payroll.PayrollType.STANDARD,
        )
        benefit = SimpleNamespace(id="benefit-id", code="CODE-1", json_ext={})
        record = {
            field: "value"
            for field in PayrollConfig.csv_reconciliation_field_mapping
        }
        record["code"] = benefit.code
        benefits = MagicMock()
        benefits.values.return_value = [record]
        benefits.select_related.return_value = [benefit]
        service = CsvReconciliationService(None)

        try:
            with (
                patch.object(service, "_resolve_payroll", return_value=payroll),
                patch("payroll.services.ensure_payroll_is_not_consolidated_source"),
                patch.object(service, "_get_benefit_consumption_qs", return_value=benefits),
                patch.object(service, "_get_source_payrolls_by_benefit", return_value={}),
                patch.object(service, "_get_payroll_micro_catchment", return_value=None),
                patch.object(service, "_get_micro_catchment", return_value=None),
            ):
                exported = pd.read_csv(BytesIO(
                    service.download_reconciliation("standard-id").getvalue()
                ))
        finally:
            PayrollConfig.csv_reconciliation_field_mapping = original_mapping

        self.assertEqual(len(exported.columns), 21)
        self.assertEqual(
            list(exported.columns),
            service._reconciliation_export_columns(),
        )

    def test_source_cannot_download_reconciliation_file_during_consolidation(self):
        service = CsvReconciliationService(None)
        source_payroll = SimpleNamespace(
            id="source-id",
            payroll_type=Payroll.PayrollType.STANDARD,
        )

        with (
            patch.object(service, "_resolve_payroll", return_value=source_payroll),
            patch(
                "payroll.services.ensure_payroll_is_not_consolidated_source",
                side_effect=ValidationError("active consolidation"),
            ) as guard,
            patch.object(service, "_get_benefit_consumption_qs") as benefits,
        ):
            with self.assertRaisesRegex(ValidationError, "active consolidation"):
                service.download_reconciliation("source-id")

        guard.assert_called_once_with(
            source_payroll,
            "download a reconciliation file for",
        )
        benefits.assert_not_called()

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
            payroll_id="payroll-id",
            status=CsvReconciliationUpload.Status.WAITING_FOR_VERIFICATION,
            unmatched_records=1,
        )
        for payroll_type in (
            Payroll.PayrollType.STANDARD,
            Payroll.PayrollType.CONSOLIDATED,
        ):
            with self.subTest(payroll_type=payroll_type):
                upload.status = CsvReconciliationUpload.Status.WAITING_FOR_VERIFICATION
                with (
                    patch("payroll.services.Payroll.objects.select_for_update") as lock_payroll,
                    patch("payroll.services.CsvReconciliationUpload.objects.select_for_update") as lock_upload,
                ):
                    lock_payroll.return_value.get.return_value = SimpleNamespace(
                        payroll_type=payroll_type,
                        status=PayrollStatus.APPROVE_FOR_PAYMENT,
                    )
                    lock_upload.return_value.get.return_value = upload
                    with self.assertRaises(ValidationError):
                        CsvReconciliationService(None).apply_upload(upload)

                self.assertEqual(
                    upload.status,
                    CsvReconciliationUpload.Status.WAITING_FOR_VERIFICATION,
                )

    def test_standard_upload_treats_national_id_mismatch_as_warning(self):
        original_setting = PayrollConfig.enable_maker_checker_for_reconciliation_upload
        PayrollConfig.enable_maker_checker_for_reconciliation_upload = True
        self.addCleanup(
            setattr,
            PayrollConfig,
            "enable_maker_checker_for_reconciliation_upload",
            original_setting,
        )
        payroll = SimpleNamespace(
            id="payroll-id",
            payroll_type=Payroll.PayrollType.STANDARD,
            status=PayrollStatus.APPROVE_FOR_PAYMENT,
            name="Standard reconciliation payroll",
            json_ext={},
        )
        upload = SimpleNamespace(
            id="upload-id",
            file_name="payments.csv",
            save=MagicMock(),
        )
        user = SimpleNamespace(login_name="reconciliation-test-user")
        headers = CsvReconciliationService(user)._reconciliation_export_columns()
        values = ["Yes" if header == "Paid" else "value" for header in headers]
        contents = ",".join(headers) + "\n" + ",".join(values)
        file = BytesIO(contents.encode())

        with (
            patch("payroll.services.Payroll.objects.filter") as find_payroll,
            patch("payroll.services.Payroll.objects.select_for_update") as lock_payroll,
            patch("payroll.services.CsvReconciliationUpload.objects.filter") as find_uploads,
            patch("payroll.services.ReconciliationUploadRow") as upload_row_model,
            patch("payroll.services.bulk_create_with_history"),
            patch("payroll.services.ensure_payroll_is_not_consolidated_source"),
            patch("payroll.services.TaskService") as task_service,
            patch.object(
                CsvReconciliationService,
                "_reconciliation_task_context",
                return_value={
                    "included_payrolls": [],
                    "consolidated": False,
                    "district": "District",
                    "phase": "Phase",
                },
            ),
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
                task_service.return_value.create.return_value = {"success": True}
                _, errors, summary = service.upload_reconciliation("payroll-id", file, upload)

        self.assertEqual(
            upload.status,
            CsvReconciliationUpload.Status.WAITING_FOR_VERIFICATION,
        )
        self.assertIsNone(errors)
        self.assertEqual(summary["unmatched_records"], 0)
        self.assertEqual(summary["matched_records"], 1)
        task_service.return_value.create.assert_called_once()
        upload_row_model.assert_called_once()

    def test_standard_upload_fails_as_a_batch_on_blocking_rows(self):
        payroll = SimpleNamespace(
            id="payroll-id",
            payroll_type=Payroll.PayrollType.STANDARD,
            status=PayrollStatus.APPROVE_FOR_PAYMENT,
            name="Standard reconciliation payroll",
            json_ext={},
        )
        upload = SimpleNamespace(
            id="upload-id",
            file_name="payments.csv",
            save=MagicMock(),
        )
        headers = CsvReconciliationService(None)._reconciliation_export_columns() + ["receipt"]
        row_values = ["value"] * (len(headers) - 1) + ["provider-ref-1"]
        contents = ",".join(headers) + "\n" + ",".join(row_values)
        file = BytesIO(contents.encode())
        user = SimpleNamespace(login_name="reconciliation-test-user")

        with (
            patch("payroll.services.Payroll.objects.filter") as find_payroll,
            patch("payroll.services.Payroll.objects.select_for_update") as lock_payroll,
            patch("payroll.services.CsvReconciliationUpload.objects.filter") as find_uploads,
            patch("payroll.services.ReconciliationUploadRow") as upload_row_constructor,
            patch("payroll.services.bulk_create_with_history") as bulk_create,
            patch("payroll.services.ensure_payroll_is_not_consolidated_source"),
            patch("payroll.services.TaskService") as task_service,
            patch.object(
                CsvReconciliationService,
                "_reconciliation_task_context",
                return_value={
                    "included_payrolls": [],
                    "consolidated": False,
                    "district": "District",
                    "phase": "Phase",
                },
            ),
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
                    return_value=(None, ["amount_mismatch"]),
                ),
            ):
                task_service.return_value.create.return_value = {"success": True}
                _, errors, summary = service.upload_reconciliation("payroll-id", file, upload)

        self.assertEqual(errors, {"2": ["amount_mismatch"]})
        self.assertEqual(summary["unmatched_records"], 1)
        self.assertEqual(upload.status, CsvReconciliationUpload.Status.FAIL)
        task_service.return_value.create.assert_not_called()
        self.assertEqual(
            upload_row_constructor.call_args.kwargs["submitted_data"]["receipt"],
            "provider-ref-1",
        )

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
            context = CsvReconciliationService(None)._reconciliation_task_context(
                consolidated_payroll
            )

        self.assertEqual(context["district"], "Karonga, Mzimba")
        self.assertEqual(
            [item["name"] for item in context["included_payrolls"]],
            ["Payroll One", "Payroll Two"],
        )

    def test_reject_approved_consolidated_payroll_uses_user_scoped_context(self):
        payroll = SimpleNamespace(
            id="consolidated-id",
            payroll_type=Payroll.PayrollType.CONSOLIDATED,
        )
        user = SimpleNamespace(id="user-id", login_name="user", username="user")
        task_context = {
            "included_payrolls": [],
            "consolidated": True,
            "district": None,
            "phase": None,
        }

        with (
            patch(
                "payroll.consolidation.ensure_payroll_is_not_consolidated_source",
            ),
            patch(
                "core.services.utils.serviceUtils.model_representation",
                return_value={"id": payroll.id},
            ),
            patch.object(StrategyOfPaymentInterface, "change_status_of_payroll"),
            patch.object(
                CsvReconciliationService,
                "_reconciliation_task_context",
                autospec=True,
                return_value=task_context,
            ) as context,
            patch.object(
                PayrollService,
                "create_accept_payroll_task",
            ) as create_task,
        ):
            StrategyOfPaymentInterface.reject_approved_payroll(payroll, user)

        service, context_payroll = context.call_args.args
        self.assertIsInstance(service, CsvReconciliationService)
        self.assertIs(service.user, user)
        self.assertIs(context_payroll, payroll)
        create_task.assert_called_once_with(
            payroll.id,
            {"id": payroll.id, **task_context},
        )

    def test_task_failure_handler_has_task_service_available(self):
        # The reconciliation completion signal uses this service to change a
        # checker task to FAILED when applying its upload raises an error.
        self.assertIsNotNone(signals.TaskService)

    def test_payroll_task_authorization_runs_before_completion(self):
        user = SimpleNamespace(id="user-id")
        task = SimpleNamespace(
            id="task-id",
            business_event=PayrollConfig.payroll_accept_event,
            entity_id="payroll-id",
        )
        task_query = MagicMock()
        task_query.first.return_value = task

        with (
            patch(
                "payroll.signals.benefit_plan_access_control_enabled",
                return_value=True,
            ),
            patch("payroll.signals.Task.objects.filter", return_value=task_query),
            patch(
                "payroll.signals.get_authorized_payroll",
                side_effect=PermissionDenied("unauthorized"),
            ) as authorize,
        ):
            with self.assertRaises(PermissionDenied):
                signals._authorize_payroll_task_completion(
                    cls_=SimpleNamespace(user=user),
                    data=[({"id": task.id},), {}],
                )

        authorize.assert_called_once_with(user, task.entity_id)

    def test_authorized_payroll_approval_still_runs_after_completion(self):
        user = SimpleNamespace(id="user-id")
        payroll = SimpleNamespace(id="payroll-id", payment_method="offline")
        strategy = SimpleNamespace(accept_payroll=MagicMock())
        with patch("payroll.signals.bind_service_signal") as bind_service_signal:
            signals.bind_service_signals()
        handler = next(
            call.args[1]
            for call in bind_service_signal.call_args_list
            if (
                call.args[1].__name__ == "on_task_complete_accept_payroll"
                and call.kwargs["bind_type"] == ServiceSignalBindType.AFTER
            )
        )

        with (
            patch(
                "payroll.signals.benefit_plan_access_control_enabled",
                return_value=True,
            ),
            patch("payroll.signals.User.objects.get", return_value=user),
            patch(
                "payroll.signals.get_authorized_payroll",
                return_value=payroll,
            ),
            patch(
                "payroll.signals.PaymentMethodStorage.get_chosen_payment_method",
                return_value=strategy,
            ),
        ):
            handler(result={
                "success": True,
                "data": {
                    "user": {"id": user.id},
                    "task": {
                        "id": "task-id",
                        "business_event": PayrollConfig.payroll_accept_event,
                        "entity_id": payroll.id,
                        "status": Task.Status.COMPLETED,
                    },
                },
            })

        strategy.accept_payroll.assert_called_once_with(payroll, user)

    def test_completed_offline_reconciliation_rechecks_source_membership(self):
        user = SimpleNamespace(id="user-id", login_name="user")
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
            patch("payroll.signals.TaskService") as task_service,
        ):
            task_service.return_value.complete_task.return_value = {"success": True}
            handler(result={
                "success": True,
                "data": {
                    "user": {"id": user.id},
                    "task": {
                        "id": "task-id",
                        "business_event": PayrollConfig.payroll_reconciliation_event,
                        "status": Task.Status.COMPLETED,
                        "entity_id": "payroll-id",
                        "data": {},
                    },
                },
            })

        ensure_not_consolidated.assert_called_once_with(payroll, "reconcile")
        get_strategy.assert_not_called()
        task_service.return_value.complete_task.assert_called_once_with(
            {"id": "task-id", "failed": True},
        )

    def test_completed_reject_delete_and_benefit_tasks_fail_when_callback_errors(self):
        user = SimpleNamespace(id="user-id")
        payroll = SimpleNamespace(payment_method="offline")
        benefit = SimpleNamespace()
        with patch("payroll.signals.bind_service_signal") as bind_service_signal:
            signals.bind_service_signals()
        handlers = {
            call.args[1].__name__: call.args[1]
            for call in bind_service_signal.call_args_list
        }
        cases = (
            (
                "on_task_complete_payroll_reject_approved_payroll",
                PayrollConfig.payroll_reject_event,
                "payroll-id",
            ),
            ("on_task_delete_payroll", PayrollConfig.payroll_delete_event, "payroll-id"),
            ("on_task_delete_benefit", PayrollConfig.benefit_delete_event, "benefit-id"),
        )

        for handler_name, event, entity_id in cases:
            with self.subTest(event=event):
                task = {
                    "id": "task-id",
                    "business_event": event,
                    "status": Task.Status.COMPLETED,
                    "entity_id": entity_id,
                    "data": {},
                }
                strategy = SimpleNamespace(
                    reject_approved_payroll=MagicMock(
                        side_effect=ValidationError("Callback failed"),
                    ),
                )
                with (
                    patch("payroll.signals.User.objects.get", return_value=user),
                    patch("payroll.signals.Payroll.objects.get", return_value=payroll),
                    patch("payroll.signals.BenefitConsumption.objects.get", return_value=benefit),
                    patch(
                        "payroll.signals.ensure_payroll_is_not_consolidated_source",
                        side_effect=ValidationError("Source payroll is locked"),
                    ),
                    patch(
                        "payroll.signals.ensure_benefit_is_not_in_active_consolidation",
                        side_effect=ValidationError("Benefit is in consolidation"),
                    ),
                    patch(
                        "payroll.signals.PaymentMethodStorage.get_chosen_payment_method",
                        return_value=strategy,
                    ),
                    patch("payroll.signals.TaskService") as task_service,
                ):
                    task_service.return_value.complete_task.return_value = {"success": True}
                    handlers[handler_name](result={
                        "success": True,
                        "data": {"user": {"id": user.id}, "task": task},
                    })

                task_service.return_value.complete_task.assert_called_once_with(
                    {"id": "task-id", "failed": True},
                )


class ReconciliationPayrollStatusTests(TestCase):
    def setUp(self):
        self.user = LogInHelper().get_or_create_user_api(
            username='recon-status-user',
            password='PayrollTestPassword1!',
        )

    def test_upload_applies_immediately_when_maker_checker_is_disabled(self):
        original_setting = PayrollConfig.enable_maker_checker_for_reconciliation_upload
        PayrollConfig.enable_maker_checker_for_reconciliation_upload = False
        self.addCleanup(
            setattr,
            PayrollConfig,
            "enable_maker_checker_for_reconciliation_upload",
            original_setting,
        )
        individual = Individual(**service_add_individual_payload)
        individual.save(username=self.user.login_name)
        payroll = Payroll(
            name="Immediate reconciliation test",
            status=PayrollStatus.APPROVE_FOR_PAYMENT,
        )
        payroll.save(username=self.user.login_name)
        benefit = BenefitConsumption(
            individual=individual,
            code="immediate-reconciliation-code",
            date_due=date(2026, 1, 1),
            amount=Decimal("100"),
            type="Cash Transfer",
            status=BenefitConsumptionStatus.ACCEPTED,
        )
        benefit.save(username=self.user.login_name)
        PayrollBenefitConsumption(payroll=payroll, benefit=benefit).save(
            username=self.user.login_name,
        )
        service = CsvReconciliationService(self.user)
        exported = service.download_reconciliation(payroll.id)
        exported.seek(0)
        dataframe = pd.read_csv(exported, dtype=str, keep_default_na=False)
        dataframe[PayrollConfig.csv_reconciliation_paid_extra_field] = (
            PayrollConfig.csv_reconciliation_paid_yes
        )
        submitted = BytesIO()
        dataframe.to_csv(submitted, index=False)
        submitted.seek(0)
        upload = CsvReconciliationUpload(file_name="immediate-reconciliation.csv")

        _, errors, summary = service.upload_reconciliation(payroll.id, submitted, upload)

        upload.refresh_from_db()
        benefit.refresh_from_db()
        payroll.refresh_from_db()
        self.assertIsNone(errors)
        self.assertEqual(summary["status"], CsvReconciliationUpload.Status.SUCCESS)
        self.assertEqual(upload.status, CsvReconciliationUpload.Status.SUCCESS)
        self.assertEqual(benefit.status, BenefitConsumptionStatus.RECONCILED)
        self.assertEqual(payroll.status, PayrollStatus.RECONCILED)
        self.assertFalse(Task.objects.filter(
            entity_id=str(payroll.id),
            business_event=PayrollConfig.payroll_reconciliation_event,
            data__upload_id=str(upload.id),
        ).exists())

    def test_standard_upload_rejects_all_rows_when_any_blocking_row_exists(self):
        individual = Individual(**service_add_individual_payload)
        individual.save(username=self.user.login_name)
        payroll = Payroll(
            name="Partial standard reconciliation test",
            status=PayrollStatus.APPROVE_FOR_PAYMENT,
        )
        payroll.save(username=self.user.login_name)
        benefit = BenefitConsumption(
            individual=individual,
            code="partial-standard-valid-code",
            date_due=date(2026, 1, 1),
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
            mismatch_reasons=["amount_mismatch"],
            submitted_data={"Paid": "Yes", "receipt": "invalid-row-receipt"},
            status=ReconciliationUploadRow.Status.INVALID,
        ).save(username=self.user.login_name)
        service = CsvReconciliationService(self.user)

        with self.assertRaises(ValidationError):
            service.apply_upload(upload)
        upload.refresh_from_db()
        payroll.refresh_from_db()
        benefit.refresh_from_db()
        self.assertEqual(upload.status, CsvReconciliationUpload.Status.WAITING_FOR_VERIFICATION)
        self.assertEqual(payroll.status, PayrollStatus.APPROVE_FOR_PAYMENT)
        self.assertEqual(benefit.status, BenefitConsumptionStatus.ACCEPTED)
        self.assertIsNone(benefit.receipt)

    def test_offline_reconciliation_releases_incomplete_source_for_recovery(self):
        individual = Individual(**service_add_individual_payload)
        individual.save(username=self.user.login_name)
        source = Payroll(
            name="Offline reconciliation source",
            status=PayrollStatus.APPROVE_FOR_PAYMENT,
        )
        source.save(username=self.user.login_name)
        consolidated = Payroll(
            name="Offline reconciliation parent",
            status=PayrollStatus.APPROVE_FOR_PAYMENT,
            payroll_type=Payroll.PayrollType.CONSOLIDATED,
        )
        consolidated.save(username=self.user.login_name)
        ConsolidatedPayrollMember(
            consolidated_payroll=consolidated,
            source_payroll=source,
        ).save(username=self.user.login_name)
        benefit = BenefitConsumption(
            individual=individual,
            code="offline-unreconciled-benefit",
            date_due=date(2026, 1, 1),
            amount=Decimal("100"),
            type="Cash Transfer",
            status=BenefitConsumptionStatus.APPROVE_FOR_PAYMENT,
        )
        benefit.save(username=self.user.login_name)
        paid_benefit = BenefitConsumption(
            individual=individual,
            code="offline-reconciled-benefit",
            date_due=date(2026, 1, 1),
            amount=Decimal("100"),
            type="Cash Transfer",
            status=BenefitConsumptionStatus.RECONCILED,
        )
        paid_benefit.save(username=self.user.login_name)
        for payroll, linked_benefit in (
            (source, benefit),
            (consolidated, benefit),
            (source, paid_benefit),
            (consolidated, paid_benefit),
        ):
            PayrollBenefitConsumption(payroll=payroll, benefit=linked_benefit).save(
                username=self.user.login_name,
            )

        from payroll.strategies.strategy_offline_payments import StrategyOfflinePayment

        StrategyOfflinePayment.reconcile_payroll(consolidated, self.user)

        source.refresh_from_db()
        self.assertEqual(source.status, PayrollStatus.RECONCILED)
        member = ConsolidatedPayrollMember.objects.get(
            consolidated_payroll=consolidated,
            source_payroll=source,
        )
        self.assertFalse(member.is_deleted)
        self.assertTrue(
            ConsolidatedPayrollMember.objects.filter(
                pk=member.pk,
                consolidated_payroll=consolidated,
            ).exists(),
        )
        self.assertEqual(
            CsvReconciliationService(self.user)._get_source_payrolls_by_benefit(
                consolidated
            )[benefit.id],
            source,
        )

        destination = Payroll(
            name="Unpaid benefit recovery",
            status=PayrollStatus.PENDING_APPROVAL,
        )
        destination.save(username=self.user.login_name)
        PayrollService(self.user)._move_benefit_consumptions(destination, source.id)

        self.assertFalse(PayrollBenefitConsumption.objects.filter(
            payroll=source,
            benefit=benefit,
            is_deleted=False,
        ).exists())
        self.assertTrue(PayrollBenefitConsumption.objects.filter(
            payroll=destination,
            benefit=benefit,
            is_deleted=False,
        ).exists())
        self.assertTrue(PayrollBenefitConsumption.objects.filter(
            payroll=source,
            benefit=paid_benefit,
            is_deleted=False,
        ).exists())
        self.assertFalse(PayrollBenefitConsumption.objects.filter(
            payroll=consolidated,
            benefit=benefit,
            is_deleted=False,
        ).exists())
        self.assertTrue(PayrollBenefitConsumption.objects.filter(
            payroll=consolidated,
            benefit=paid_benefit,
            is_deleted=False,
        ).exists())
        self.assertFalse(PayrollBenefitConsumption.objects.filter(
            payroll=destination,
            benefit=paid_benefit,
            is_deleted=False,
        ).exists())
        with self.assertRaises(ValidationError):
            PayrollService(self.user).delete({"id": source.id})
        benefit.refresh_from_db()
        self.assertEqual(benefit.status, BenefitConsumptionStatus.ACCEPTED)
        paid_benefit.refresh_from_db()
        self.assertEqual(paid_benefit.status, BenefitConsumptionStatus.RECONCILED)

    def test_gateway_reconciliation_marks_incomplete_sources_for_recovery(self):
        individual = Individual(**service_add_individual_payload)
        individual.save(username=self.user.login_name)
        completed_source = Payroll(
            name="Gateway completed source",
            status=PayrollStatus.APPROVE_FOR_PAYMENT,
        )
        completed_source.save(username=self.user.login_name)
        pending_source = Payroll(
            name="Gateway pending source",
            status=PayrollStatus.APPROVE_FOR_PAYMENT,
        )
        pending_source.save(username=self.user.login_name)
        consolidated = Payroll(
            name="Gateway reconciliation parent",
            status=PayrollStatus.APPROVE_FOR_PAYMENT,
            payroll_type=Payroll.PayrollType.CONSOLIDATED,
        )
        consolidated.save(username=self.user.login_name)
        for source in (completed_source, pending_source):
            ConsolidatedPayrollMember(
                consolidated_payroll=consolidated,
                source_payroll=source,
            ).save(username=self.user.login_name)
        completed_benefit = BenefitConsumption(
            individual=individual,
            code="gateway-completed-benefit",
            date_due=date(2026, 1, 1),
            amount=Decimal("100"),
            type="Cash Transfer",
            status=BenefitConsumptionStatus.APPROVE_FOR_PAYMENT,
        )
        completed_benefit.save(username=self.user.login_name)
        pending_benefit = BenefitConsumption(
            individual=individual,
            code="gateway-pending-benefit",
            date_due=date(2026, 1, 1),
            amount=Decimal("100"),
            type="Cash Transfer",
            status=BenefitConsumptionStatus.APPROVE_FOR_PAYMENT,
        )
        pending_benefit.save(username=self.user.login_name)
        for source, benefit in (
            (completed_source, completed_benefit),
            (pending_source, pending_benefit),
            (consolidated, completed_benefit),
            (consolidated, pending_benefit),
        ):
            PayrollBenefitConsumption(payroll=source, benefit=benefit).save(
                username=self.user.login_name,
            )

        from payroll.tasks import send_request_to_reconcile

        def mark_reconciled(benefits, user):
            for benefit in benefits:
                benefit.status = BenefitConsumptionStatus.RECONCILED
                benefit.save(username=user.login_name)

        with (
            patch("payroll.tasks.StrategyOnlinePayment.initialize_payment_gateway"),
            patch(
                "payroll.tasks.StrategyOnlinePayment.get_benefits_attached_to_payroll",
                return_value=[completed_benefit, pending_benefit],
            ) as get_benefits,
            patch("payroll.tasks.StrategyOnlinePayment.PAYMENT_GATEWAY") as gateway,
            patch(
                "payroll.tasks.StrategyOnlinePayment.reconcile_benefit_consumption",
                side_effect=mark_reconciled,
            ),
        ):
            gateway.reconcile.side_effect = [True, False]
            send_request_to_reconcile.run(str(consolidated.id), str(self.user.id))

        get_benefits.assert_called_once_with(
            consolidated,
            BenefitConsumptionStatus.APPROVE_FOR_PAYMENT,
        )
        completed_source.refresh_from_db()
        pending_source.refresh_from_db()
        self.assertEqual(completed_source.status, PayrollStatus.RECONCILED)
        self.assertEqual(pending_source.status, PayrollStatus.RECONCILED)
        self.assertEqual(ConsolidatedPayrollMember.objects.filter(
            consolidated_payroll=consolidated,
            source_payroll__in=[completed_source, pending_source],
            is_deleted=False,
        ).count(), 2)

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
            date_due=date(2026, 1, 1),
            amount=Decimal("100"),
            type="Cash Transfer",
            status=BenefitConsumptionStatus.ACCEPTED,
        )
        benefit_a.save(username=self.user.login_name)
        benefit_b = BenefitConsumption(
            individual=individual,
            code="pending-source-code",
            date_due=date(2026, 1, 1),
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

        StrategyOfPaymentInterface.change_status_of_payroll(
            consolidated_payroll,
            PayrollStatus.RECONCILED,
            self.user,
        )

        source_a.refresh_from_db()
        source_b.refresh_from_db()
        self.assertEqual(source_a.status, PayrollStatus.RECONCILED)
        self.assertEqual(source_b.status, PayrollStatus.RECONCILED)

    def test_invalid_upload_batch_does_not_apply_its_valid_rows(self):
        individual = Individual(**service_add_individual_payload)
        individual.save(username=self.user.login_name)
        payroll = Payroll(
            name="All-or-nothing reconciliation test",
            status=PayrollStatus.APPROVE_FOR_PAYMENT,
            payroll_type=Payroll.PayrollType.CONSOLIDATED,
        )
        payroll.save(username=self.user.login_name)
        benefit = BenefitConsumption(
            individual=individual,
            code="all-or-nothing-valid-code",
            date_due=date(2026, 1, 1),
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

    def test_releasing_consolidation_invalidates_pending_uploads(self):
        individual = Individual(**service_add_individual_payload)
        individual.save(username=self.user.login_name)
        source = Payroll(name="Consolidation upload source", status=PayrollStatus.APPROVE_FOR_PAYMENT)
        source.save(username=self.user.login_name)
        consolidated = Payroll(
            name="Consolidation upload parent",
            status=PayrollStatus.REJECTED,
            payroll_type=Payroll.PayrollType.CONSOLIDATED,
        )
        consolidated.save(username=self.user.login_name)
        ConsolidatedPayrollMember(
            consolidated_payroll=consolidated,
            source_payroll=source,
        ).save(username=self.user.login_name)
        benefit = BenefitConsumption(
            individual=individual,
            code="released-upload-benefit",
            date_due=date(2026, 1, 1),
            amount=Decimal("100"),
            type="Cash Transfer",
        )
        benefit.save(username=self.user.login_name)
        PayrollBenefitConsumption(payroll=source, benefit=benefit).save(username=self.user.login_name)
        PayrollBenefitConsumption(payroll=consolidated, benefit=benefit).save(username=self.user.login_name)
        upload = CsvReconciliationUpload(
            payroll=consolidated,
            status=CsvReconciliationUpload.Status.WAITING_FOR_VERIFICATION,
        )
        upload.save(username=self.user.login_name)

        StrategyOfPaymentInterface.remove_benefits_from_rejected_payroll(
            consolidated, user=self.user,
        )

        upload.refresh_from_db()
        self.assertEqual(upload.status, CsvReconciliationUpload.Status.FAIL)
        self.assertTrue(upload.error["consolidation_released"])

    def test_upload_cannot_apply_after_parent_becomes_rejected(self):
        individual = Individual(**service_add_individual_payload)
        individual.save(username=self.user.login_name)
        consolidated = Payroll(
            name="Rejected consolidation upload",
            status=PayrollStatus.REJECTED,
            payroll_type=Payroll.PayrollType.CONSOLIDATED,
        )
        consolidated.save(username=self.user.login_name)
        benefit = BenefitConsumption(
            individual=individual,
            code="rejected-consolidation-benefit",
            date_due=date(2026, 1, 1),
            amount=Decimal("100"),
            type="Cash Transfer",
            status=BenefitConsumptionStatus.ACCEPTED,
        )
        benefit.save(username=self.user.login_name)
        PayrollBenefitConsumption(payroll=consolidated, benefit=benefit).save(
            username=self.user.login_name,
        )
        upload = CsvReconciliationUpload(
            payroll=consolidated,
            status=CsvReconciliationUpload.Status.WAITING_FOR_VERIFICATION,
        )
        upload.save(username=self.user.login_name)
        ReconciliationUploadRow(
            upload=upload,
            row_number=2,
            benefit=benefit,
            submitted_data={"Paid": "Yes"},
            status=ReconciliationUploadRow.Status.VALID,
        ).save(username=self.user.login_name)

        with self.assertRaises(ValidationError):
            CsvReconciliationService(self.user).apply_upload(upload)

        benefit.refresh_from_db()
        self.assertEqual(benefit.status, BenefitConsumptionStatus.ACCEPTED)

    def test_upload_cannot_apply_benefits_no_longer_linked_to_parent(self):
        individual = Individual(**service_add_individual_payload)
        individual.save(username=self.user.login_name)
        source = Payroll(name="Reassigned consolidation source", status=PayrollStatus.APPROVE_FOR_PAYMENT)
        source.save(username=self.user.login_name)
        old_parent = Payroll(
            name="Old consolidation parent",
            status=PayrollStatus.APPROVE_FOR_PAYMENT,
            payroll_type=Payroll.PayrollType.CONSOLIDATED,
        )
        old_parent.save(username=self.user.login_name)
        ConsolidatedPayrollMember(
            consolidated_payroll=old_parent,
            source_payroll=source,
        ).save(username=self.user.login_name)
        benefit = BenefitConsumption(
            individual=individual,
            code="reassigned-consolidation-benefit",
            date_due=date(2026, 1, 1),
            amount=Decimal("100"),
            type="Cash Transfer",
            status=BenefitConsumptionStatus.ACCEPTED,
        )
        benefit.save(username=self.user.login_name)
        PayrollBenefitConsumption(payroll=source, benefit=benefit).save(
            username=self.user.login_name,
        )
        old_link = PayrollBenefitConsumption(payroll=old_parent, benefit=benefit)
        old_link.save(username=self.user.login_name)
        upload = CsvReconciliationUpload(
            payroll=old_parent,
            status=CsvReconciliationUpload.Status.WAITING_FOR_VERIFICATION,
        )
        upload.save(username=self.user.login_name)
        ReconciliationUploadRow(
            upload=upload,
            row_number=2,
            benefit=benefit,
            submitted_data={"Paid": "Yes"},
            status=ReconciliationUploadRow.Status.VALID,
        ).save(username=self.user.login_name)

        old_link.delete(user=self.user)
        new_parent = Payroll(
            name="New consolidation parent",
            status=PayrollStatus.APPROVE_FOR_PAYMENT,
            payroll_type=Payroll.PayrollType.CONSOLIDATED,
        )
        new_parent.save(username=self.user.login_name)
        ConsolidatedPayrollMember(
            consolidated_payroll=new_parent,
            source_payroll=source,
        ).save(username=self.user.login_name)
        PayrollBenefitConsumption(payroll=new_parent, benefit=benefit).save(
            username=self.user.login_name,
        )

        with self.assertRaises(ValidationError):
            CsvReconciliationService(self.user).apply_upload(upload)

        benefit.refresh_from_db()
        self.assertEqual(benefit.status, BenefitConsumptionStatus.ACCEPTED)
