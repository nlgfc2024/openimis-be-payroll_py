from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from payroll.services import CsvReconciliationService, PayrollService


class PrimaryRecipientFilterTests(SimpleTestCase):
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
