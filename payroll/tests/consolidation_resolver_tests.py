from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from payroll.consolidation import payrolls_in_district
from payroll.schema import Query


class ConsolidationResolverTests(SimpleTestCase):
    def setUp(self):
        self.info = SimpleNamespace(
            context=SimpleNamespace(
                user=SimpleNamespace(id=1, has_perms=lambda permissions: True),
            ),
        )

    def test_candidates_resolve_district_uuid_to_location_id(self):
        location = SimpleNamespace(id=102)
        payroll_queryset = MagicMock()
        with patch("payroll.schema.Location.objects.only") as locations, \
                patch("payroll.schema.Payroll.objects.filter") as payroll_filter, \
                patch("payroll.schema.payrolls_in_district", return_value=[]) as in_district:
            locations.return_value.get.return_value = location
            payroll_filter.return_value.filter.return_value.select_related.return_value.distinct.return_value = (
                payroll_queryset
            )

            result = Query._consolidation_candidates("district-uuid")

        self.assertEqual(result, [])
        locations.return_value.get.assert_called_once_with(uuid="district-uuid")
        in_district.assert_called_once_with(payroll_queryset, 102)

    def test_candidates_empty_for_unknown_district(self):
        from location.models import Location

        with patch("payroll.schema.Location.objects.only") as locations, \
                patch("payroll.schema.Payroll.objects.none", return_value=[]) as none, \
                patch("payroll.schema.payrolls_in_district") as in_district:
            locations.return_value.get.side_effect = Location.DoesNotExist

            result = Query._consolidation_candidates("missing-uuid")

        self.assertEqual(result, [])
        none.assert_called_once_with()
        in_district.assert_not_called()

    def test_candidates_exclude_payrolls_in_active_consolidation(self):
        with patch("payroll.schema.Location.objects.only") as locations, \
                patch("payroll.schema.Payroll.objects.filter") as payroll_filter, \
                patch("payroll.schema.payrolls_in_district", return_value=[]):
            locations.return_value.get.return_value = SimpleNamespace(id=102)

            Query._consolidation_candidates("district-uuid")

        exclusion = payroll_filter.return_value.filter.call_args.args[0]
        self.assertTrue(exclusion.negated)

    def test_payrolls_in_district_resolves_saved_location_uuids(self):
        district = SimpleNamespace(id=102, uuid="district-uuid", parent_id=None)
        child = SimpleNamespace(id=103, uuid="child-uuid", parent_id=102)
        payrolls = [
            SimpleNamespace(
                id=1,
                json_ext={"filter_criteria": {"location_ids": ["district-uuid"]}},
            ),
            SimpleNamespace(
                id=2,
                json_ext={"filter_criteria": {"location_ids": ["103"]}},
            ),
        ]
        project_model = SimpleNamespace(
            objects=SimpleNamespace(filter=MagicMock()),
        )
        project_model.objects.filter.return_value.select_related.return_value = []

        with patch("payroll.consolidation.apps.get_model", return_value=project_model), \
                patch(
                    "payroll.consolidation.Location.objects.filter",
                    return_value=[district, child],
                ):
            result = payrolls_in_district(payrolls, district.id)

        self.assertEqual(result, payrolls)

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
