import graphene
import graphene_django_optimizer as gql_optimizer
from gettext import gettext as _
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
    RejectPayrollMutation, MakePaymentForPayrollMutation, DeleteBenefitConsumptionMutation
from payroll.gql_queries import BenefitConsumptionGQLType, PaymentPointGQLType, \
    PayrollGQLType, PaymentMethodGQLType, \
    PaymentMethodListGQLType, BenefitAttachmentListGQLType, \
    CsvReconciliationUploadGQLType, ReconciliationUploadRowGQLType, PayrollBenefitConsumptionGQLType, \
    PaymentGatewayConfigGQLType, BenefitsSummaryGQLType
from payroll.models import PaymentPoint, Payroll, \
    BenefitConsumption, BenefitAttachment, \
    CsvReconciliationUpload, ReconciliationUploadRow, PayrollBenefitConsumption, BenefitConsumptionStatus
from payroll.payments_registry import PaymentMethodStorage
from social_protection.models import BenefitPlan


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

        return gql_optimizer.query(BenefitConsumption.objects.filter(*filters), info)

    def resolve_benefit_attachment_by_payroll(self, info, **kwargs):
        Query._check_permissions(info.context.user, PayrollConfig.gql_payroll_search_perms)
        filters = [*append_validity_filter(**kwargs),
                   Q(benefit__payrollbenefitconsumption__payroll_id=kwargs.get("payroll_uuid"),
                     is_deleted=False,
                     benefit__payrollbenefitconsumption__is_deleted=False,
                     benefit__payrollbenefitconsumption__payroll__is_deleted=False)]

        client_mutation_id = kwargs.get("client_mutation_id", None)
        if client_mutation_id:
            wait_for_mutation(client_mutation_id)
            filters.append(Q(mutations__mutation__client_mutation_id=client_mutation_id))

        return gql_optimizer.query(BenefitAttachment.objects.filter(*filters), info)

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

        query = PayrollBenefitConsumption.objects.filter(*filters)
        return gql_optimizer.query(query, info)

    def resolve_benefit_consumption(self, info, **kwargs):
        Query._check_permissions(info.context.user, PayrollConfig.gql_payroll_search_perms)
        filters = append_validity_filter(**kwargs)

        client_mutation_id = kwargs.get("client_mutation_id")
        if client_mutation_id:
            wait_for_mutation(client_mutation_id)
            filters.append(Q(mutations__mutation__client_mutation_id=client_mutation_id))

        query = BenefitConsumption.objects.filter(*filters)
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

        amount_received = BenefitConsumption.objects.filter(
            *filters,
            is_deleted=False,
            payrollbenefitconsumption__is_deleted=False,
            status=BenefitConsumptionStatus.RECONCILED
        ).aggregate(total_received=Sum('amount'))['total_received'] or 0

        amount_due = BenefitConsumption.objects.filter(
            *filters,
            is_deleted=False,
            payrollbenefitconsumption__is_deleted=False
        ).exclude(status=BenefitConsumptionStatus.RECONCILED).aggregate(total_due=Sum('amount'))['total_due'] or 0

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
    delete_payroll = DeletePayrollMutation.Field()
    close_payroll = ClosePayrollMutation.Field()
    reject_payroll = RejectPayrollMutation.Field()
    make_payment_for_payroll = MakePaymentForPayrollMutation.Field()
    delete_benefit_consumption = DeleteBenefitConsumptionMutation.Field()
