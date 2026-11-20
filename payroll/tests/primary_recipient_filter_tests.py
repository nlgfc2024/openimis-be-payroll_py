from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from payroll.services import CsvReconciliationService, PayrollService


class PrimaryRecipientFilterTests(SimpleTestCase):
    @patch("payroll.services.apps.get_model")
    def test_single_project_payroll_uses_its_project_catchment(self, get_model):
        project = SimpleNamespace(micro_catchment=SimpleNamespace(name="Project Catchment"))
        get_model.return_value.objects.filter.return_value.select_related.return_value.first.return_value = project
        payroll = SimpleNamespace(
            json_ext={"filter_criteria": {"project_ids": ["project-id"]}},
        )

        result = CsvReconciliationService(None)._get_payroll_micro_catchment(payroll)

        self.assertEqual(result, "Project Catchment")
        get_model.return_value.objects.filter.assert_called_once_with(
            id="project-id", is_deleted=False,
        )

    def test_multi_project_payroll_has_no_single_project_catchment(self):
        payroll = SimpleNamespace(
            json_ext={"filter_criteria": {"project_ids": ["project-a", "project-b"]}},
        )

        self.assertIsNone(CsvReconciliationService(None)._get_payroll_micro_catchment(payroll))

    @patch("payroll.services.get_calculation_object")
    def test_group_payroll_passes_explicit_household_enrollments_to_calculation(self, calculation_object):
        calculation = MagicMock()
        calculation_object.return_value = calculation
        payment_plan = SimpleNamespace(calculation="calculation-id")
        payroll = SimpleNamespace(id="payroll-id")
        payment_cycle = SimpleNamespace(id="cycle-id")
        enrollments = MagicMock()
        service = PayrollService(SimpleNamespace(id="user-id"))

        service._generate_benefits(
            payment_plan,
            "2026-10-01",
            "2026-10-31",
            payroll,
            payment_cycle,
            enrollments_queryset=enrollments,
        )

        calculation.calculate_if_active_for_object.assert_called_once_with(
            payment_plan,
            user_id="user-id",
            start_date="2026-10-01",
            end_date="2026-10-31",
            payroll=payroll,
            payment_cycle=payment_cycle,
            enrollments_queryset=enrollments,
        )

    @patch("payroll.services.apps.get_model")
    @patch("payroll.services.Beneficiary.objects.filter")
    def test_project_payroll_requires_a_payable_individual_and_project_enrollment(self, beneficiary_filter, _get_model):
        initial_queryset = MagicMock()
        project_queryset = MagicMock()
        beneficiary_filter.return_value = initial_queryset
        initial_queryset.filter.return_value = project_queryset
        project_queryset.distinct.return_value = project_queryset

        payment_plan = SimpleNamespace(benefit_plan=SimpleNamespace(id="benefit-plan-id"))
        PayrollService(None)._select_beneficiary_based_on_criteria({
            "json_ext": {"filter_criteria": {"project_ids": ["project-id"]}},
        }, payment_plan)

        # PRIMARY recipients, or individuals who belong to no household.
        initial_queryset.filter.assert_called_once()
        self.assertEqual(len(initial_queryset.filter.call_args.args), 1)
        self.assertEqual(initial_queryset.filter.call_args.kwargs, {})
        project_queryset.filter.assert_called_once_with(
            project_enrollments__project__id__in=["project-id"],
            project_enrollments__is_deleted=False,
        )

    @patch("payroll.services.apps.get_model")
    def test_group_payroll_uses_existing_primary_recipient_membership(self, get_model):
        enrollments = MagicMock()
        enrollments.distinct.return_value = enrollments
        get_model.return_value.objects.filter.return_value = enrollments
        payment_plan = SimpleNamespace(benefit_plan=SimpleNamespace(id="benefit-plan-id"))

        result = PayrollService(None)._select_group_enrollments_based_on_criteria(
            {"json_ext": {}}, payment_plan,
        )

        self.assertIs(result, enrollments)
        get_model.assert_called_once_with(
            "project_social_protection", "GroupBeneficiaryProjectEnrollment",
        )
        get_model.return_value.objects.filter.assert_called_once()
        call = get_model.return_value.objects.filter.call_args
        self.assertEqual(call.kwargs, dict(
            group_beneficiary__benefit_plan_id="benefit-plan-id",
            group_beneficiary__status="ACTIVE",
            group_beneficiary__is_deleted=False,
            group_beneficiary__group__groupindividuals__recipient_type="PRIMARY",
            group_beneficiary__group__groupindividuals__is_deleted=False,
            project__is_deleted=False,
            is_deleted=False,
        ))
        # COMPLETED projects, or TRAINING projects when the project module has them.
        self.assertIn(("project__status", "COMPLETED"), call.args[0].children)

    @patch("payroll.services.BenefitConsumption.objects.filter")
    def test_payment_details_are_snapshotted_once_on_generated_benefits(self, benefit_filter):
        benefit = SimpleNamespace(
            json_ext={"extra_info": {"provider_note": "keep"}},
            individual=SimpleNamespace(json_ext={
                "gender": "Female",
                "account_number": "00123456",
                "account_name": "Ada Lovelace",
                "bank": "Example Bank",
            }),
            save=MagicMock(),
        )
        benefit_filter.return_value.select_related.return_value = [benefit]
        payroll = SimpleNamespace(id="payroll-id")
        user = SimpleNamespace(id="user-id")
        service = PayrollService(user)

        service._snapshot_kyc_payment_details(payroll)
        self.assertEqual(benefit.json_ext, {
            "extra_info": {"provider_note": "keep"},
            "kyc_payment_details": {
                "gender": "Female",
                "account_number": "00123456",
                "account_name": "Ada Lovelace",
                "bank": "Example Bank",
            },
        })

        benefit.individual.json_ext["account_number"] = "99999999"
        service._snapshot_kyc_payment_details(payroll)
        self.assertEqual(
            benefit.json_ext["kyc_payment_details"]["account_number"],
            "00123456",
        )
        self.assertEqual(benefit.save.call_count, 1)

    @patch("payroll.services.BenefitConsumption.objects.filter")
    def test_payment_snapshot_handles_a_benefit_without_an_individual(self, benefit_filter):
        benefit = SimpleNamespace(json_ext=None, individual=None, save=MagicMock())
        benefit_filter.return_value.select_related.return_value = [benefit]

        PayrollService(SimpleNamespace(id="user-id"))._snapshot_kyc_payment_details(
            SimpleNamespace(id="payroll-id")
        )

        self.assertEqual(benefit.json_ext, {
            "kyc_payment_details": {
                "gender": None,
                "account_number": None,
                "account_name": None,
                "bank": None,
            },
        })
        benefit.save.assert_called_once()

    @patch("payroll.services.PayrollBenefitConsumption.objects.filter")
    @patch("payroll.services.BenefitAttachment.objects.filter")
    @patch("payroll.services.apps.get_model")
    def test_group_benefits_are_reassigned_to_the_primary_recipient(
        self, get_model, attachment_filter, payroll_benefit_filter,
    ):
        enrollments = MagicMock()
        enrollments.values_list.return_value = [("beneficiary-id", "group-id")]
        get_model.return_value.objects.filter.return_value.values_list.return_value = [
            ("group-id", "recipient-id"),
        ]
        benefit = SimpleNamespace(id="benefit-id", individual_id="worker-id", save=MagicMock())
        attachment = SimpleNamespace(
            benefit=benefit,
            benefit_id="benefit-id",
            bill=SimpleNamespace(subject_id="beneficiary-id"),
        )
        attachment_filter.return_value.select_related.return_value = [attachment]
        payroll_benefit_filter.return_value.values_list.return_value = ["benefit-id"]

        PayrollService(SimpleNamespace(id="user-id"))._assign_group_benefits_to_primary_recipients(
            SimpleNamespace(id="payroll-id"), enrollments,
        )

        self.assertEqual(benefit.individual_id, "recipient-id")
        benefit.save.assert_called_once()

    @patch("payroll.services.BenefitConsumption.objects.filter")
    def test_reconciliation_export_uses_saved_payroll_benefits(self, benefit_filter):
        queryset = MagicMock()
        queryset.distinct.return_value = queryset
        queryset.exists.return_value = True
        benefit_filter.return_value = queryset
        payroll = SimpleNamespace(id="payroll-id")

        result = CsvReconciliationService(None)._get_benefit_consumption_qs(payroll)

        self.assertIs(result, queryset)
        benefit_filter.assert_called_once_with(
            payrollbenefitconsumption__payroll=payroll,
            is_deleted=False,
        )
