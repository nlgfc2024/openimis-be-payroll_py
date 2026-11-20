"""Who training payrolls select and pay: households through their PRIMARY recipient
or verified primary worker, and individuals who belong to no household."""
import unittest
from types import SimpleNamespace

from django.test import TestCase

from core.test_helpers import LogInHelper
from individual.models import Group, GroupIndividual
from project_social_protection.models import (
    BeneficiaryProjectEnrollment,
    GroupBeneficiaryProjectEnrollment,
    Project,
    ProjectStatus,
)
from project_social_protection.tests.test_helpers import create_project
from social_protection.models import Beneficiary, BeneficiaryStatus, GroupBeneficiary
from social_protection.tests.test_helpers import (
    add_individual_to_group,
    create_benefit_plan,
    create_group,
    create_individual,
)

from payroll.services import PayrollService

HAS_PROJECT_TYPE = any(field.name == "project_type" for field in Project._meta.get_fields())


def payment_plan(benefit_plan):
    return SimpleNamespace(benefit_plan=benefit_plan)


def project_payroll(*projects):
    return {"json_ext": {"filter_criteria": {"project_ids": [str(project.id) for project in projects]}}}


class GroupPayeeSelectionTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = LogInHelper().get_or_create_user_api()
        cls.benefit_plan = create_benefit_plan(
            cls.user.username, {'code': 'PRTPGRP', 'type': 'GROUP', 'max_beneficiaries': None})
        cls.work = create_project('Payee work project', cls.benefit_plan, cls.user.username,
                                  status=ProjectStatus.COMPLETED)
        cls.running_work = create_project('Payee running work project', cls.benefit_plan, cls.user.username,
                                          status=ProjectStatus.IN_PROGRESS)

    @classmethod
    def household(cls, code, projects, primary_recipient=True, primary_workers=1, verified=True):
        """A household with a head (the PRIMARY recipient when asked) and workers."""
        group = create_group(cls.user.username, {'code': code})
        members = []
        for position in range(max(primary_workers, 1) + 1):
            individual = create_individual(cls.user.username, {'first_name': f'{code}-{position}'})
            members.append(add_individual_to_group(cls.user.username, individual, group, is_head=position == 0))
        # Updated in SQL: saving a member rewrites the household's json_ext, and
        # saving a head makes it the PRIMARY recipient.
        GroupIndividual.objects.filter(id=members[0].id).update(
            recipient_type="PRIMARY" if primary_recipient else None)
        GroupIndividual.objects.filter(id__in=[m.id for m in members[1:primary_workers + 1]]).update(
            json_ext={"primary_worker": True})
        Group.objects.filter(id=group.id).update(
            json_ext={"validation_status": "VERIFIED" if verified else "PENDING"})
        household = GroupBeneficiary(group=group, benefit_plan=cls.benefit_plan, status=BeneficiaryStatus.ACTIVE)
        household.save(user=cls.user)
        for project in projects:
            GroupBeneficiaryProjectEnrollment(group_beneficiary=household, project=project).save(user=cls.user)
        return household, members

    def selected(self, *projects):
        enrollments = PayrollService(self.user)._select_group_enrollments_based_on_criteria(
            project_payroll(*projects), payment_plan(self.benefit_plan))
        return set(enrollments.values_list("group_beneficiary_id", flat=True))

    def test_by_default_households_are_selected_through_their_primary_recipient(self):
        paid, _ = self.household('PRTP-A', [self.work])
        no_recipient, _ = self.household('PRTP-B', [self.work], primary_recipient=False)
        self.assertEqual(self.selected(self.work), {paid.id})
        self.assertNotIn(no_recipient.id, self.selected(self.work))

    def test_work_projects_must_be_completed(self):
        household, _ = self.household('PRTP-C', [self.running_work])
        self.assertEqual(self.selected(self.running_work), set())
        self.assertNotIn(household.id, self.selected(self.running_work))

    @unittest.skipUnless(HAS_PROJECT_TYPE, "needs Project.project_type from the project module")
    def test_trainings_are_selected_whatever_their_status(self):
        training = create_project('Payee running training', self.benefit_plan, self.user.username,
                                  activity=None, working_days=3, project_type='TRAINING',
                                  status=ProjectStatus.IN_PROGRESS)
        household, _ = self.household('PRTP-H', [training])
        self.assertEqual(self.selected(training), {household.id})


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
