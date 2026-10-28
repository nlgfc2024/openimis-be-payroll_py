from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from payroll.apps import PayrollConfig
from payroll.models import (
    BenefitConsumption,
    BenefitConsumptionStatus,
    CsvReconciliationUpload,
    DistrictWagesheet,
    DistrictWagesheetPayroll,
    DistrictWagesheetStatus,
    Payroll,
    PayrollBenefitConsumption,
    PayrollStatus,
    ReconciliationUploadRow,
)
from payroll.services import CsvReconciliationService, DistrictWagesheetService
from core.test_helpers import LogInHelper
from individual.models import Individual
from individual.tests.data import service_add_individual_payload
from location.test_helpers import create_test_location


class DistrictWagesheetPolicyTests(SimpleTestCase):
    """Unit coverage for the non-database policy used by district wagesheets."""

    def test_financial_year_is_derived_from_payment_cycle_start_date(self):
        original_month = PayrollConfig.financial_year_start_month
        try:
            PayrollConfig.financial_year_start_month = 7
            self.assertEqual(
                DistrictWagesheetService._financial_year(
                    SimpleNamespace(start_date=date(2026, 7, 1)),
                ),
                "2026/27",
            )
            self.assertEqual(
                DistrictWagesheetService._financial_year(
                    SimpleNamespace(start_date=date(2027, 6, 30)),
                ),
                "2026/27",
            )
        finally:
            PayrollConfig.financial_year_start_month = original_month

    def test_wagesheet_has_distinct_district_national_and_psp_stages(self):
        self.assertEqual(DistrictWagesheetStatus.PENDING_DISTRICT_APPROVAL, "PENDING_DISTRICT_APPROVAL")
        self.assertEqual(DistrictWagesheetStatus.PENDING_NATIONAL_APPROVAL, "PENDING_NATIONAL_APPROVAL")
        self.assertEqual(DistrictWagesheetStatus.READY_FOR_PSP, "READY_FOR_PSP")
        self.assertEqual(DistrictWagesheetStatus.SUBMITTED_TO_PSP, "SUBMITTED_TO_PSP")


class DistrictWagesheetReconciliationTests(TestCase):
    """Regression coverage for applying reviewed district PSP feedback."""

    def setUp(self):
        self.user = LogInHelper().get_or_create_user_api(username="district-wagesheet-test-user")
        self.district = create_test_location("D", custom_props={"code": "DIST-WAGESHEET-TEST"})
        self.wagesheet = DistrictWagesheet.objects.create(
            district=self.district,
            benefit_plan_id="phase-wagesheet-test",
            financial_year="2026/27",
            status=DistrictWagesheetStatus.PENDING_REVIEW,
        )

    def _individual(self, suffix):
        individual = Individual(**{
            **service_add_individual_payload,
            "first_name": f"District{suffix}",
            "last_name": "Wagesheet",
        })
        individual.save(username=self.user.login_name)
        return individual

    def _payroll_with_benefits(self, name, benefit_count):
        payroll = Payroll(
            name=name,
            status=PayrollStatus.APPROVE_FOR_PAYMENT,
            payment_method="StrategyOfflinePayment",
        )
        payroll.save(username=self.user.login_name)
        DistrictWagesheetPayroll.objects.create(batch=self.wagesheet, payroll=payroll)
        benefits = []
        for index in range(benefit_count):
            benefit = BenefitConsumption(
                individual=self._individual(f"{name}{index}"),
                code=f"{name}-benefit-{index}",
                amount=Decimal("100.00"),
                status=BenefitConsumptionStatus.ACCEPTED,
            )
            benefit.save(username=self.user.login_name)
            link = PayrollBenefitConsumption(payroll=payroll, benefit=benefit)
            link.save(username=self.user.login_name)
            benefits.append(benefit)
        return payroll, benefits

    def test_district_then_national_approval_makes_wagesheet_ready_for_psp(self):
        """The two approval tasks must gate PSP submission in that order."""
        service = DistrictWagesheetService(self.user)
        self.wagesheet.status = DistrictWagesheetStatus.PENDING_DISTRICT_APPROVAL
        self.wagesheet.save(username=self.user.login_name)

        with patch.object(service, "_create_approval_task") as create_task:
            service.resolve_approval(self.wagesheet.id, "district", approved=True)
            self.wagesheet.refresh_from_db()
            self.assertEqual(
                self.wagesheet.status,
                DistrictWagesheetStatus.PENDING_NATIONAL_APPROVAL,
            )
            create_task.assert_called_once_with(self.wagesheet, "national")

            service.resolve_approval(self.wagesheet.id, "national", approved=True)
            self.wagesheet.refresh_from_db()
            self.assertEqual(self.wagesheet.status, DistrictWagesheetStatus.READY_FOR_PSP)

    def test_wagesheet_name_contains_district_phase_and_financial_year(self):
        wagesheet_name = DistrictWagesheetService._wagesheet_name(
            self.district,
            self.wagesheet.benefit_plan_id,
            self.wagesheet.financial_year,
        )

        self.assertEqual(
            wagesheet_name,
            f"District Wagesheet - {self.district.name} - phase-wagesheet-test - 2026/27",
        )

    def test_approved_feedback_reconciles_only_fully_resolved_source_payrolls(self):
        resolved_payroll, [resolved_benefit] = self._payroll_with_benefits("resolved", 1)
        partial_payroll, partial_benefits = self._payroll_with_benefits("partial", 2)
        upload = CsvReconciliationUpload(
            district_wagesheet=self.wagesheet,
            status=CsvReconciliationUpload.Status.WAITING_FOR_VERIFICATION,
        )
        upload.save(username=self.user.login_name)

        for benefit in (resolved_benefit, partial_benefits[0]):
            row = ReconciliationUploadRow(
                upload=upload,
                benefit=benefit,
                code=benefit.code,
                status=ReconciliationUploadRow.Status.VALID,
                submitted_data={"Paid": "Yes", "receipt": f"PSP-{benefit.code}"},
            )
            row.save(username=self.user.login_name)

        # This is the service called once the reconciliation review task is approved.
        CsvReconciliationService(self.user).apply_upload(upload)

        resolved_benefit.refresh_from_db()
        partial_benefits[0].refresh_from_db()
        partial_benefits[1].refresh_from_db()
        resolved_payroll.refresh_from_db()
        partial_payroll.refresh_from_db()
        self.wagesheet.refresh_from_db()

        self.assertEqual(resolved_benefit.status, BenefitConsumptionStatus.RECONCILED)
        self.assertEqual(partial_benefits[0].status, BenefitConsumptionStatus.RECONCILED)
        self.assertEqual(partial_benefits[1].status, BenefitConsumptionStatus.ACCEPTED)
        self.assertEqual(resolved_payroll.status, PayrollStatus.RECONCILED)
        self.assertEqual(partial_payroll.status, PayrollStatus.APPROVE_FOR_PAYMENT)
        self.assertEqual(self.wagesheet.status, DistrictWagesheetStatus.APPLIED)
