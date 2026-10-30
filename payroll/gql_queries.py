import graphene
from graphene_django import DjangoObjectType
from django.apps import apps
from django.db.models import Prefetch

from core import prefix_filterset, ExtendedConnection
from core.gql_queries import UserGQLType
from invoice.gql.gql_types.bill_types import BillGQLType
from location.gql_queries import LocationGQLType
from individual.gql_queries import IndividualGQLType
from payroll.models import PaymentPoint, Payroll, BenefitConsumption, \
    PayrollBenefitConsumption, BenefitAttachment, CsvReconciliationUpload, ReconciliationUploadRow, ConsolidatedPayrollMember
from contribution_plan.gql import PaymentPlanGQLType
from payment_cycle.gql_queries import PaymentCycleGQLType
from social_protection.models import BenefitPlan
from tasks_management.models import Task
from payroll.utils import get_micro_catchment_name


def _malawi_locations(individual):
    """Return a beneficiary's village and ancestors keyed by location type."""
    locations = {}
    location = individual.location
    # The supported hierarchy is District (R) -> TA (D) -> GVH (W) -> Village (V).
    # The guard also makes incomplete or malformed historical hierarchies safe to display.
    while location and location.id not in {item.id for item in locations.values()}:
        locations[location.type] = location
        location = location.parent
    return locations


def benefit_consumption_queryset(queryset):
    """Load data used by payroll-context resolvers in fixed-size queries."""
    BeneficiaryProjectEnrollment = apps.get_model(
        "project_social_protection", "BeneficiaryProjectEnrollment",
    )
    from social_protection.models import Beneficiary

    enrollments = BeneficiaryProjectEnrollment.objects.filter(
        is_deleted=False,
        project__is_deleted=False,
    ).select_related("project")
    beneficiaries = Beneficiary.objects.filter(is_deleted=False).prefetch_related(
        Prefetch("project_enrollments", queryset=enrollments, to_attr="active_project_enrollments"),
    )
    return queryset.select_related(
        "individual__location__parent__parent__parent",
    ).prefetch_related(
        Prefetch("individual__beneficiary_set", queryset=beneficiaries, to_attr="active_beneficiaries"),
        Prefetch(
            "payrollbenefitconsumption_set",
            queryset=PayrollBenefitConsumption.objects.filter(
                is_deleted=False,
                payroll__is_deleted=False,
            ).select_related("payroll").order_by("-date_created"),
            to_attr="active_payroll_links",
        ),
    )


class PaymentPointGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')

    class Meta:
        model = PaymentPoint
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "name": ["iexact", "istartswith", "icontains"],
            **prefix_filterset("location__", LocationGQLType._meta.filter_fields),
            **prefix_filterset("ppm__", UserGQLType._meta.filter_fields),

            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
        }
        connection_class = ExtendedConnection


class BenefitAttachmentGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')

    class Meta:
        model = BenefitAttachment
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            **prefix_filterset("bill__", BillGQLType._meta.filter_fields),

            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "date_valid_from": ["exact", "lt", "lte", "gt", "gte"],
            "date_valid_to": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
        }
        connection_class = ExtendedConnection


class BenefitConsumptionGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')
    benefit_attachment = graphene.List(BenefitAttachmentGQLType)
    national_id = graphene.String()
    form_number = graphene.String()
    phone_number = graphene.String()
    district_name = graphene.String()
    traditional_authority_name = graphene.String()
    group_village_head_name = graphene.String()
    catchment_name = graphene.String()
    project_name = graphene.String()

    class Meta:
        model = BenefitConsumption
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "photo": ["iexact", "istartswith", "icontains"],
            "code": ["iexact", "istartswith", "icontains"],
            "status": ["exact", "startswith", "icontains", "contains"],
            "receipt": ["exact", "startswith", "icontains"],
            "type": ["exact", "startswith", "icontains"],
            "amount": ["exact", "lt", "lte", "gt", "gte"],
            "date_due": ["exact", "lt", "lte", "gt", "gte"],
            **prefix_filterset("individual__", IndividualGQLType._meta.filter_fields),

            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "date_valid_from": ["exact", "lt", "lte", "gt", "gte"],
            "date_valid_to": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
        }
        connection_class = ExtendedConnection

    def resolve_benefit_attachment(self, info):
        return BenefitAttachment.objects.filter(
            benefit_id=self.id,
            is_deleted=False
        )

    def resolve_national_id(self, info):
        return (self.individual.json_ext or {}).get("national_id")

    def resolve_form_number(self, info):
        form_number = (self.individual.json_ext or {}).get("form_number")
        return str(form_number) if form_number is not None else None

    def resolve_phone_number(self, info):
        phone_number = (self.individual.json_ext or {}).get("household_mobile_number")
        return str(phone_number) if phone_number is not None else None

    def resolve_district_name(self, info):
        location = _malawi_locations(self.individual).get("R")
        return location.name if location else None

    def resolve_traditional_authority_name(self, info):
        location = _malawi_locations(self.individual).get("D")
        return location.name if location else None

    def resolve_group_village_head_name(self, info):
        location = _malawi_locations(self.individual).get("W")
        return location.name if location else None

    def resolve_catchment_name(self, info):
        return get_micro_catchment_name(self.individual.location)

    def resolve_project_name(self, info):
        """Return the project(s) that selected this beneficiary for the payroll."""
        from project_social_protection.models import Project

        payroll_links = getattr(self, "active_payroll_links", None)
        payroll_link = payroll_links[0] if payroll_links else self.payrollbenefitconsumption_set.filter(
            is_deleted=False, payroll__is_deleted=False,
        ).select_related("payroll").order_by("-date_created").first()
        project_ids = (
            (payroll_link.payroll.json_ext or {}).get("filter_criteria", {}).get("project_ids", [])
            if payroll_link else []
        )
        beneficiaries = getattr(self.individual, "active_beneficiaries", None)
        if beneficiaries is None:
            beneficiaries = self.individual.beneficiary_set.filter(is_deleted=False).prefetch_related(
                "project_enrollments__project",
            )
        enrolled_projects = []
        for beneficiary in beneficiaries:
            enrollments = getattr(beneficiary, "active_project_enrollments", None)
            if enrollments is None:
                enrollments = beneficiary.project_enrollments.filter(
                    is_deleted=False,
                    project__is_deleted=False,
                ).select_related("project")
            enrolled_projects.extend(enrollment.project for enrollment in enrollments)
        if project_ids and enrolled_projects:
            selected_ids = {str(id_) for id_ in project_ids}
            projects = [project for project in enrolled_projects if str(project.id) in selected_ids]
        elif enrolled_projects:
            projects = enrolled_projects
        else:
            projects = Project.objects.filter(id__in=project_ids, is_deleted=False) if project_ids else []
        names = sorted({project.name for project in projects})
        return ", ".join(names) or None


class ConsolidatedPayrollMemberGQLType(graphene.ObjectType):
    source_payroll_id = graphene.String()
    source_payroll_name = graphene.String()
    source_payroll_status = graphene.String()
    beneficiaries_count = graphene.Int()
    total_amount = graphene.Decimal()


class PayrollGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')
    benefit_consumption = graphene.List(BenefitConsumptionGQLType)
    benefit_plan_name_code = graphene.String()
    consolidated_members = graphene.List(ConsolidatedPayrollMemberGQLType)

    class Meta:
        model = Payroll
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "name": ["iexact", "istartswith", "icontains"],
            "status": ["exact", "startswith", "contains"],
            "payment_method": ["exact", "startswith", "contains"],
            **prefix_filterset("payment_point__", PaymentPointGQLType._meta.filter_fields),
            **prefix_filterset("payment_plan__", PaymentPlanGQLType._meta.filter_fields),
            **prefix_filterset("payment_cycle__", PaymentCycleGQLType._meta.filter_fields),

            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "date_valid_from": ["exact", "lt", "lte", "gt", "gte"],
            "date_valid_to": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
        }
        connection_class = ExtendedConnection

    def resolve_benefit_consumption(self, info):
        return benefit_consumption_queryset(BenefitConsumption.objects.filter(
            payrollbenefitconsumption__payroll__id=self.id,
            is_deleted=False,
            payrollbenefitconsumption__is_deleted=False,
            individual__groupindividuals__recipient_type="PRIMARY",
            individual__groupindividuals__is_deleted=False,
        ).distinct())

    def resolve_benefit_plan_name_code(self, info):
        benefit_plan = BenefitPlan.objects.get(id=self.payment_plan.benefit_plan.id, is_deleted=False)
        return f"{benefit_plan.code} - {benefit_plan.name}"

    def resolve_consolidated_members(self, info):
        if self.payroll_type != Payroll.PayrollType.CONSOLIDATED:
            return []
        return [{
            "source_payroll_id": str(member.source_payroll_id),
            "source_payroll_name": member.source_payroll.name,
            "source_payroll_status": member.source_payroll.status,
            "beneficiaries_count": member.beneficiaries_count,
            "total_amount": member.total_amount,
        } for member in ConsolidatedPayrollMember.objects.filter(
            consolidated_payroll=self, is_deleted=False,
        ).select_related("source_payroll")]


class PaymentMethodGQLType(graphene.ObjectType):
    name = graphene.String()


class PaymentGatewayConfigGQLType(graphene.ObjectType):
    base_url = graphene.String()
    api_key = graphene.String()
    timeout = graphene.Int()


class PaymentMethodListGQLType(graphene.ObjectType):
    payment_methods = graphene.List(PaymentMethodGQLType)


class BenefitAttachmentListGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')

    class Meta:
        model = BenefitAttachment
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            **prefix_filterset("bill__", BillGQLType._meta.filter_fields),
            **prefix_filterset("benefit__", BenefitConsumptionGQLType._meta.filter_fields),

            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "date_valid_from": ["exact", "lt", "lte", "gt", "gte"],
            "date_valid_to": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
        }
        connection_class = ExtendedConnection


class CsvReconciliationUploadGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')
    review_task_id = graphene.String()
    review_task_status = graphene.String()

    @staticmethod
    def _review_task(upload):
        if not hasattr(upload, '_reconciliation_review_task'):
            upload._reconciliation_review_task = Task.objects.filter(
                source='payroll_reconciliation',
                data__upload_id=str(upload.id),
                is_deleted=False,
            ).order_by('-date_created').first()
        return upload._reconciliation_review_task

    @classmethod
    def resolve_review_task_id(cls, upload, info):
        task = cls._review_task(upload)
        return str(task.id) if task else None

    @classmethod
    def resolve_review_task_status(cls, upload, info):
        task = cls._review_task(upload)
        return task.status if task else None

    class Meta:
        model = CsvReconciliationUpload
        interfaces = (graphene.relay.Node,)

        filter_fields = {
            "id": ["exact"],
            "file_name": ["exact", "iexact", "istartswith", "icontains"],
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "status": ["exact", "iexact", "istartswith", "icontains"],
            "is_deleted": ["exact"],
            "version": ["exact"],
            **prefix_filterset("payroll__", PayrollGQLType._meta.filter_fields),
        }
        connection_class = ExtendedConnection


class ReconciliationUploadRowGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')

    class Meta:
        model = ReconciliationUploadRow
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "row_number": ["exact", "lt", "lte", "gt", "gte"],
            "code": ["exact", "iexact", "istartswith", "icontains"],
            "national_id": ["exact", "iexact", "istartswith", "icontains"],
            "status": ["exact", "iexact"],
            "is_deleted": ["exact"],
            **prefix_filterset("upload__", CsvReconciliationUploadGQLType._meta.filter_fields),
        }
        connection_class = ExtendedConnection


class PayrollBenefitConsumptionGQLType(DjangoObjectType):

    class Meta:
        model = PayrollBenefitConsumption
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            **prefix_filterset("payroll__", PayrollGQLType._meta.filter_fields),
            **prefix_filterset("benefit__", BenefitConsumptionGQLType._meta.filter_fields),
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
        }
        connection_class = ExtendedConnection


class BenefitsSummaryGQLType(graphene.ObjectType):
    total_amount_received = graphene.String()
    total_amount_due = graphene.String()
