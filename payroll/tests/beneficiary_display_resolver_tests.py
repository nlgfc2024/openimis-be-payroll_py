from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from payroll.apps import PayrollConfig
from payroll.gql_queries import BenefitConsumptionGQLType
from payroll.models import BenefitConsumptionStatus
from payroll.utils import get_micro_catchment_name


class BeneficiaryDisplayResolverTests(SimpleTestCase):
    def test_receipt_filter_remains_exposed(self):
        self.assertIn("receipt", BenefitConsumptionGQLType._meta.filter_fields)

    def test_unresolved_fee_error_is_filterable_and_survives_zero_snapshot(self):
        benefit = SimpleNamespace(
            withdrawal_fee=Decimal("0.00"),
            withdrawal_fee_error="No effective fee band.",
        )

        self.assertIn(
            "withdrawal_fee_error",
            BenefitConsumptionGQLType._meta.filter_fields,
        )
        self.assertEqual(
            BenefitConsumptionGQLType.resolve_withdrawal_fee(benefit, None),
            Decimal("0.00"),
        )
        self.assertEqual(
            BenefitConsumptionGQLType.resolve_withdrawal_fee_error(benefit, None),
            "No effective fee band.",
        )

    def test_kyc_resolvers_support_json_strings_and_legacy_aliases(self):
        benefit = SimpleNamespace(individual=SimpleNamespace(json_ext=(
            '{"sex":"Female","payment_account_number":"00012345",'
            '"payment_account_name":"Jane Banda","payment_provider":"Mpamba"}'
        )))

        self.assertEqual(BenefitConsumptionGQLType.resolve_gender(benefit, None), "Female")
        self.assertEqual(BenefitConsumptionGQLType.resolve_account_number(benefit, None), "00012345")
        self.assertEqual(BenefitConsumptionGQLType.resolve_account_name(benefit, None), "Jane Banda")
        self.assertEqual(BenefitConsumptionGQLType.resolve_bank(benefit, None), "Mpamba")

    def test_receipt_is_visible_to_payroll_users_without_fee_admin_right(self):
        benefit = SimpleNamespace(receipt="provider-reference-1")
        user = SimpleNamespace(is_authenticated=True, has_perms=lambda _perms: False)
        info = SimpleNamespace(context=SimpleNamespace(user=user))

        self.assertEqual(
            BenefitConsumptionGQLType.resolve_receipt(benefit, info),
            "provider-reference-1",
        )
        self.assertTrue(BenefitConsumptionGQLType.resolve_has_receipt(benefit, info))

    def test_receiptless_reconciled_benefit_still_has_paid_state(self):
        benefit = SimpleNamespace(receipt=None, status=BenefitConsumptionStatus.RECONCILED)

        self.assertTrue(BenefitConsumptionGQLType.resolve_has_receipt(benefit, None))

    def test_withdrawal_fee_manager_can_read_receipt(self):
        benefit = SimpleNamespace(receipt="provider-reference-1")
        user = SimpleNamespace(is_authenticated=True, has_perms=lambda _perms: True)
        info = SimpleNamespace(context=SimpleNamespace(user=user))

        with patch.object(PayrollConfig, "gql_withdrawal_fee_manage_perms", ["202005"]):
            self.assertEqual(
                BenefitConsumptionGQLType.resolve_receipt(benefit, info),
                "provider-reference-1",
            )

    def test_amount_to_pay_adds_the_programme_paid_fee(self):
        benefit = SimpleNamespace(
            amount=Decimal("100.00"),
            withdrawal_fee=Decimal("5.50"),
        )

        self.assertEqual(
            BenefitConsumptionGQLType.resolve_amount_to_pay(benefit, None),
            Decimal("105.50"),
        )

    def test_amount_to_pay_is_unavailable_before_fee_snapshot(self):
        benefit = SimpleNamespace(
            amount=Decimal("100.00"),
            withdrawal_fee=None,
            withdrawal_fee_prepared_at=None,
            json_ext={},
        )
        info = SimpleNamespace(context=SimpleNamespace())

        with (
            patch(
                "payroll.services.WithdrawalFeeConfigService.build_fee_preview_catalog",
                return_value={},
            ),
            patch(
                "payroll.services.WithdrawalFeeConfigService.preview_fee_for_benefit",
                return_value=(None, "No effective fee band."),
            ),
        ):
            self.assertIsNone(BenefitConsumptionGQLType.resolve_amount_to_pay(benefit, info))

    def test_amount_to_pay_uses_a_live_fee_preview_before_snapshot(self):
        benefit = SimpleNamespace(
            amount=Decimal("100.00"),
            withdrawal_fee=None,
            withdrawal_fee_prepared_at=None,
            json_ext={},
        )
        info = SimpleNamespace(context=SimpleNamespace())

        with (
            patch(
                "payroll.services.WithdrawalFeeConfigService.build_fee_preview_catalog",
                return_value={},
            ),
            patch(
                "payroll.services.WithdrawalFeeConfigService.preview_fee_for_benefit",
                return_value=(Decimal("5.50"), None),
            ),
        ):
            self.assertEqual(
                BenefitConsumptionGQLType.resolve_withdrawal_fee(benefit, info),
                Decimal("5.50"),
            )
            self.assertEqual(
                BenefitConsumptionGQLType.resolve_amount_to_pay(benefit, info),
                Decimal("105.50"),
            )
            self.assertIsNone(
                BenefitConsumptionGQLType.resolve_withdrawal_fee_error(benefit, info)
            )

    def test_project_name_intersects_selected_projects_with_active_enrollments(self):
        project_a = SimpleNamespace(id="project-a", name="Project A")
        project_b = SimpleNamespace(id="project-b", name="Project B")
        beneficiary = SimpleNamespace(
            active_project_enrollments=[SimpleNamespace(project=project_b)],
        )
        payroll = SimpleNamespace(json_ext={"filter_criteria": {"project_ids": ["project-a", "project-b"]}})
        benefit = SimpleNamespace(
            individual=SimpleNamespace(active_beneficiaries=[beneficiary]),
            active_payroll_links=[SimpleNamespace(payroll=payroll)],
        )

        self.assertEqual(BenefitConsumptionGQLType.resolve_project_name(benefit, None), "Project B")

    def test_project_name_uses_all_active_enrollments_without_payroll_project_filter(self):
        project_a = SimpleNamespace(id="project-a", name="Project A")
        project_b = SimpleNamespace(id="project-b", name="Project B")
        beneficiary = SimpleNamespace(
            active_project_enrollments=[
                SimpleNamespace(project=project_b),
                SimpleNamespace(project=project_a),
            ],
        )
        benefit = SimpleNamespace(
            individual=SimpleNamespace(active_beneficiaries=[beneficiary]),
            active_payroll_links=[SimpleNamespace(payroll=SimpleNamespace(json_ext={}))],
        )

        self.assertEqual(BenefitConsumptionGQLType.resolve_project_name(benefit, None), "Project A, Project B")

    @patch("project_social_protection.models.Project.objects.filter")
    def test_project_name_falls_back_to_selected_projects_only_without_enrollments(self, project_filter):
        project_filter.return_value = [SimpleNamespace(id="project-a", name="Project A")]
        payroll = SimpleNamespace(json_ext={"filter_criteria": {"project_ids": ["project-a"]}})
        benefit = SimpleNamespace(
            individual=SimpleNamespace(active_beneficiaries=[]),
            active_payroll_links=[SimpleNamespace(payroll=payroll)],
        )

        self.assertEqual(BenefitConsumptionGQLType.resolve_project_name(benefit, None), "Project A")
        project_filter.assert_called_once_with(id__in=["project-a"], is_deleted=False)

    @patch("payroll.utils.apps.get_model")
    def test_catchment_uses_only_active_gvh_mapping(self, get_model):
        link = SimpleNamespace(micro_catchment=SimpleNamespace(name="Catchment A"))
        query = MagicMock()
        query.filter.return_value.select_related.return_value.order_by.return_value.first.return_value = link
        get_model.return_value.objects = query
        gvh = SimpleNamespace(id="gvh-id", type="W", parent=None)

        self.assertEqual(get_micro_catchment_name(gvh), "Catchment A")
        query.filter.assert_called_once_with(
            location_id="gvh-id",
            validity_to__isnull=True,
            micro_catchment__validity_to__isnull=True,
        )

    def test_catchment_is_empty_without_a_gvh(self):
        self.assertIsNone(get_micro_catchment_name(None))
