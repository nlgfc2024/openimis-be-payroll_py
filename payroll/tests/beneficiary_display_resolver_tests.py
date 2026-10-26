from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from payroll.gql_queries import BenefitConsumptionGQLType
from payroll.utils import get_micro_catchment_name


class BeneficiaryDisplayResolverTests(SimpleTestCase):
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
