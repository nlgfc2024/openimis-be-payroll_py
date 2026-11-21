from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from graphene import Schema
from graphene.test import Client
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied
from django.test import TestCase, override_settings

from contribution_plan.models import PaymentPlan
from core.test_helpers import create_test_technical_user
from individual.models import Individual
from individual.tests.data import service_add_individual_payload
from invoice.models import Bill
from payroll.benefit_plan_access import filter_payroll_queryset, get_authorized_payroll
from payroll.gql_queries import (
    BenefitAttachmentGQLType,
    BenefitConsumptionGQLType,
    CsvReconciliationUploadGQLType,
    PayrollGQLType,
)
from payroll.gql_mutations import DeletePayrollMutation
from payroll.models import (
    BenefitConsumption,
    BenefitAttachment,
    CsvReconciliationUpload,
    Payroll,
    PayrollBenefitConsumption,
)
from payroll.schema import Query
from payroll.services import CsvReconciliationService, PayrollService
from social_protection.apps import SocialProtectionConfig
from social_protection.models import BenefitPlan, UserBenefitPlanAccess


@override_settings(ROW_SECURITY=False)
class PayrollBenefitPlanAccessTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_technical_user(username="track07-payroll-user")
        cls.superuser = create_test_technical_user(
            username="track07-payroll-superuser", super_user=True,
        )
        cls.upg = BenefitPlan(code="UPG070001", name="UPG Track 07")
        cls.upg.save(user=cls.user)
        cls.rmep = BenefitPlan(code="RMEP07001", name="RMEP Track 07")
        cls.rmep.save(user=cls.user)
        content_type = ContentType.objects.get_for_model(BenefitPlan)
        cls.upg_payment_plan = PaymentPlan(
            code="PP-UPG-07", name="UPG payment plan", periodicity=1,
            calculation=uuid4(), benefit_plan_type=content_type,
            benefit_plan_id=str(cls.upg.id),
        )
        cls.upg_payment_plan.save(user=cls.user)
        cls.rmep_payment_plan = PaymentPlan(
            code="PP-RMEP-07", name="RMEP payment plan", periodicity=1,
            calculation=uuid4(), benefit_plan_type=content_type,
            benefit_plan_id=str(cls.rmep.id),
        )
        cls.rmep_payment_plan.save(user=cls.user)
        cls.upg_payroll = Payroll(name="UPG payroll", payment_plan=cls.upg_payment_plan)
        cls.upg_payroll.save(user=cls.user)
        cls.rmep_payroll = Payroll(name="RMEP payroll", payment_plan=cls.rmep_payment_plan)
        cls.rmep_payroll.save(user=cls.user)
        cls.individual = Individual(**service_add_individual_payload)
        cls.individual.save(user=cls.user)
        cls.upg_benefit = BenefitConsumption(
            individual=cls.individual, code="UPG-BC-07", amount=100,
        )
        cls.upg_benefit.save(user=cls.user)
        cls.rmep_benefit = BenefitConsumption(
            individual=cls.individual, code="RMEP-BC-07", amount=200,
        )
        cls.rmep_benefit.save(user=cls.user)
        PayrollBenefitConsumption(
            payroll=cls.upg_payroll, benefit=cls.upg_benefit,
        ).save(user=cls.user)
        PayrollBenefitConsumption(
            payroll=cls.rmep_payroll, benefit=cls.rmep_benefit,
        ).save(user=cls.user)
        bill = Bill(
            subject_type=ContentType.objects.get_for_model(Individual),
            subject_id=cls.individual.id,
            status=Bill.Status.VALIDATED,
            code="TRACK-07-BILL",
        )
        bill.save(user=cls.user)
        cls.rmep_attachment = BenefitAttachment(
            benefit=cls.rmep_benefit, bill=bill,
        )
        cls.rmep_attachment.save(user=cls.user)
        cls.upg_upload = CsvReconciliationUpload(payroll=cls.upg_payroll)
        cls.upg_upload.save(user=cls.user)
        cls.rmep_upload = CsvReconciliationUpload(payroll=cls.rmep_payroll)
        cls.rmep_upload.save(user=cls.user)

    def setUp(self):
        self.original_enabled = SocialProtectionConfig.enable_benefit_plan_access_control
        self.original_all = SocialProtectionConfig.gql_benefit_plan_all_access_perms
        SocialProtectionConfig.enable_benefit_plan_access_control = True
        SocialProtectionConfig.gql_benefit_plan_all_access_perms = ["160006"]
        UserBenefitPlanAccess.objects.filter(user=self.user).delete()

    def tearDown(self):
        SocialProtectionConfig.enable_benefit_plan_access_control = self.original_enabled
        SocialProtectionConfig.gql_benefit_plan_all_access_perms = self.original_all

    def assign(self, plan):
        assignment = UserBenefitPlanAccess(user=self.user, benefit_plan=plan)
        assignment.save(user=self.user)

    def visible_payrolls(self, user=None):
        return set(filter_payroll_queryset(
            Payroll.objects.filter(id__in=[self.upg_payroll.id, self.rmep_payroll.id]),
            user or self.user,
        ).values_list("id", flat=True))

    def test_upg_only_and_rmep_only_access(self):
        self.assign(self.upg)
        self.assertEqual(self.visible_payrolls(), {self.upg_payroll.id})
        UserBenefitPlanAccess.objects.filter(user=self.user).delete()
        self.assign(self.rmep)
        self.assertEqual(self.visible_payrolls(), {self.rmep_payroll.id})

    def test_graphql_list_is_scoped_to_assigned_programme(self):
        self.assign(self.upg)
        client = Client(Schema(query=Query))
        with patch.object(Query, "_check_permissions"):
            result = client.execute(
                "{ payroll { edges { node { uuid } } } }",
                context_value=SimpleNamespace(user=self.user, headers={}),
            )
        self.assertNotIn("errors", result)
        uuids = {
            edge["node"]["uuid"]
            for edge in result["data"]["payroll"]["edges"]
        }
        self.assertIn(str(self.upg_payroll.id), uuids)
        self.assertNotIn(str(self.rmep_payroll.id), uuids)

    def test_payroll_history_queryset_is_scoped(self):
        self.assign(self.upg)
        history = Payroll.get_queryset(Payroll.history.all(), self.user)
        history_ids = set(history.values_list("id", flat=True))
        self.assertIn(self.upg_payroll.id, history_ids)
        self.assertNotIn(self.rmep_payroll.id, history_ids)

    def test_both_programmes_and_no_assignment(self):
        self.assertEqual(self.visible_payrolls(), set())
        self.assign(self.upg)
        self.assign(self.rmep)
        self.assertEqual(
            self.visible_payrolls(), {self.upg_payroll.id, self.rmep_payroll.id}
        )

    def test_all_plan_permission_and_superuser_are_unrestricted(self):
        with patch.object(self.user, "has_perms", return_value=True):
            self.assertEqual(
                self.visible_payrolls(), {self.upg_payroll.id, self.rmep_payroll.id}
            )
        self.assertEqual(
            self.visible_payrolls(self.superuser),
            {self.upg_payroll.id, self.rmep_payroll.id},
        )

    def test_direct_id_relay_service_and_reconciliation_are_denied(self):
        self.assign(self.upg)
        info = SimpleNamespace(context=SimpleNamespace(user=self.user))
        self.assertIsNone(PayrollGQLType.get_node(info, self.rmep_payroll.id))
        self.assertIsNone(BenefitConsumptionGQLType.get_node(
            info, self.rmep_benefit.id,
        ))
        self.assertIsNone(BenefitAttachmentGQLType.get_node(
            info, self.rmep_attachment.id,
        ))
        self.assertIsNone(CsvReconciliationUploadGQLType.get_node(
            info, self.rmep_upload.id,
        ))
        with self.assertRaises(PermissionDenied):
            get_authorized_payroll(self.user, self.rmep_payroll.id)
        with self.assertRaises(PermissionDenied):
            PayrollService(self.user)._get_payment_plan({
                "payment_plan_id": self.rmep_payment_plan.id,
            })
        with self.assertRaises(PermissionDenied):
            DeletePayrollMutation._mutate(
                self.user, ids=[self.rmep_payroll.id]
            )
        with self.assertRaises(PermissionDenied):
            CsvReconciliationService(self.user)._resolve_payroll(self.rmep_payroll.id)
        with self.assertRaises(PermissionDenied):
            CsvReconciliationService(self.user).download_upload_review(
                self.rmep_upload, b"code\n"
            )

    def test_feature_disabled_preserves_existing_behaviour(self):
        SocialProtectionConfig.enable_benefit_plan_access_control = False
        self.assertEqual(
            self.visible_payrolls(), {self.upg_payroll.id, self.rmep_payroll.id}
        )
