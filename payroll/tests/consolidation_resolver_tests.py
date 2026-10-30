from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from payroll.schema import Query


class ConsolidationResolverTests(SimpleTestCase):
    def setUp(self):
        self.info = SimpleNamespace(
            context=SimpleNamespace(
                user=SimpleNamespace(id=1, has_perms=lambda permissions: True),
            ),
        )

    @patch("payroll.schema.Query._consolidation_candidates")
    def test_candidates_resolver_works_with_graphene_root(self, candidates_query):
        candidates = [SimpleNamespace(id="payroll-id")]
        candidates_query.return_value = candidates

        result = Query.resolve_consolidation_candidates(
            None,
            self.info,
            district_id="district-id",
            benefit_plan_id="phase-id",
        )

        self.assertEqual(result, candidates)
        candidates_query.assert_called_once_with("district-id", "phase-id")

    @patch("payroll.schema.BenefitPlan.objects.filter")
    @patch("payroll.schema.Query._consolidation_candidates")
    def test_phases_resolver_works_with_graphene_root(self, candidates_query, phases_filter):
        candidates_query.return_value = [
            SimpleNamespace(
                payment_plan_id="payment-plan-id",
                payment_plan=SimpleNamespace(benefit_plan_id="phase-id"),
            ),
        ]
        phase = SimpleNamespace(id="phase-id")
        phases_filter.return_value.order_by.return_value = [phase]

        result = Query.resolve_consolidation_phases(
            None,
            self.info,
            district_id="district-id",
        )

        self.assertEqual(result, [phase])
        candidates_query.assert_called_once_with("district-id")
        phases_filter.assert_called_once_with(id__in={"phase-id"}, is_deleted=False)
