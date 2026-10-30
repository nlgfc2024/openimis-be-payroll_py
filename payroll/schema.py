import graphene
import graphene_django_optimizer as gql_optimizer
from gettext import gettext as _
from django.apps import apps
from django.contrib.auth.models import AnonymousUser
from django.db.models import Q, Sum

from core.schema import OrderedDjangoFilterConnectionField
from core.services import wait_for_mutation
from core.utils import append_validity_filter
from invoice.gql.gql_types.bill_types import BillGQLType
from invoice.models import Bill
from location.services import get_ancestor_location_filter
from payroll.apps import PayrollConfig
from payroll.gql_mutations import CreatePaymentPointMutation, UpdatePaymentPointMutation, DeletePaymentPointMutation, \
    CreatePayrollMutation, DeletePayrollMutation, ClosePayrollMutation, \
    RejectPayrollMutation, MakePaymentForPayrollMutation, DeleteBenefitConsumptionMutation, CreateConsolidatedPayrollMutation
from payroll.gql_queries import BenefitConsumptionGQLType, PaymentPointGQLType, benefit_consumption_queryset, \
    PayrollGQLType, PaymentMethodGQLType, \
    PaymentMethodListGQLType, BenefitAttachmentListGQLType, \
    CsvReconciliationUploadGQLType, ReconciliationUploadRowGQLType, PayrollBenefitConsumptionGQLType, \
    PaymentGatewayConfigGQLType, BenefitsSummaryGQLType
from payroll.models import PaymentPoint, Payroll, \
    BenefitConsumption, BenefitAttachment, \
    CsvReconciliationUpload, ReconciliationUploadRow, PayrollBenefitConsumption, BenefitConsumptionStatus
from payroll.payments_registry import PaymentMethodStorage
from social_protection.models import BenefitPlan


class ConsolidationPhaseGQLType(graphene.ObjectType):
    uuid = graphene.String()
    code = graphene.String()
    name = graphene.String()


class Query(graphene.ObjectType):
    payment_point = OrderedDjangoFilterConnectionField(
        PaymentPointGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        client_mutation_id=graphene.String(),
        parent_location=graphene.String(),
        parent_location_level=graphene.Int(),
    )
    payroll = OrderedDjangoFilterConnectionField(
        PayrollGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        dateValidFrom__Gte=graphene.DateTime(),
        dateValidTo__Lte=graphene.DateTime(),
        client_mutation_id=graphene.String(),
    )

    bill_by_payroll = OrderedDjangoFilterConnectionField(
        BillGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        dateValidFrom__Gte=graphene.DateTime(),
        dateValidTo__Lte=graphene.DateTime(),
        applyDefaultValidityFilter=graphene.Boolean(),
        client_mutation_id=graphene.String(),
        payroll_uuid=graphene.UUID(required=True)
    )

    benefit_consumption = OrderedDjangoFilterConnectionField(
        BenefitConsumptionGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        dateValidFrom__Gte=graphene.DateTime(),
        dateValidTo__Lte=graphene.DateTime(),
        client_mutation_id=graphene.String(),
    )

    payment_methods = graphene.Field(
        PaymentMethodListGQLType,
    )

    payment_gateway_config = graphene.Field(
        PaymentGatewayConfigGQLType,
    )

    benefit_consumption_by_payroll = OrderedDjangoFilterConnectionField(
        BenefitConsumptionGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        dateValidFrom__Gte=graphene.DateTime(),
        dateValidTo__Lte=graphene.DateTime(),
        applyDefaultValidityFilter=graphene.Boolean(),
        client_mutation_id=graphene.String(),
        payroll_uuid=graphene.UUID(required=True),
        filterOnlyUnpaid=graphene.Boolean()
    )

    benefit_attachment_by_payroll = OrderedDjangoFilterConnectionField(
        BenefitAttachmentListGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        dateValidFrom__Gte=graphene.DateTime(),
        dateValidTo__Lte=graphene.DateTime(),
        applyDefaultValidityFilter=graphene.Boolean(),
        client_mutation_id=graphene.String(),
        payroll_uuid=graphene.UUID(required=True)
    )

    csv_reconciliation_upload = OrderedDjangoFilterConnectionField(
        CsvReconciliationUploadGQLType,
        orderBy=graphene.List(of_type=graphene.String),
    )

    reconciliation_upload_row = OrderedDjangoFilterConnectionField(
        ReconciliationUploadRowGQLType,
        orderBy=graphene.List(of_type=graphene.String),
    )

    payroll_benefit_consumption = OrderedDjangoFilterConnectionField(
        PayrollBenefitConsumptionGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        dateValidFrom__Gte=graphene.DateTime(),
        dateValidTo__Lte=graphene.DateTime(),
        client_mutation_id=graphene.String(),
        benefitPlanName=graphene.String(),
        benefitPlanUuid=graphene.String(),
        paymentCycleUuid=graphene.String(),
    )

    benefits_summary = graphene.Field(
        BenefitsSummaryGQLType,
        individualId=graphene.String(),
        payrollId=graphene.String(),
        benefitPlanUuid=graphene.String(),
        paymentCycleUuid=graphene.String(),
    )

    consolidation_candidates = graphene.List(
        PayrollGQLType,
        district_id=graphene.UUID(required=True),
        benefit_plan_id=graphene.UUID(required=True),
    )
    consolidation_phases = graphene.List(
        ConsolidationPhaseGQLType,
        district_id=graphene.UUID(required=True),
    )

    def resolve_bill_by_payroll(self, info, **kwargs):
        Query._check_permissions(info.context.user, PayrollConfig.gql_payroll_search_perms)
        filters = [*append_validity_filter(**kwargs), Q(payrollbill__payroll_id=kwargs.get("payroll_uuid"),
                                                        is_deleted=False,
                                                        payrollbill__is_deleted=False,
                                                        payrollbill__payroll__is_deleted=False)]

        client_mutation_id = kwargs.get("client_mutation_id", None)
        if client_mutation_id:
            wait_for_mutation(client_mutation_id)
            filters.append(Q(mutations__mutation__client_mutation_id=client_mutation_id))

        subject_type = kwargs.get("subject_type", None)
        if subject_type:
            filters.append(Q(subject_type__model=subject_type))

        thirdparty_type = kwargs.get("thirdparty_type", None)
        if thirdparty_type:
            filters.append(Q(thirdparty_type__model=thirdparty_type))

        return gql_optimizer.query(Bill.objects.filter(*filters), info)

    def resolve_benefit_consumption_by_payroll(self, info, **kwargs):
        Query._check_permissions(info.context.user, PayrollConfig.gql_payroll_search_perms)
        filters = [*append_validity_filter(**kwargs),
                   Q(payrollbenefitconsumption__payroll_id=kwargs.get("payroll_uuid"),
                     is_deleted=False,
                     payrollbenefitconsumption__is_deleted=False,
                     payrollbenefitconsumption__payroll__is_deleted=False)]

        client_mutation_id = kwargs.get("client_mutation_id", None)
        if client_mutation_id:
            wait_for_mutation(client_mutation_id)
            filters.append(Q(mutations__mutation__client_mutation_id=client_mutation_id))

        filter_only_unpaid = kwargs.get("filterOnlyUnpaid", None)
        if filter_only_unpaid:
            filters.append(Q(status__in=[BenefitConsumptionStatus.ACCEPTED, BenefitConsumptionStatus.APPROVE_FOR_PAYMENT]))

        return gql_optimizer.query(benefit_consumption_queryset(
            BenefitConsumption.objects.filter(
                *filters,
                individual__groupindividuals__recipient_type="PRIMARY",
                individual__groupindividuals__is_deleted=False,
            ).distinct()
        ), info)

    def resolve_benefit_attachment_by_payroll(self, info, **kwargs):
        Query._check_permissions(info.context.user, PayrollConfig.gql_payroll_search_perms)
        filters = [*append_validity_filter(**kwargs),
                   Q(benefit__payrollbenefitconsumption__payroll_id=kwargs.get("payroll_uuid"),
                     is_deleted=False,
                     benefit__payrollbenefitconsumption__is_deleted=False,
                     benefit__payrollbenefitconsumption__payroll__is_deleted=False,
                     benefit__individual__groupindividuals__recipient_type="PRIMARY",
                     benefit__individual__groupindividuals__is_deleted=False)]

        client_mutation_id = kwargs.get("client_mutation_id", None)
        if client_mutation_id:
            wait_for_mutation(client_mutation_id)
            filters.append(Q(mutations__mutation__client_mutation_id=client_mutation_id))

        return gql_optimizer.query(BenefitAttachment.objects.filter(*filters).distinct(), info)

    def resolve_payment_point(self, info, **kwargs):
        Query._check_permissions(info.context.user, PayrollConfig.gql_payment_point_search_perms)
        filters = []

        client_mutation_id = kwargs.get("client_mutation_id")
        if client_mutation_id:
            wait_for_mutation(client_mutation_id)
            filters.append(Q(mutations__mutation__client_mutation_id=client_mutation_id))

        parent_location = kwargs.get('parent_location')
        if parent_location:
            filters += [get_ancestor_location_filter(parent_location)]

        query = PaymentPoint.objects.filter(*filters)
        return gql_optimizer.query(query, info)

    def resolve_payroll(self, info, **kwargs):
        Query._check_permissions(info.context.user, PayrollConfig.gql_payroll_search_perms)
        filters = append_validity_filter(**kwargs)

        client_mutation_id = kwargs.get("client_mutation_id")
        if client_mutation_id:
            wait_for_mutation(client_mutation_id)
            filters.append(Q(mutations__mutation__client_mutation_id=client_mutation_id))

        query = Payroll.objects.filter(*filters)
        return gql_optimizer.query(query, info)

    @staticmethod
    def _consolidation_candidates(district_id, benefit_plan_id=None):
        """Return available source payrolls belonging to a district.

        New project payrolls retain project IDs in their criteria rather than a
        district ID, so resolve their project location ancestry as well as the
        legacy location filter.
        """
        filters = {
            "status": 'APPROVE_FOR_PAYMENT',
            "payroll_type": Payroll.PayrollType.STANDARD,
            "is_deleted": False,
        }
        if benefit_plan_id:
            filters["payment_plan__benefit_plan_id"] = benefit_plan_id
        payrolls = Payroll.objects.filter(**filters).exclude(
            consolidated_memberships__is_deleted=False,
        ).select_related("payment_plan__benefit_plan").distinct()
        project_model = apps.get_model("project_social_protection", "Project")
        district_id = str(district_id)
        matches = []
        for payroll in payrolls:
            criteria = (payroll.json_ext or {}).get("filter_criteria", {})
            location_ids = {str(location_id) for location_id in criteria.get("location_ids", [])}
            if district_id in location_ids:
                matches.append(payroll)
                continue
            projects = project_model.objects.filter(
                id__in=criteria.get("project_ids", []), is_deleted=False,
            ).select_related("location__parent__parent__parent")
            for project in projects:
                location = project.location
                while location:
                    if str(location.uuid) == district_id:
                        matches.append(payroll)
                        break
                    location = location.parent
                else:
                    continue
                break
            else:
                # Older payrolls can have neither a project nor a district in
                # their saved criteria. Their paid beneficiaries still provide
                # the authoritative district for consolidation eligibility.
                beneficiary_in_district = BenefitConsumption.objects.filter(
                    payrollbenefitconsumption__payroll=payroll,
                    payrollbenefitconsumption__is_deleted=False,
                    is_deleted=False,
                ).filter(
                    Q(individual__location__uuid=district_id)
                    | Q(individual__location__parent__uuid=district_id)
                    | Q(individual__location__parent__parent__uuid=district_id)
                    | Q(individual__location__parent__parent__parent__uuid=district_id)
                ).exists()
                if beneficiary_in_district:
                    matches.append(payroll)
        return matches

    def resolve_consolidation_candidates(self, info, district_id, benefit_plan_id):
        Query._check_permissions(info.context.user, PayrollConfig.gql_payroll_search_perms)
        return self._consolidation_candidates(district_id, benefit_plan_id)

    def resolve_consolidation_phases(self, info, district_id):
        Query._check_permissions(info.context.user, PayrollConfig.gql_payroll_search_perms)
        phase_ids = {
            payroll.payment_plan.benefit_plan_id
            for payroll in self._consolidation_candidates(district_id)
            if payroll.payment_plan_id
        }
        return BenefitPlan.objects.filter(id__in=phase_ids, is_deleted=False).order_by("name")

    def resolve_payroll_benefit_consumption(self, info, **kwargs):
        Query._check_permissions(info.context.user, PayrollConfig.gql_payroll_search_perms)
        filters = append_validity_filter(**kwargs)

        client_mutation_id = kwargs.get("client_mutation_id")
        if client_mutation_id:
            wait_for_mutation(client_mutation_id)
            filters.append(Q(mutations__mutation__client_mutation_id=client_mutation_id))

        benefit_plan_name = kwargs.get("benefitPlanName")
        if benefit_plan_name:
            benefit_plan_ids = list(BenefitPlan.objects.filter(name__icontains=benefit_plan_name).values_list('id', flat=True))
            filters.append(Q(payroll__payment_plan__benefit_plan_id__in=benefit_plan_ids))

        benefit_plan_uuid = kwargs.get("benefitPlanUuid")
        if benefit_plan_uuid:
            filters.append(Q(payroll__payment_plan__benefit_plan_id=benefit_plan_uuid))

        payment_cycle_uuid = kwargs.get("paymentCycleUuid")
        if payment_cycle_uuid:
            filters.append(Q(payroll__payment_cycle_id=payment_cycle_uuid))

        query = PayrollBenefitConsumption.objects.filter(
            *filters,
            benefit__individual__groupindividuals__recipient_type="PRIMARY",
            benefit__individual__groupindividuals__is_deleted=False,
        ).distinct()
        return gql_optimizer.query(query, info)

    def resolve_benefit_consumption(self, info, **kwargs):
        Query._check_permissions(info.context.user, PayrollConfig.gql_payroll_search_perms)
        filters = append_validity_filter(**kwargs)

        client_mutation_id = kwargs.get("client_mutation_id")
        if client_mutation_id:
            wait_for_mutation(client_mutation_id)
            filters.append(Q(mutations__mutation__client_mutation_id=client_mutation_id))

        query = benefit_consumption_queryset(BenefitConsumption.objects.filter(*filters))
        return gql_optimizer.query(query, info)

    def resolve_payment_methods(self, info, **kwargs):
        user = info.context.user
        if type(user) is AnonymousUser or not user.id:
            raise PermissionError("Unauthorized")

        payment_methods = PaymentMethodStorage.get_all_available_payment_methods()
        gql_payment_methods = Query._build_payment_method_options(payment_methods)
        return PaymentMethodListGQLType(gql_payment_methods)

    def resolve_payment_gateway_config(self, info):
        return PaymentGatewayConfigGQLType(
            base_url=PayrollConfig.payment_gateway_base_url,
            api_key=PayrollConfig.payment_gateway_api_key,
            timeout=PayrollConfig.payment_gateway_timeout,
        )

    def resolve_csv_reconciliation_upload(self, info, **kwargs):
        Query._check_permissions(info.context.user, PayrollConfig.gql_csv_reconciliation_search_perms)
        filters = append_validity_filter(**kwargs)

        query = CsvReconciliationUpload.objects.filter(*filters)
        return gql_optimizer.query(query, info)

    def resolve_reconciliation_upload_row(self, info, **kwargs):
        Query._check_permissions(info.context.user, PayrollConfig.gql_csv_reconciliation_search_perms)
        filters = append_validity_filter(**kwargs)
        return gql_optimizer.query(ReconciliationUploadRow.objects.filter(*filters), info)

    def resolve_benefits_summary(self, info, **kwargs):
        Query._check_permissions(info.context.user,
                                 PayrollConfig.gql_payroll_search_perms)
        filters = append_validity_filter(**kwargs)
        filters.append(Q(
            individual__groupindividuals__recipient_type="PRIMARY",
            individual__groupindividuals__is_deleted=False,
        ))
        individual_id = kwargs.get("individualId", None)
        payroll_id = kwargs.get("payrollId", None)
        benefit_plan_uuid = kwargs.get("benefitPlanUuid", None)
        payment_cycle_uuid = kwargs.get("paymentCycleUuid", None)

        if individual_id:
            filters.append(Q(individual__id=individual_id))

        if payroll_id:
            filters.append(Q(payrollbenefitconsumption__payroll_id=payroll_id))

        if benefit_plan_uuid:
            filters.append(Q(payrollbenefitconsumption__payroll__payment_plan__benefit_plan_id=benefit_plan_uuid))

        if payment_cycle_uuid:
            filters.append(Q(payrollbenefitconsumption__payroll__payment_cycle_id=payment_cycle_uuid))

        benefits = BenefitConsumption.objects.filter(
            *filters,
            is_deleted=False,
            payrollbenefitconsumption__is_deleted=False,
        ).distinct()

        amount_received = benefits.filter(
            status=BenefitConsumptionStatus.RECONCILED,
        ).aggregate(total_received=Sum('amount'))['total_received'] or 0

        amount_due = benefits.exclude(
            status=BenefitConsumptionStatus.RECONCILED,
        ).aggregate(total_due=Sum('amount'))['total_due'] or 0

        return BenefitsSummaryGQLType(
            total_amount_received=amount_received,
            total_amount_due=amount_due,
        )

    @staticmethod
    def _build_payment_method_options(payment_methods):
        gql_payment_methods = []
        for payment_method in payment_methods:
            gql_payment_methods.append(
                PaymentMethodGQLType(
                    name=payment_method['name']
                )
            )
        return gql_payment_methods

    @staticmethod
    def _check_permissions(user, perms):
        if type(user) is AnonymousUser or not user.id or not user.has_perms(perms):
            raise PermissionError(_("Unauthorized"))


class Mutation(graphene.ObjectType):
    create_payment_point = CreatePaymentPointMutation.Field()
    update_payment_point = UpdatePaymentPointMutation.Field()
    delete_payment_point = DeletePaymentPointMutation.Field()

    create_payroll = CreatePayrollMutation.Field()
    create_consolidated_payroll = CreateConsolidatedPayrollMutation.Field()
    delete_payroll = DeletePayrollMutation.Field()
    close_payroll = ClosePayrollMutation.Field()
    reject_payroll = RejectPayrollMutation.Field()
    make_payment_for_payroll = MakePaymentForPayrollMutation.Field()
    delete_benefit_consumption = DeleteBenefitConsumptionMutation.Field()
