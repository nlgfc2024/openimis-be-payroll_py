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

    @patch("payroll.services.Beneficiary.objects.filter")
    def test_project_payroll_requires_primary_recipient_and_project_enrollment(self, beneficiary_filter):
        initial_queryset = MagicMock()
        primary_queryset = MagicMock()
        project_queryset = MagicMock()
        beneficiary_filter.return_value = initial_queryset
        initial_queryset.filter.return_value = primary_queryset
        primary_queryset.filter.return_value = project_queryset
        project_queryset.distinct.return_value = project_queryset

        payment_plan = SimpleNamespace(benefit_plan=SimpleNamespace(id="benefit-plan-id"))
        PayrollService(None)._select_beneficiary_based_on_criteria({
            "json_ext": {"filter_criteria": {"project_ids": ["project-id"]}},
        }, payment_plan)

        initial_queryset.filter.assert_called_once_with(
            individual__groupindividuals__recipient_type="PRIMARY",
            individual__groupindividuals__is_deleted=False,
        )
        primary_queryset.filter.assert_called_once_with(
            project_enrollments__project__id__in=["project-id"],
            project_enrollments__is_deleted=False,
        )

    @patch("payroll.services.BenefitConsumption.objects.filter")
    def test_reconciliation_export_uses_primary_recipients_only(self, benefit_filter):
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
            individual__groupindividuals__recipient_type="PRIMARY",
            individual__groupindividuals__is_deleted=False,
        )
