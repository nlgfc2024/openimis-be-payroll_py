from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from graphene import Schema
from graphene.test import Client
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied
from django.test import TestCase, override_settings

from contribution_plan.models import PaymentPlan
from core.test_helpers import (
    create_test_interactive_user,
    create_test_role,
    create_test_technical_user,
)
from individual.models import Individual
from individual.tests.data import service_add_individual_payload
from invoice.models import Bill
from payroll.benefit_plan_access import (
    filter_benefit_queryset,
    filter_payroll_queryset,
    get_authorized_payroll,
    require_payment_plan_access,
    task_ownership_for_payment_plan,
)
from payroll.apps import PayrollConfig
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
from tasks_management.models import Task, TaskExecutor, TaskGroup
from tasks_management.services import TaskService


@override_settings(ROW_SECURITY=False)
class PayrollBenefitPlanAccessTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        role = create_test_role([], name="Track 07 payroll access test role")
        cls.user = create_test_interactive_user(
            username="track07-payroll-user",
            roles=[role.id],
        )
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

    def approval_task(self, entity, event, benefit_plan_id):
        group = TaskGroup(
            code=f"T07-{str(uuid4())[:8]}",
            completion_policy=TaskGroup.TaskGroupCompletionPolicy.ALL,
        )
        group.save(user=self.user)
        TaskExecutor(user=self.user, task_group=group).save(user=self.user)
        task = Task(
            source="track07-before-signal-test",
            entity=entity,
            benefit_plan_id=str(benefit_plan_id),
            benefit_plan_is_shared=False,
            status=Task.Status.ACCEPTED,
            task_group=group,
            business_event=event,
            business_status={},
            data={},
        )
        task.save(user=self.user)
        return task

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

    def test_deleted_authorized_link_cannot_expose_active_unauthorized_benefit(self):
        self.assign(self.upg)
        upg_link = PayrollBenefitConsumption.objects.get(
            payroll=self.upg_payroll,
            benefit=self.upg_benefit,
            is_deleted=False,
        )
        upg_link.delete(user=self.user)
        PayrollBenefitConsumption(
            payroll=self.rmep_payroll,
            benefit=self.upg_benefit,
        ).save(user=self.user)

        visible = filter_benefit_queryset(
            BenefitConsumption.objects.filter(id=self.upg_benefit.id),
            self.user,
        )

        self.assertFalse(visible.exists())

    def test_shared_authorization_failures_are_not_bypassed(self):
        queryset = Payroll.objects.all()
        with patch(
            "payroll.benefit_plan_access.get_accessible_benefit_plan_ids",
            side_effect=PermissionDenied("denied"),
        ), self.assertRaises(PermissionDenied):
            filter_payroll_queryset(queryset, self.user)
        with patch(
            "payroll.benefit_plan_access.require_shared_benefit_plan_access",
            side_effect=PermissionDenied("denied"),
        ), self.assertRaises(PermissionDenied):
            require_payment_plan_access(self.user, self.upg_payment_plan)

    def test_all_plan_permission_and_superuser_are_unrestricted(self):
        with patch.object(self.user, "has_perms", return_value=True):
            self.assertEqual(
                self.visible_payrolls(), {self.upg_payroll.id, self.rmep_payroll.id}
            )
        self.assertEqual(
            self.visible_payrolls(self.superuser),
            {self.upg_payroll.id, self.rmep_payroll.id},
        )

    def test_task_ownership_preserves_plan_and_legacy_product_scope(self):
        self.assertEqual(
            task_ownership_for_payment_plan(self.upg_payment_plan),
            {"benefit_plan_id": str(self.upg.id)},
        )
        legacy = SimpleNamespace(
            benefit_plan_type_id=ContentType.objects.get_for_model(Individual).id,
            benefit_plan_id=str(self.individual.id),
        )
        self.assertEqual(
            task_ownership_for_payment_plan(legacy),
            {"benefit_plan_is_shared": True},
        )

    def test_payroll_approval_producer_sets_explicit_task_ownership(self):
        self.assign(self.upg)
        result = PayrollService(self.user).create_accept_payroll_task(
            self.upg_payroll.id,
            {"phase": "test"},
        )
        self.assertTrue(result["success"], result)
        task = Task.objects.get(id=result["data"]["id"])
        self.assertEqual(task.benefit_plan_id, str(self.upg.id))
        self.assertFalse(task.benefit_plan_is_shared)

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

    def test_before_signal_denies_payroll_approval_without_state_changes(self):
        self.assign(self.upg)
        task = self.approval_task(
            self.rmep_payroll,
            PayrollConfig.payroll_accept_event,
            self.upg.id,
        )
        task_status = task.status
        payroll_status = self.rmep_payroll.status
        task_history_count = Task.history.filter(id=task.id).count()
        payroll_history_count = Payroll.history.filter(
            id=self.rmep_payroll.id,
        ).count()

        with self.assertRaises(PermissionDenied):
            TaskService(self.user).complete_task({"id": task.id})

        task.refresh_from_db()
        self.rmep_payroll.refresh_from_db()
        self.assertEqual(task.status, task_status)
        self.assertEqual(self.rmep_payroll.status, payroll_status)
        self.assertEqual(
            Task.history.filter(id=task.id).count(),
            task_history_count,
        )
        self.assertEqual(
            Payroll.history.filter(id=self.rmep_payroll.id).count(),
            payroll_history_count,
        )

    def test_before_signal_allows_authorized_payroll_approval(self):
        self.assign(self.upg)
        task = self.approval_task(
            self.upg_payroll,
            PayrollConfig.payroll_accept_event,
            self.upg.id,
        )
        strategy = SimpleNamespace(accept_payroll=MagicMock())

        with patch(
            "payroll.signals.PaymentMethodStorage.get_chosen_payment_method",
            return_value=strategy,
        ):
            result = TaskService(self.user).complete_task({"id": task.id})

        self.assertTrue(result["success"], result)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.COMPLETED)
        strategy.accept_payroll.assert_called_once_with(
            self.upg_payroll,
            self.user,
        )

    def test_before_signal_protects_benefit_deletion(self):
        self.assign(self.upg)
        task = self.approval_task(
            self.rmep_benefit,
            PayrollConfig.benefit_delete_event,
            self.upg.id,
        )
        task_status = task.status
        benefit_status = self.rmep_benefit.status
        task_history_count = Task.history.filter(id=task.id).count()
        benefit_history_count = BenefitConsumption.history.filter(
            id=self.rmep_benefit.id,
        ).count()

        with self.assertRaises(PermissionDenied):
            TaskService(self.user).complete_task({"id": task.id})

        task.refresh_from_db()
        self.rmep_benefit.refresh_from_db()
        self.assertEqual(task.status, task_status)
        self.assertEqual(self.rmep_benefit.status, benefit_status)
        self.assertEqual(
            Task.history.filter(id=task.id).count(),
            task_history_count,
        )
        self.assertEqual(
            BenefitConsumption.history.filter(id=self.rmep_benefit.id).count(),
            benefit_history_count,
        )

    def test_before_signal_preserves_feature_off_completion(self):
        SocialProtectionConfig.enable_benefit_plan_access_control = False
        task = self.approval_task(
            self.rmep_payroll,
            PayrollConfig.payroll_accept_event,
            self.upg.id,
        )
        strategy = SimpleNamespace(accept_payroll=MagicMock())

        with patch(
            "payroll.signals.PaymentMethodStorage.get_chosen_payment_method",
            return_value=strategy,
        ):
            result = TaskService(self.user).complete_task({"id": task.id})

        self.assertTrue(result["success"], result)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.COMPLETED)
        strategy.accept_payroll.assert_called_once_with(
            self.rmep_payroll,
            self.user,
        )

    def test_feature_disabled_preserves_existing_behaviour(self):
        SocialProtectionConfig.enable_benefit_plan_access_control = False
        self.assertEqual(
            self.visible_payrolls(), {self.upg_payroll.id, self.rmep_payroll.id}
        )
