"""Who training payrolls select and pay."""
from types import SimpleNamespace

from django.test import TestCase

from core.test_helpers import LogInHelper
from individual.models import Group, GroupIndividual
from project_social_protection.models import (
    BeneficiaryProjectEnrollment,
    ProjectStatus,
)
from project_social_protection.tests.test_helpers import create_project
from social_protection.models import Beneficiary, BeneficiaryStatus
from social_protection.tests.test_helpers import (
    add_individual_to_group,
    create_benefit_plan,
    create_group,
    create_individual,
)

from payroll.services import PayrollService


def payment_plan(benefit_plan):
    return SimpleNamespace(benefit_plan=benefit_plan)


def project_payroll(*projects):
    return {"json_ext": {"filter_criteria": {"project_ids": [str(project.id) for project in projects]}}}


class IndividualPayeeSelectionTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = LogInHelper().get_or_create_user_api()
        cls.benefit_plan = create_benefit_plan(
            cls.user.username, {'code': 'PRTPIND', 'type': 'INDIVIDUAL', 'max_beneficiaries': None})
        cls.project = create_project('Payee individual project', cls.benefit_plan, cls.user.username,
                                     status=ProjectStatus.COMPLETED)

    @classmethod
    def beneficiary(cls, name, household=None, recipient_type=None, deleted_household=False):
        individual = create_individual(cls.user.username, {'first_name': name})
        if household:
            group = create_group(cls.user.username, {'code': household})
            member = add_individual_to_group(cls.user.username, individual, group, is_head=True)
            GroupIndividual.objects.filter(id=member.id).update(recipient_type=recipient_type)
            if deleted_household:
                Group.objects.filter(id=group.id).update(is_deleted=True)
        beneficiary = Beneficiary(individual=individual, benefit_plan=cls.benefit_plan,
                                  status=BeneficiaryStatus.ACTIVE)
        beneficiary.save(user=cls.user)
        BeneficiaryProjectEnrollment(beneficiary=beneficiary, project=cls.project).save(user=cls.user)
        return beneficiary

    def test_individuals_without_a_household_and_primary_recipients_are_selected(self):
        alone = self.beneficiary('PRTP-Alone')
        recipient = self.beneficiary('PRTP-Recipient', 'PRTP-HH1', recipient_type="PRIMARY")
        member = self.beneficiary('PRTP-Member', 'PRTP-HH2', recipient_type="SECONDARY")
        former = self.beneficiary('PRTP-Former', 'PRTP-HH3', deleted_household=True)

        selected = PayrollService(self.user)._select_beneficiary_based_on_criteria(
            project_payroll(self.project), payment_plan(self.benefit_plan))

        self.assertEqual(set(selected.values_list("id", flat=True)), {alone.id, recipient.id, former.id})
        self.assertNotIn(member.id, set(selected.values_list("id", flat=True)))
