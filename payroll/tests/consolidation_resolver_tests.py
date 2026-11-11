from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from django.db.models.expressions import NegatedExpression

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
                patch("payroll.schema.ContentType.objects.get_for_model", return_value=1), \
                patch("payroll.schema.Payroll.objects.filter") as payroll_filter, \
                patch("payroll.schema.payrolls_in_district", return_value=[]) as in_district:
            locations.return_value.get.return_value = location
            payroll_filter.return_value.filter.return_value.select_related.return_value.distinct.return_value = (
                payroll_queryset
            )

            result = Query._consolidation_candidates("district-uuid")

        self.assertEqual(result, [])
        locations.return_value.get.assert_called_once_with(
            uuid="district-uuid",
            validity_to__isnull=True,
        )
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

    def test_candidates_empty_for_unassigned_district(self):
        with patch("payroll.schema.Location.objects.only") as locations, \
                patch("payroll.schema.LocationManager") as location_manager, \
                patch("payroll.schema.Payroll.objects.none", return_value=[]) as none, \
                patch("payroll.schema.Payroll.objects.filter") as payroll_filter:
            locations.return_value.get.return_value = SimpleNamespace(id=102)
            location_manager.return_value.is_allowed.return_value = False

            result = Query._consolidation_candidates(
                "district-uuid",
                user=SimpleNamespace(id=1),
            )

        self.assertEqual(result, [])
        none.assert_called_once_with()
        payroll_filter.assert_not_called()
        location_manager.return_value.is_allowed.assert_called_once_with(
            SimpleNamespace(id=1),
            [102],
        )

    def test_candidates_exclude_payrolls_in_active_consolidation(self):
        with patch("payroll.schema.Location.objects.only") as locations, \
                patch("payroll.schema.ContentType.objects.get_for_model", return_value=1), \
                patch("payroll.schema.Payroll.objects.filter") as payroll_filter, \
                patch("payroll.schema.payrolls_in_district", return_value=[]):
            locations.return_value.get.return_value = SimpleNamespace(id=102)

            Query._consolidation_candidates("district-uuid")

        exclusion = payroll_filter.return_value.filter.call_args.args[0]
        self.assertIsInstance(exclusion, NegatedExpression)

    def test_candidates_exclude_recipients_from_prior_consolidations(self):
        candidate = SimpleNamespace(id=1)
        candidate_links = MagicMock()
        candidate_links.values_list.return_value = [(1, 25)]
        prior_links = MagicMock()
        prior_links.exclude.return_value.values_list.return_value.distinct.return_value = [25]
        benefit_links = MagicMock()
        benefit_links.filter.return_value = MagicMock()
        with patch("payroll.schema.Location.objects.only") as locations, \
                patch("payroll.schema.ContentType.objects.get_for_model", return_value=1), \
                patch("payroll.schema.Payroll.objects.filter") as payroll_filter, \
                patch("payroll.schema.ConsolidatedPayrollMember.objects.filter"), \
                patch("payroll.schema.PayrollBenefitConsumption.objects.filter") as benefit_filter, \
                patch("payroll.schema.Task.objects.filter"), \
                patch(
                    "payroll.schema.payrolls_in_district",
                    return_value=[candidate],
                ):
            locations.return_value.get.return_value = SimpleNamespace(id=102)
            benefit_filter.side_effect = [
                benefit_links,
                candidate_links,
                prior_links,
            ]

            result = Query._consolidation_candidates("district-uuid")

        self.assertEqual(result, [])

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

    def test_payrolls_in_district_rejects_spanning_and_unlocated_projects(self):
        district = SimpleNamespace(id=102, uuid="district-uuid", parent_id=None)
        child = SimpleNamespace(id=103, uuid="child-uuid", parent_id=102)
        other_district = SimpleNamespace(id=202, uuid="other-district", parent_id=None)
        other_child = SimpleNamespace(id=203, uuid="other-child", parent_id=202)
        spanning = SimpleNamespace(
            id=1,
            json_ext={"filter_criteria": {"project_ids": ["project-a", "project-b"]}},
        )
        unlocated = SimpleNamespace(
            id=2,
            json_ext={"filter_criteria": {"project_ids": ["project-c"]}},
        )
        projects = [
            SimpleNamespace(id="project-a", location_id=child.id),
            SimpleNamespace(id="project-b", location_id=other_child.id),
            SimpleNamespace(id="project-c", location_id=None),
        ]
        project_model = SimpleNamespace(
            objects=SimpleNamespace(filter=MagicMock()),
        )
        project_model.objects.filter.return_value.select_related.return_value = projects

        with patch("payroll.consolidation.apps.get_model", return_value=project_model), \
                patch(
                    "payroll.consolidation.Location.objects.filter",
                    return_value=[district, child, other_district, other_child],
                ):
            result = payrolls_in_district([spanning, unlocated], district.id)

        self.assertEqual(result, [])

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
        candidates_query.assert_called_once_with(
            "district-id",
            "phase-id",
            user=self.info.context.user,
        )

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
        candidates_query.assert_called_once_with(
            "district-id",
            user=self.info.context.user,
        )
        phases_filter.assert_called_once_with(id__in={"phase-id"}, is_deleted=False)
