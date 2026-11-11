import json
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from graphene import Schema
from graphene.test import Client
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError

from contribution_plan.models import PaymentPlan
from individual.models import Group, GroupIndividual, Individual
from individual.tests.data import service_add_individual_payload
from invoice.models import Bill
from payment_cycle.models import PaymentCycle
from payroll.models import (
    BenefitAttachment,
    BenefitConsumption,
    BenefitConsumptionStatus,
    ConsolidatedPayrollMember,
    Payroll,
    PayrollBenefitConsumption,
    PayrollBill,
    PayrollStatus,
)
from payroll.consolidation import payrolls_in_district
from payroll.services import PayrollService
from payroll.strategies.strategy_of_payments_interface import StrategyOfPaymentInterface
from tasks_management.models import Task
from payroll.tests.data import gql_payroll_create, gql_payroll_query, gql_payroll_delete, \
    gql_payroll_create_no_json_ext
from payroll.tests.helpers import PaymentPointHelper
from core.test_helpers import LogInHelper, create_test_role
from core.models.openimis_graphql_test_case import openIMISGraphQLTestCase, BaseTestContext
from payroll.schema import Query, Mutation
from location.test_helpers import create_test_location
from social_protection.models import BenefitPlan, Beneficiary, BeneficiaryStatus
from project_social_protection.models import BeneficiaryProjectEnrollment, ProjectStatus
from social_protection.tests.data import service_add_payload
from project_social_protection.tests.test_helpers import create_project
from location.test_helpers import assign_user_districts, create_basic_test_locations


class PayrollGQLTestCase(openIMISGraphQLTestCase):

    user = None
    user_unauthorized = None
    gql_client = None
    gql_context = None
    gql_context_unauthorized = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        # Create test locations
        create_basic_test_locations()

        # Create test roles
        authorized_perms = [
            "gql_payroll_search_perms",
            "gql_payroll_create_perms",
            "gql_payroll_delete_perms",
            "gql_csv_reconciliation_search_perms",
        ]
        cls.authorized_role = create_test_role(perm_names=authorized_perms, name="PayrollAuthorizedRole")

        unauthorized_perms = []  # No permissions for unauthorized user
        cls.unauthorized_role = create_test_role(perm_names=unauthorized_perms, name="PayrollUnauthorizedRole")

        cls.user = LogInHelper().get_or_create_user_api(
            username='username_authorized',
            roles=[cls.authorized_role.id],
            password='PayrollTestPassword1!',
        )
        cls.user_unauthorized = LogInHelper().get_or_create_user_api(
            username='username_unauthorized',
            roles=[cls.unauthorized_role.id],
            password='PayrollTestPassword1!',
        )
        gql_schema = Schema(
            query=Query,
            mutation=Mutation
        )
        cls.gql_client = Client(gql_schema)
        cls.gql_context = BaseTestContext(cls.user)
        cls.gql_context_unauthorized = BaseTestContext(cls.user_unauthorized)
        cls.name = "TestCreatePayroll"
        cls.status = PayrollStatus.PENDING_APPROVAL
        cls.payment_method = "TestPaymentMethod"
        cls.date_valid_from, cls.date_valid_to = cls.__get_start_and_end_of_current_month()
        cls.payment_point = PaymentPointHelper().get_or_create_payment_point_api()
        cls.benefit_plan = cls.__create_benefit_plan()

        cls.region = create_test_location("R", custom_props={"code": "REG-PAYROLL-01"})
        cls.district = create_test_location("D", custom_props={"code": "DIST-PAYROLL-01", "parent": cls.region})
        assign_user_districts(cls.user, ["DIST-PAYROLL-01"])
        cls.ward = create_test_location("W", custom_props={"code": "WARD-PAYROLL-01", "parent": cls.district})
        cls.village = create_test_location("V", custom_props={"code": "VILL-PAYROLL-01", "parent": cls.ward})
        cls.other_location = create_test_location("V", custom_props={"code": "VILL-OTHER-01"})

        cls.project_1 = create_project(
            "Payroll Project 1", cls.benefit_plan, cls.user.username,
            allows_multiple_enrollments=True, status=ProjectStatus.COMPLETED,
        )
        cls.project_2 = create_project(
            "Payroll Project 2", cls.benefit_plan, cls.user.username,
            allows_multiple_enrollments=True, status=ProjectStatus.COMPLETED,
        )
        # Both projects allow multiple enrollments to support test_create_with_multi_project_enrollment_no_duplicates

        cls.individual = cls.__create_individual(location=None, able_bodied=True)
        cls.individual_2 = cls.__create_individual(location=cls.village, able_bodied=False)
        cls.individual_3 = cls.__create_individual(location=cls.other_location, able_bodied=True)

        cls.subject_type = ContentType.objects.get_for_model(Beneficiary)
        cls.payment_plan = cls.__create_payment_plan(cls.benefit_plan)
        cls.payment_cycle = cls.__create_payment_cycle()
        cls.beneficiary = cls.__create_beneficiary(cls.individual, cls.project_1)
        cls.beneficiary_2 = cls.__create_beneficiary(cls.individual_2, cls.project_2)
        cls.beneficiary_3 = cls.__create_beneficiary(cls.individual_3, cls.project_1)
        cls.bill = cls.__create_bill()
        cls.json_ext_able_bodied_false = """{"advanced_criteria": [{"custom_filter_condition": "able_bodied__boolean=False"}]}"""
        cls.json_ext_able_bodied_true = """{"advanced_criteria": [{"custom_filter_condition": "able_bodied__boolean=True"}]}"""
        cls.includedUnpaid = False

    def setup(self):
        payroll = self.payroll_from_db()
        if payroll:
            self.delete_payroll_and_check_bill(payroll)
        temp_payroll = self.payroll_from_db(f"{self.name}-tmp")
        if temp_payroll:
            self.delete_payroll_and_check_bill(temp_payroll)

    def payroll_from_db(self, name=None):
        return Payroll.objects.filter(
            name=(name or self.name),
            payment_plan_id=self.payment_plan.id,
            payment_cycle_id=self.payment_cycle.id,
            payment_point_id=self.payment_point.id,
            payment_method=self.payment_method,
            status=self.status,
            date_valid_from=self.date_valid_from,
            date_valid_to=self.date_valid_to,
            is_deleted=False,
        ).first()

    def test_query(self):
        output = self.gql_client.execute(gql_payroll_query, context=self.gql_context.get_request())
        result = output.get('data', {}).get('payroll', {})
        self.assertTrue(result)

    def test_query_unauthorized(self):
        output = self.gql_client.execute(gql_payroll_query, context=self.gql_context_unauthorized.get_request())
        error = next(iter(output.get('errors', [])), {}).get('message', None)
        self.assertTrue(error)

    def create_payroll(self, name, json_ext):
        variables = {
            "name": name,
            "paymentCycleId": str(self.payment_cycle.id),
            "paymentPlanId": str(self.payment_plan.id),
            "paymentPointId": str(self.payment_point.id),
            "paymentMethod": self.payment_method,
            "status": self.status,
            "dateValidFrom": self.date_valid_from,
            "dateValidTo": self.date_valid_to,
            "jsonExt": json_ext,
            "clientMutationId": str(uuid.uuid4())
        }
        output = self.gql_client.execute(gql_payroll_create, context=self.gql_context.get_request(), variable_values=variables)
        self.assertEqual(output.get('errors'), None)

        return self.payroll_from_db(name)

    def create_payroll_no_json_ext(self, name):
        payload = gql_payroll_create_no_json_ext % (
            name,
            self.payment_cycle.id,
            self.payment_plan.id,
            self.payment_point.id,
            self.payment_method,
            self.status,
            self.date_valid_from,
            self.date_valid_to,
        )
        output = self.gql_client.execute(payload, context=self.gql_context.get_request())
        self.assertEqual(output.get('errors'), None)

        return self.payroll_from_db(name)

    def delete_payroll_and_check_bill(self, payroll):
        # payroll_bill = PayrollBill.objects.filter(
        #     bill=self.bill, payroll=payroll.first())
        # self.assertEqual(payroll_bill.count(), 1)
        # payroll_bill.delete()
        payroll.delete(username='username_authorized')
        # self.assertEqual(PayrollBill.objects.all().count(), 0)
        # FIXME
        # self.assertIsNone(payroll)

    def test_create_no_advanced_criteria(self):
        payroll = self.create_payroll_no_json_ext(self.name)
        self.assertIsNotNone(payroll)

    def test_create_full(self):
        payroll = self.create_payroll(self.name, self.json_ext_able_bodied_true)
        self.assertIsNotNone(payroll)

    def test_create_with_project_filter(self):
        json_ext = json.dumps({
            "filter_criteria": {
                "project_ids": [str(self.project_1.id)]
            }
        })
        payroll = self.create_payroll(f"{self.name}_project", json_ext)
        self.assertIsNotNone(payroll)

        payroll_benefits = PayrollBenefitConsumption.objects.filter(
            payroll=payroll,
            is_deleted=False
        )
        individual_ids = set(pb.benefit.individual_id for pb in payroll_benefits if pb.benefit)
        beneficiaries = Beneficiary.objects.filter(
            individual_id__in=individual_ids,
            benefit_plan=self.benefit_plan
        )
        beneficiary_ids = set(b.id for b in beneficiaries)

        self.assertIn(self.beneficiary.id, beneficiary_ids)
        self.assertIn(self.beneficiary_3.id, beneficiary_ids)
        self.assertNotIn(self.beneficiary_2.id, beneficiary_ids)

    def test_create_with_location_filter(self):
        json_ext = json.dumps({
            "filter_criteria": {
                "location_ids": [str(self.district.uuid)]
            }
        })
        payroll = self.create_payroll(f"{self.name}_location", json_ext)
        self.assertIsNotNone(payroll)

        payroll_benefits = PayrollBenefitConsumption.objects.filter(
            payroll=payroll,
            is_deleted=False
        )
        individual_ids = set(pb.benefit.individual_id for pb in payroll_benefits if pb.benefit)
        beneficiaries = Beneficiary.objects.filter(
            individual_id__in=individual_ids,
            benefit_plan=self.benefit_plan
        )
        beneficiary_ids = set(b.id for b in beneficiaries)
        self.assertNotIn(self.beneficiary.id, beneficiary_ids)
        self.assertIn(self.beneficiary_2.id, beneficiary_ids)
        self.assertNotIn(self.beneficiary_3.id, beneficiary_ids)

    def test_create_with_location_hierarchy_filter(self):
        json_ext = json.dumps({
            "filter_criteria": {
                "location_ids": [str(self.ward.uuid)]
            }
        })
        payroll = self.create_payroll(f"{self.name}_location_hierarchy", json_ext)
        self.assertIsNotNone(payroll)

        payroll_benefits = PayrollBenefitConsumption.objects.filter(
            payroll=payroll,
            is_deleted=False
        )
        individual_ids = set(pb.benefit.individual_id for pb in payroll_benefits if pb.benefit)
        beneficiaries = Beneficiary.objects.filter(
            individual_id__in=individual_ids,
            benefit_plan=self.benefit_plan
        )
        beneficiary_ids = set(b.id for b in beneficiaries)
        self.assertNotIn(self.beneficiary.id, beneficiary_ids)
        self.assertIn(self.beneficiary_2.id, beneficiary_ids)
        self.assertNotIn(self.beneficiary_3.id, beneficiary_ids)

    def test_create_with_combined_project_and_location(self):
        json_ext = json.dumps({
            "filter_criteria": {
                "project_ids": [str(self.project_1.id)],
                "location_ids": [str(self.other_location.uuid)]
            }
        })
        payroll = self.create_payroll(f"{self.name}_combined", json_ext)
        self.assertIsNotNone(payroll)

        payroll_benefits = PayrollBenefitConsumption.objects.filter(
            payroll=payroll,
            is_deleted=False
        )
        individual_ids = set(pb.benefit.individual_id for pb in payroll_benefits if pb.benefit)
        beneficiaries = Beneficiary.objects.filter(
            individual_id__in=individual_ids,
            benefit_plan=self.benefit_plan
        )
        beneficiary_ids = set(b.id for b in beneficiaries)
        self.assertIn(self.beneficiary_3.id, beneficiary_ids)
        self.assertNotIn(self.beneficiary.id, beneficiary_ids)
        self.assertNotIn(self.beneficiary_2.id, beneficiary_ids)

    def test_create_with_project_and_advanced_criteria(self):
        json_ext = json.dumps({
            "filter_criteria": {
                "project_ids": [str(self.project_1.id), str(self.project_2.id)]
            },
            "advanced_criteria": [
                {"custom_filter_condition": "able_bodied__boolean=True"}
            ]
        })
        payroll = self.create_payroll(f"{self.name}_project_advanced", json_ext)
        self.assertIsNotNone(payroll)

        payroll_benefits = PayrollBenefitConsumption.objects.filter(
            payroll=payroll,
            is_deleted=False
        )
        individual_ids = set(pb.benefit.individual_id for pb in payroll_benefits if pb.benefit)
        beneficiaries = Beneficiary.objects.filter(
            individual_id__in=individual_ids,
            benefit_plan=self.benefit_plan
        )
        beneficiary_ids = set(b.id for b in beneficiaries)
        self.assertIn(self.beneficiary.id, beneficiary_ids)
        self.assertIn(self.beneficiary_3.id, beneficiary_ids)
        self.assertNotIn(self.beneficiary_2.id, beneficiary_ids)

    def test_create_with_empty_project_ids(self):
        json_ext = json.dumps({
            "filter_criteria": {
                "project_ids": []
            }
        })
        payroll = self.create_payroll(f"{self.name}_empty_project", json_ext)
        self.assertIsNotNone(payroll)

        payroll_benefits = PayrollBenefitConsumption.objects.filter(
            payroll=payroll,
            is_deleted=False
        )
        self.assertGreater(payroll_benefits.count(), 0)

    def test_create_fail_due_to_empty_name(self):
        payroll = self.create_payroll("", self.json_ext_able_bodied_true)
        self.assertIsNone(payroll)

    def test_create_with_multi_project_enrollment_no_duplicates(self):
        """Regression test: beneficiary enrolled in multiple filtered projects should not be duplicated."""
        # Create a beneficiary enrolled in BOTH project_1 AND project_2
        multi_enrolled_individual = self.__create_individual(able_bodied=True)
        multi_enrolled_beneficiary = Beneficiary(
            individual=multi_enrolled_individual,
            benefit_plan=self.benefit_plan,
            json_ext=multi_enrolled_individual.json_ext,
            status=BeneficiaryStatus.ACTIVE,
        )
        multi_enrolled_beneficiary.save(username=self.user.username)

        enrollment_1 = BeneficiaryProjectEnrollment(
            beneficiary=multi_enrolled_beneficiary,
            project=self.project_1,
        )
        enrollment_1.save(username=self.user.username)
        enrollment_2 = BeneficiaryProjectEnrollment(
            beneficiary=multi_enrolled_beneficiary,
            project=self.project_2,
        )
        enrollment_2.save(username=self.user.username)

        # Filter by both projects - without distinct() this would duplicate the beneficiary
        json_ext = json.dumps({
            "filter_criteria": {
                "project_ids": [str(self.project_1.id), str(self.project_2.id)]
            }
        })
        payroll = self.create_payroll(f"{self.name}_multi_enroll", json_ext)
        self.assertIsNotNone(payroll)

        # Count benefits for the multi-enrolled beneficiary - should be exactly 1, not 2
        payroll_benefits = PayrollBenefitConsumption.objects.filter(
            payroll=payroll,
            benefit__individual=multi_enrolled_individual,
            is_deleted=False
        )
        self.assertEqual(
            payroll_benefits.count(),
            1,
            "Beneficiary enrolled in multiple filtered projects should only generate one benefit"
        )

    # def test_create_fail_due_to_one_bill_assigment(self):
    #     tmp_name = f"{self.name}-tmp"
    #     payroll_tmp = self.create_payroll(tmp_name, self.json_ext_able_bodied_true)
    #     self.assertTrue(payroll_tmp.exists())

    #     payroll = self.create_payroll(self.name, self.json_ext_able_bodied_true)
    #     self.assertFalse(payroll.exists())

    #     self.delete_payroll_and_check_bill(payroll_tmp)

    def test_create_unauthorized(self):
        variables = {
            "name": self.name,
            "paymentCycleId": str(self.payment_cycle.id),
            "paymentPlanId": str(self.payment_plan.id),
            "paymentMethod": self.payment_method,
            "status": PayrollStatus.PENDING_APPROVAL,
            "dateValidFrom": self.date_valid_from,
            "dateValidTo": self.date_valid_to,
            "jsonExt": self.json_ext_able_bodied_false,
            "clientMutationId": str(uuid.uuid4())
        }

        self.gql_client.execute(
            gql_payroll_create, context=self.gql_context_unauthorized.get_request(), variable_values=variables)
        self.assertFalse(
            Payroll.objects.filter(
                name=self.name,
                payment_plan_id=self.benefit_plan.id,
                payment_cycle_id=self.payment_cycle.id,
                payment_point_id=self.payment_point.id,
                payment_method=self.payment_method,
                status=self.status,
                date_valid_from=self.date_valid_from,
                date_valid_to=self.date_valid_to,
                json_ext=self.json_ext_able_bodied_false,
                is_deleted=False,
            ).exists()
        )

    def test_delete(self):
        payroll = Payroll(name=self.name,
                          payment_plan_id=self.payment_plan.id,
                          payment_point_id=self.payment_point.id,
                          payment_cycle_id=self.payment_cycle.id,
                          payment_method=self.payment_method,
                          status=self.status,
                          date_valid_from=self.date_valid_from,
                          date_valid_to=self.date_valid_to,
                          json_ext=json.loads(self.json_ext_able_bodied_false),
                          )
        payroll.save(user=self.user)
        # payroll_bill = PayrollBill(payroll=payroll, bill=self.bill)
        # payroll_bill.save(user=self.user)
        payload = gql_payroll_delete % json.dumps([str(payroll.id)])
        output = self.gql_client.execute(payload, context=self.gql_context.get_request())
        self.assertEqual(output.get('errors'), None)
        # FIXME self.assertTrue(Payroll.objects.filter(id=payroll.id, is_deleted=True).exists())
        # self.assertEqual(PayrollBill.objects.filter(payroll=payroll, bill=self.bill).count(), 0)

    def test_delete_unauthorized(self):
        payroll = Payroll(name=self.name,
                          payment_plan_id=self.payment_plan.id,
                          payment_cycle_id=self.payment_cycle.id,
                          payment_point_id=self.payment_point.id,
                          payment_method=self.payment_method,
                          status=self.status,
                          date_valid_from=self.date_valid_from,
                          date_valid_to=self.date_valid_to,
                          json_ext=json.loads(self.json_ext_able_bodied_true),
                          )
        payroll.save(user=self.user)
        payroll_bill = PayrollBill(payroll=payroll, bill=self.bill)
        payroll_bill.save(user=self.user)
        payload = gql_payroll_delete % json.dumps([str(payroll.id)])
        self.gql_client.execute(payload, context=self.gql_context_unauthorized.get_request())
        # output = self.gql_client.execute(payload, context=self.gql_context_unauthorized.get_request())
        # FIXME look for delete task instead
        # self.assertTrue(Payroll.objects.filter(id=payroll.id, is_deleted=False).exists())
        # self.assertEqual(PayrollBill.objects.filter(payroll=payroll, bill=self.bill).count(), 1)
        # payroll_bill.delete(username=self.user.username)
        # self.assertTrue(PayrollBill.objects.filter(payroll=payroll, bill=self.bill, is_deleted=True))

    def test_consolidated_creation_validates_selected_sources(self):
        service = PayrollService(self.user)
        source = self._create_consolidation_source("Consolidation validation source")
        invalid_cases = [
            (self.district.uuid, self.benefit_plan.id, []),
            (self.district.uuid, uuid.uuid4(), [source.id]),
            (self.district.uuid, self.benefit_plan.id, [uuid.uuid4()]),
            (self.district.uuid, self.benefit_plan.id, [source.id, source.id]),
        ]
        for district_id, phase_id, source_ids in invalid_cases:
            with self.subTest(source_ids=source_ids):
                with self.assertRaises(ValidationError):
                    service.create_consolidated_payroll(district_id, phase_id, source_ids)

        pending_source = self._create_consolidation_source(
            "Pending consolidation source",
            status=PayrollStatus.PENDING_APPROVAL,
        )
        with self.assertRaises(ValidationError):
            service.create_consolidated_payroll(
                self.district.uuid, self.benefit_plan.id, [pending_source.id],
            )

        wrong_district_source = self._create_consolidation_source(
            "Wrong district source",
            json_ext={"filter_criteria": {"location_ids": [str(self.other_location.id)]}},
        )
        with self.assertRaises(ValidationError):
            service.create_consolidated_payroll(
                self.district.uuid, self.benefit_plan.id, [wrong_district_source.id],
            )

        incompatible_source = self._create_consolidation_source(
            "Incompatible payment source",
            payment_method="another-payment-method",
        )
        with self.assertRaises(ValidationError):
            service.create_consolidated_payroll(
                self.district.uuid,
                self.benefit_plan.id,
                [source.id, incompatible_source.id],
            )

        for field in ("payment_point", "payment_cycle"):
            incompatible_source = self._create_consolidation_source(
                f"Incompatible {field} source",
                **{field: None},
            )
            with self.subTest(incompatible_field=field):
                with self.assertRaises(ValidationError):
                    service.create_consolidated_payroll(
                        self.district.uuid,
                        self.benefit_plan.id,
                        [source.id, incompatible_source.id],
                    )

    def test_consolidated_creation_blocks_duplicate_membership_and_creates_valid_payroll(self):
        source = self._create_consolidation_source("Consolidation source")
        self._attach_benefit(source)
        service = PayrollService(self.user)

        with patch("payroll.services.TaskService") as task_service:
            task_service.return_value.create.return_value = {"success": True}
            consolidated = service.create_consolidated_payroll(
                self.district.uuid, self.benefit_plan.id, [source.id],
            )
        self.assertEqual(consolidated.payroll_type, Payroll.PayrollType.CONSOLIDATED)
        self.assertTrue(ConsolidatedPayrollMember.objects.filter(
            consolidated_payroll=consolidated,
            source_payroll=source,
            is_deleted=False,
        ).exists())
        task_service.return_value.create.assert_called_once()

        second_source = self._create_consolidation_source("Duplicate membership source")
        second_benefit = self._attach_benefit(second_source)
        second_benefit.individual = self.individual_3
        second_benefit.save(user=self.user)
        ConsolidatedPayrollMember(
            consolidated_payroll=consolidated,
            source_payroll=second_source,
        ).save(user=self.user)
        with self.assertRaises(ValidationError):
            service.create_consolidated_payroll(
                self.district.uuid, self.benefit_plan.id, [second_source.id],
            )

    def test_consolidated_creation_rejects_duplicate_recipients(self):
        source_a = self._create_consolidation_source("Duplicate recipient source A")
        source_b = self._create_consolidation_source("Duplicate recipient source B")
        self._attach_benefit(source_a)
        self._attach_benefit(source_b)

        with self.assertRaisesRegex(ValidationError, "same recipient"):
            PayrollService(self.user).create_consolidated_payroll(
                self.district.uuid,
                self.benefit_plan.id,
                [source_a.id, source_b.id],
            )

    def test_consolidated_creation_rejects_recipients_in_prior_consolidations(self):
        source_a = self._create_consolidation_source("Prior consolidation source")
        source_b = self._create_consolidation_source("Second consolidation source")
        self._attach_benefit(source_a)
        self._attach_benefit(source_b)
        service = PayrollService(self.user)

        with patch("payroll.services.TaskService") as task_service:
            task_service.return_value.create.return_value = {"success": True}
            service.create_consolidated_payroll(
                self.district.uuid,
                self.benefit_plan.id,
                [source_a.id],
            )

        with self.assertRaisesRegex(ValidationError, "already included"):
            service.create_consolidated_payroll(
                self.district.uuid,
                self.benefit_plan.id,
                [source_b.id],
            )

    def test_consolidated_creation_rejects_pending_source_tasks(self):
        source = self._create_consolidation_source("Pending task consolidation source")
        self._attach_benefit(source)
        Task(
            source="payroll_delete",
            entity_type=ContentType.objects.get_for_model(Payroll),
            entity_id=str(source.id),
            status=Task.Status.RECEIVED,
        ).save(user=self.user)

        with self.assertRaisesRegex(ValidationError, "pending task"):
            PayrollService(self.user).create_consolidated_payroll(
                self.district.uuid,
                self.benefit_plan.id,
                [source.id],
            )

        with self.subTest(pending_task="benefit"):
            source = self._create_consolidation_source("Pending benefit task source")
            benefit = self._attach_benefit(source)
            Task(
                source="benefit_delete",
                entity_type=ContentType.objects.get_for_model(BenefitConsumption),
                entity_id=str(benefit.id),
                status=Task.Status.RECEIVED,
            ).save(user=self.user)
            with self.assertRaisesRegex(ValidationError, "benefit with pending work"):
                PayrollService(self.user).create_consolidated_payroll(
                    self.district.uuid,
                    self.benefit_plan.id,
                    [source.id],
                )

    def test_consolidated_creation_rejects_pending_deletion_or_reconciled_benefits(self):
        for status in (
            BenefitConsumptionStatus.PENDING_DELETION,
            BenefitConsumptionStatus.RECONCILED,
        ):
            with self.subTest(status=status):
                source = self._create_consolidation_source(
                    f"Ineligible benefit source {status}",
                )
                benefit = self._attach_benefit(source)
                benefit.status = status
                benefit.save(user=self.user)

                with self.assertRaisesRegex(ValidationError, "pending deletion"):
                    PayrollService(self.user).create_consolidated_payroll(
                        self.district.uuid,
                        self.benefit_plan.id,
                        [source.id],
                    )

    def test_consolidated_creation_enforces_district_access(self):
        source = self._create_consolidation_source("Unauthorized district source")
        self._attach_benefit(source)

        with patch("payroll.services.LocationManager") as location_manager:
            location_manager.return_value.is_allowed.return_value = False
            with self.assertRaisesRegex(ValidationError, "not allowed"):
                PayrollService(self.user).create_consolidated_payroll(
                    self.district.uuid,
                    self.benefit_plan.id,
                    [source.id],
                )
        location_manager.return_value.is_allowed.assert_called_once_with(
            self.user,
            [self.district.id],
        )

    def test_consolidated_creation_rolls_back_when_task_creation_fails(self):
        source = self._create_consolidation_source("Task failure consolidation source")
        self._attach_benefit(source)
        service = PayrollService(self.user)
        consolidated_count = Payroll.objects.filter(
            payroll_type=Payroll.PayrollType.CONSOLIDATED,
        ).count()

        with patch("payroll.services.TaskService") as task_service:
            task_service.return_value.create.return_value = {
                "success": False,
                "error": "Task creation failed",
            }
            with self.assertRaisesRegex(ValueError, "Task creation failed"):
                service.create_consolidated_payroll(
                    self.district.uuid, self.benefit_plan.id, [source.id],
                )

        self.assertFalse(ConsolidatedPayrollMember.objects.filter(
            source_payroll=source,
            is_deleted=False,
        ).exists())
        self.assertEqual(
            Payroll.objects.filter(payroll_type=Payroll.PayrollType.CONSOLIDATED).count(),
            consolidated_count,
        )

    def test_rejecting_consolidated_payroll_preserves_shared_benefits_and_bills(self):
        source, consolidated, benefit = self._create_shared_consolidation_benefit()

        StrategyOfPaymentInterface.reject_payroll(consolidated, self.user)

        self.assertTrue(BenefitConsumption.objects.filter(id=benefit.id, is_deleted=False).exists())
        self.assertTrue(BenefitAttachment.objects.filter(
            benefit=benefit, bill=self.bill, is_deleted=False,
        ).exists())
        self.assertTrue(Bill.objects.filter(id=self.bill.id, is_deleted=False).exists())
        self.assertTrue(PayrollBenefitConsumption.objects.filter(
            payroll=source, benefit=benefit, is_deleted=False,
        ).exists())
        self.assertFalse(PayrollBenefitConsumption.objects.filter(
            payroll=consolidated, benefit=benefit, is_deleted=False,
        ).exists())
        self.assertFalse(ConsolidatedPayrollMember.objects.filter(
            consolidated_payroll=consolidated, is_deleted=False,
        ).exists())

    def test_deleting_consolidated_payroll_preserves_shared_benefits_and_bills(self):
        source, consolidated, benefit = self._create_shared_consolidation_benefit()

        StrategyOfPaymentInterface.remove_benefits_from_rejected_payroll(
            consolidated, user=self.user,
        )
        consolidated.delete(user=self.user)

        self.assertTrue(BenefitConsumption.objects.filter(id=benefit.id, is_deleted=False).exists())
        self.assertTrue(BenefitAttachment.objects.filter(
            benefit=benefit, bill=self.bill, is_deleted=False,
        ).exists())
        self.assertTrue(Bill.objects.filter(id=self.bill.id, is_deleted=False).exists())
        self.assertTrue(PayrollBenefitConsumption.objects.filter(
            payroll=source, benefit=benefit, is_deleted=False,
        ).exists())

    def test_rejecting_approved_consolidated_payroll_preserves_payment_state(self):
        source, consolidated, benefit = self._create_shared_consolidation_benefit()
        benefit.status = BenefitConsumptionStatus.RECONCILED
        benefit.receipt = "source-payment-receipt"
        benefit.save(user=self.user)

        with patch("payroll.services.PayrollService.create_accept_payroll_task"):
            StrategyOfPaymentInterface.reject_approved_payroll(consolidated, self.user)

        benefit.refresh_from_db()
        consolidated.refresh_from_db()
        self.assertEqual(benefit.status, BenefitConsumptionStatus.RECONCILED)
        self.assertEqual(benefit.receipt, "source-payment-receipt")
        self.assertEqual(consolidated.status, PayrollStatus.PENDING_APPROVAL)
        self.assertTrue(PayrollBenefitConsumption.objects.filter(
            payroll=source, benefit=benefit, is_deleted=False,
        ).exists())
        self.assertTrue(BenefitAttachment.objects.filter(
            benefit=benefit, bill=self.bill, is_deleted=False,
        ).exists())

    def test_active_consolidation_blocks_source_payroll_actions(self):
        source = self._create_consolidation_source("Reserved source")
        consolidated = Payroll(
            name="Reserved consolidated payroll",
            payment_plan=self.payment_plan,
            payment_cycle=self.payment_cycle,
            payment_point=self.payment_point,
            payment_method=self.payment_method,
            status=PayrollStatus.PENDING_APPROVAL,
            payroll_type=Payroll.PayrollType.CONSOLIDATED,
            district=self.district,
        )
        consolidated.save(user=self.user)
        ConsolidatedPayrollMember(
            consolidated_payroll=consolidated,
            source_payroll=source,
        ).save(user=self.user)
        service = PayrollService(self.user)
        actions = (
            lambda: service.delete({"id": source.id}),
            lambda: service.close_payroll({"id": source.id}),
            lambda: service.reject_approved_payroll({"id": source.id}),
            lambda: service.make_payment_for_payroll({"id": source.id}),
        )
        for action in actions:
            with self.subTest(action=action):
                with self.assertRaises(ValidationError):
                    action()

    def test_recovery_move_blocks_active_consolidated_source(self):
        source, consolidated, benefit = self._create_shared_consolidation_benefit()
        destination = self._create_consolidation_source("Recovery destination")
        benefit.status = BenefitConsumptionStatus.APPROVE_FOR_PAYMENT
        benefit.save(user=self.user)

        with self.assertRaises(ValidationError):
            PayrollService(self.user)._move_benefit_consumptions(destination, source.id)

        self.assertTrue(PayrollBenefitConsumption.objects.filter(
            payroll=source, benefit=benefit, is_deleted=False,
        ).exists())
        self.assertTrue(PayrollBenefitConsumption.objects.filter(
            payroll=consolidated, benefit=benefit, is_deleted=False,
        ).exists())
        benefit.refresh_from_db()
        self.assertEqual(benefit.status, BenefitConsumptionStatus.APPROVE_FOR_PAYMENT)

    def test_recovery_move_blocks_consolidated_parent(self):
        source, consolidated, benefit = self._create_shared_consolidation_benefit()
        destination = self._create_consolidation_source("Recovery destination from parent")

        with self.assertRaises(ValidationError):
            PayrollService(self.user)._move_benefit_consumptions(destination, consolidated.id)

        self.assertTrue(PayrollBenefitConsumption.objects.filter(
            payroll=source, benefit=benefit, is_deleted=False,
        ).exists())
        self.assertTrue(PayrollBenefitConsumption.objects.filter(
            payroll=consolidated, benefit=benefit, is_deleted=False,
        ).exists())

    def test_district_fallback_only_applies_without_saved_criteria(self):
        legacy_source = self._create_consolidation_source(
            "Legacy district source",
            json_ext={},
        )
        self._attach_benefit(legacy_source)
        criteria_source = self._create_consolidation_source(
            "Saved criteria source",
            json_ext={"filter_criteria": {"location_ids": [str(self.other_location.id)]}},
        )
        self._attach_benefit(criteria_source)
        uuid_criteria_source = self._create_consolidation_source(
            "UUID criteria source",
            json_ext={"filter_criteria": {"location_ids": [str(self.district.uuid)]}},
        )

        matches = payrolls_in_district(
            [legacy_source, criteria_source, uuid_criteria_source],
            self.district.id,
        )

        self.assertEqual(
            [payroll.id for payroll in matches],
            [legacy_source.id, uuid_criteria_source.id],
        )

    def _create_consolidation_source(self, name, **overrides):
        fields = {
            "name": name,
            "payment_plan": self.payment_plan,
            "payment_cycle": self.payment_cycle,
            "payment_point": self.payment_point,
            "payment_method": self.payment_method,
            "status": PayrollStatus.APPROVE_FOR_PAYMENT,
            "json_ext": {
                "filter_criteria": {"location_ids": [str(self.district.id)]},
            },
        }
        fields.update(overrides)
        source = Payroll(**fields)
        source.save(user=self.user)
        return source

    def _attach_benefit(self, payroll):
        benefit = BenefitConsumption(
            individual=self.individual_2,
            code=str(uuid.uuid4()),
            date_due=date(2026, 1, 1),
            amount=Decimal("100"),
            type="Cash Transfer",
            status=BenefitConsumptionStatus.ACCEPTED,
        )
        benefit.save(user=self.user)
        PayrollBenefitConsumption(payroll=payroll, benefit=benefit).save(user=self.user)
        return benefit

    def _create_shared_consolidation_benefit(self):
        source = self._create_consolidation_source("Shared benefit source")
        consolidated = Payroll(
            name="Shared benefit consolidated payroll",
            payment_plan=self.payment_plan,
            payment_cycle=self.payment_cycle,
            payment_point=self.payment_point,
            payment_method=self.payment_method,
            status=PayrollStatus.APPROVE_FOR_PAYMENT,
            payroll_type=Payroll.PayrollType.CONSOLIDATED,
            district=self.district,
        )
        consolidated.save(user=self.user)
        ConsolidatedPayrollMember(
            consolidated_payroll=consolidated,
            source_payroll=source,
        ).save(user=self.user)
        benefit = self._attach_benefit(source)
        PayrollBenefitConsumption(payroll=consolidated, benefit=benefit).save(user=self.user)
        BenefitAttachment(benefit=benefit, bill=self.bill).save(user=self.user)
        return source, consolidated, benefit

    @classmethod
    def __create_benefit_plan(cls):
        object_data = {
            **service_add_payload
        }

        benefit_plan = BenefitPlan(**object_data)
        benefit_plan.save(username=cls.user.username)

        return benefit_plan

    @classmethod
    def __create_payment_plan(cls, benefit_plan):
        object_data = {
            'is_deleted': False,
            'code': "PP-E",
            'name': "Example Payment Plan",
            'benefit_plan': benefit_plan,
            'periodicity': 1,
            'calculation': "32d96b58-898a-460a-b357-5fd4b95cd87c",
            'json_ext': {
                'calculation_rule': {
                    'fixed_batch': 2,
                    'limit_per_single_transaction': 100
                }
            },
        }

        payment_plan = PaymentPlan(**object_data)
        payment_plan.save(username=cls.user.username)
        return payment_plan

    @classmethod
    def __create_payment_cycle(cls):
        pc = PaymentCycle(
            start_date='2023-02-01',
            end_date='2023-03-01',
            type=ContentType.objects.get_for_model(BenefitPlan),
            code=str(datetime.now()))
        pc.save(username=cls.user.username)
        return pc

    @classmethod
    def __create_individual(cls, location=None, able_bodied=True):
        object_data = {
            **service_add_individual_payload,
        }

        if location:
            object_data['location'] = location

        individual = Individual(**object_data)
        individual.json_ext = {"able_bodied": able_bodied}
        individual.save(username=cls.user.username)
        group = Group(code=str(uuid.uuid4()), location=location)
        group.save(username=cls.user.username)
        GroupIndividual(
            group=group,
            individual=individual,
            recipient_type=GroupIndividual.RecipientType.PRIMARY,
        ).save(username=cls.user.username)
        return individual

    @classmethod
    def __create_beneficiary(cls, individual, project):
        object_data = {
            "individual": individual,
            "benefit_plan": cls.benefit_plan,
            "json_ext": individual.json_ext,
            "status": BeneficiaryStatus.ACTIVE,
        }
        beneficiary = Beneficiary(**object_data)
        beneficiary.save(username=cls.user.username)

        enrollment = BeneficiaryProjectEnrollment(beneficiary=beneficiary, project=project)
        enrollment.save(username=cls.user.username)

        return beneficiary

    @classmethod
    def __create_bill(cls):
        object_data = {
            "subject_type": cls.subject_type,
            "subject_id": cls.beneficiary.id,
            "status": Bill.Status.VALIDATED,
            "code": str(datetime.now())
        }
        bill = Bill(**object_data)
        bill.save(username=cls.user.username)
        return bill

    @classmethod
    def __get_start_and_end_of_current_month(cls):
        today = datetime.today()
        # Set date_valid_from to the beginning of the current month
        date_valid_from = today.replace(day=1).strftime("%Y-%m-%d")

        # Calculate the last day of the current month
        next_month = today.replace(day=28) + timedelta(days=4)  # Adding 4 days to ensure we move to the next month
        date_valid_to = (next_month - timedelta(days=next_month.day)).strftime("%Y-%m-%d")

        return date_valid_from, date_valid_to
