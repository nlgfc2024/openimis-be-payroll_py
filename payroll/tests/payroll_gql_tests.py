import json
import uuid
from datetime import datetime, timedelta

from graphene import Schema
from graphene.test import Client
from django.contrib.contenttypes.models import ContentType

from contribution_plan.models import PaymentPlan
from individual.models import Individual
from individual.tests.data import service_add_individual_payload
from invoice.models import Bill
from payment_cycle.models import PaymentCycle
from payroll.models import Payroll, PayrollBill, PayrollStatus, PayrollBenefitConsumption
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
from location.test_helpers import create_basic_test_locations


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

        cls.user = LogInHelper().get_or_create_user_api(username='username_authorized', roles=[cls.authorized_role.id])
        cls.user_unauthorized = LogInHelper().get_or_create_user_api(username='username_unauthorized', roles=[cls.unauthorized_role.id])
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
