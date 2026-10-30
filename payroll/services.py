import hashlib
import logging
from decimal import Decimal, InvalidOperation
import pandas as pd
from io import BytesIO

from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import transaction
from simple_history.utils import bulk_create_with_history
from django.db.models import Q, Sum
from django.utils.translation import gettext as _

from core import datetime
from core.custom_filters import CustomFilterWizardStorage
from core.models import InteractiveUser
from core.services import BaseService
from core.signals import register_service_signal
from invoice.models import Bill, BillItem, PaymentInvoice, DetailPaymentInvoice
from location.models import Location
from invoice.services import PaymentInvoiceService
from payment_cycle.models import PaymentCycle
from payroll.apps import PayrollConfig
from payroll.models import (
    PaymentPoint,
    Payroll,
    PayrollBenefitConsumption,
    BenefitConsumption,
    BenefitAttachment,
    BenefitConsumptionStatus,
    ConsolidatedPayrollMember,
    PayrollStatus,
    CsvReconciliationUpload,
    ReconciliationUploadRow
)
from payroll.tasks import send_requests_to_gateway_payment
from payroll.utils import PayrollNameGenerator
from payroll.validation import PaymentPointValidation, PayrollValidation, BenefitConsumptionValidation
from calculation.services import get_calculation_object
from core.services.utils import output_exception, check_authentication
from contribution_plan.models import PaymentPlan
from social_protection.models import Beneficiary, BeneficiaryStatus
from tasks_management.apps import TasksManagementConfig
from tasks_management.models import Task
from tasks_management.services import TaskService, _get_std_task_data_payload

logger = logging.getLogger(__name__)


class PaymentPointService(BaseService):
    OBJECT_TYPE = PaymentPoint

    def __init__(self, user, validation_class=PaymentPointValidation):
        super().__init__(user, validation_class)

    @register_service_signal('payment_point_service.create')
    def create(self, obj_data):
        return super().create(obj_data)

    @register_service_signal('payment_point_service.update')
    def update(self, obj_data):
        return super().update(obj_data)

    @register_service_signal('payment_point_service.delete')
    def delete(self, obj_data):
        return super().delete(obj_data)


class PayrollService(BaseService):
    OBJECT_TYPE = Payroll

    def __init__(self, user, validation_class=PayrollValidation):
        super().__init__(user, validation_class)

    @check_authentication
    @register_service_signal('payroll_service.create')
    def create(self, obj_data):
        try:
            with transaction.atomic():
                obj_data = self._adjust_create_payload(obj_data)
                from_failed_invoices_payroll_id = obj_data.pop("from_failed_invoices_payroll_id", None)
                payment_plan = self._get_payment_plan(obj_data)
                payment_cycle = self._get_payment_cycle(obj_data)
                project_names = self._get_project_names(obj_data)
                if not obj_data.get("name"):
                    obj_data["name"] = self._generate_payroll_name(
                        payment_plan, payment_cycle, project_names
                    )
                date_valid_from, date_valid_to = self._get_dates_parameter(obj_data)
                payroll, dict_representation = self._save_payroll(
                    obj_data, payment_plan, payment_cycle, project_names
                )
                if not bool(from_failed_invoices_payroll_id):
                    selected_projects = (obj_data.get("json_ext") or {}).get("filter_criteria", {}).get("project_ids", [])
                    if self._is_group_payment_plan(payment_plan):
                        enrollments_queryset = self._select_group_enrollments_based_on_criteria(
                            obj_data, payment_plan
                        )
                        if selected_projects and not enrollments_queryset.exists():
                            raise ValidationError(
                                "No active household beneficiaries with a primary recipient are assigned "
                                "to the selected completed project. Ensure the household assignments and "
                                "their primary recipients were saved before creating the payroll."
                            )
                        # The timesheet GROUP strategy creates one benefit per group
                        # enrollment. Passing the explicit enrollment queryset is
                        # essential: it limits the run to the chosen project instead
                        # of allowing the strategy to calculate every completed
                        # project in the benefit plan.
                        self._generate_benefits(
                            payment_plan,
                            date_valid_from,
                            date_valid_to,
                            payroll,
                            payment_cycle,
                            enrollments_queryset=enrollments_queryset,
                        )
                        self._assign_group_benefits_to_primary_recipients(
                            payroll, enrollments_queryset
                        )
                    else:
                        beneficiaries_queryset = self._select_beneficiary_based_on_criteria(
                            obj_data, payment_plan
                        )
                        if selected_projects and not beneficiaries_queryset.exists():
                            raise ValidationError(
                                "No active primary beneficiaries are assigned to the selected project. "
                                "Ensure its beneficiary assignments were saved and the assigned people are active "
                                "programme beneficiaries before creating the payroll."
                            )
                        self._generate_benefits(
                            payment_plan,
                            date_valid_from,
                            date_valid_to,
                            payroll,
                            payment_cycle,
                            beneficiaries_queryset=beneficiaries_queryset,
                        )
                        # Some calculation rules generate a benefit for every eligible
                        # beneficiary in the payment plan and do not apply the queryset
                        # passed above.  The payroll must still honour its saved project
                        # and location criteria, so retain only the generated benefits
                        # belonging to the selected beneficiaries.
                        self._enforce_beneficiary_scope(payroll, beneficiaries_queryset)
                else:
                    self._move_benefit_consumptions(payroll, from_failed_invoices_payroll_id)
                self.create_accept_payroll_task(payroll.id, obj_data)
                return dict_representation
        except Exception as exc:
            return output_exception(model_name=self.OBJECT_TYPE.__name__, method="create", exception=exc)

    @register_service_signal('payroll_service.update')
    def update(self, obj_data):
        raise NotImplementedError()

    @check_authentication
    @register_service_signal('payroll_service.delete')
    def delete(self, obj_data):
        payroll_to_delete = Payroll.objects.get(id=obj_data['id'])
        data = {'id': payroll_to_delete.id}
        TaskService(self.user).create({
            'source': 'payroll_delete',
            'entity': payroll_to_delete,
            'status': Task.Status.RECEIVED,
            'executor_action_event': TasksManagementConfig.default_executor_event,
            'business_event': PayrollConfig.payroll_delete_event,
            'data': _get_std_task_data_payload(data)
        })

    @check_authentication
    @register_service_signal('payroll_service.attach_benefit_to_payroll')
    def attach_benefit_to_payroll(self, payroll_id, benefit_id):
        active_links = PayrollBenefitConsumption.objects.filter(
            benefit_id=benefit_id,
            is_deleted=False,
        )
        if active_links.exclude(payroll_id=payroll_id).exists():
            raise ValidationError(
                "A benefit consumption can belong to only one active payroll."
            )
        if active_links.filter(payroll_id=payroll_id).exists():
            return
        payroll_benefit = PayrollBenefitConsumption(payroll_id=payroll_id, benefit_id=benefit_id)
        payroll_benefit.save(user=self.user)

    @register_service_signal('payroll_service.create_task')
    def create_accept_payroll_task(self, payroll_id, obj_data):
        payroll_to_accept = Payroll.objects.get(id=payroll_id)
        data = {**obj_data, 'id': payroll_id}
        TaskService(self.user).create({
            'source': 'payroll',
            'entity': payroll_to_accept,
            'status': Task.Status.RECEIVED,
            'executor_action_event': TasksManagementConfig.default_executor_event,
            'business_event': PayrollConfig.payroll_accept_event,
            'data': _get_std_task_data_payload(data)
        })

    @transaction.atomic
    def create_consolidated_payroll(self, district_id, benefit_plan_id, source_payroll_ids):
        """Create a parent payroll without cloning the source benefits."""
        if not source_payroll_ids:
            raise ValidationError("Select at least one approved payroll to consolidate.")
        sources = list(Payroll.objects.select_for_update().filter(
            id__in=source_payroll_ids,
            payment_plan__benefit_plan_id=benefit_plan_id,
            status=PayrollStatus.APPROVE_FOR_PAYMENT,
            payroll_type=Payroll.PayrollType.STANDARD,
            is_deleted=False,
        ).select_related("payment_plan", "payment_cycle", "payment_point"))
        if len(sources) != len(set(source_payroll_ids)):
            raise ValidationError("Every selected payroll must still be approved and belong to the selected phase.")
        if ConsolidatedPayrollMember.objects.filter(
                source_payroll_id__in=[source.id for source in sources], is_deleted=False).exists():
            raise ValidationError("A selected payroll already belongs to a consolidated payroll.")
        first = sources[0]
        if any((source.payment_method, source.payment_point_id, source.payment_cycle_id) !=
               (first.payment_method, first.payment_point_id, first.payment_cycle_id) for source in sources):
            raise ValidationError("Selected payrolls must use the same payment method, point, and cycle.")
        district = Location.objects.get(uuid=district_id, is_deleted=False)
        phase = first.payment_plan.benefit_plan.name or first.payment_plan.benefit_plan.code
        name_base = f"{district.name}-{phase}-Consolidated Payroll"
        sequence = Payroll.objects.filter(name__startswith=name_base, is_deleted=False).count() + 1
        payroll = Payroll(
            name=f"{name_base}-{sequence:03d}", payment_plan=first.payment_plan,
            payment_cycle=first.payment_cycle, payment_point=first.payment_point,
            payment_method=first.payment_method, status=PayrollStatus.PENDING_APPROVAL,
            payroll_type=Payroll.PayrollType.CONSOLIDATED, district=district,
            json_ext={"consolidated_source_payroll_ids": [str(source.id) for source in sources]},
        )
        payroll.save(username=self.user.login_name)
        for source in sources:
            benefits = PayrollBenefitConsumption.objects.filter(payroll=source, is_deleted=False)
            benefit_ids = list(benefits.values_list("benefit_id", flat=True))
            total = BenefitConsumption.objects.filter(id__in=benefit_ids).aggregate(total=Sum("amount"))["total"] or 0
            ConsolidatedPayrollMember(
                consolidated_payroll=payroll, source_payroll=source,
                beneficiaries_count=len(benefit_ids), total_amount=total,
            ).save(username=self.user.login_name)
            for benefit_id in benefit_ids:
                PayrollBenefitConsumption(payroll=payroll, benefit_id=benefit_id).save(username=self.user.login_name)
        self.create_accept_payroll_task(payroll.id, {
            "id": payroll.id, "district": district.name, "phase": phase,
            "included_payrolls": [{"id": str(source.id), "name": source.name} for source in sources],
            "consolidated": True,
        })
        return payroll

    @register_service_signal('payroll_service.close_payroll')
    def close_payroll(self, obj_data):
        payroll_to_close = Payroll.objects.get(id=obj_data['id'])
        data = {'id': payroll_to_close.id}
        TaskService(self.user).create({
            'source': 'payroll_reconciliation',
            'entity': payroll_to_close,
            'status': Task.Status.RECEIVED,
            'executor_action_event': TasksManagementConfig.default_executor_event,
            'business_event': PayrollConfig.payroll_reconciliation_event,
            'data': _get_std_task_data_payload(data)
        })

    @register_service_signal('payroll_service.reject_approve_payroll')
    def reject_approved_payroll(self, obj_data):
        payroll_to_reject = Payroll.objects.get(id=obj_data['id'])
        data = {'id': payroll_to_reject.id}
        TaskService(self.user).create({
            'source': 'payroll_reject',
            'entity': payroll_to_reject,
            'status': Task.Status.RECEIVED,
            'executor_action_event': TasksManagementConfig.default_executor_event,
            'business_event': PayrollConfig.payroll_reject_event,
            'data': _get_std_task_data_payload(data)
        })

    def make_payment_for_payroll(self, obj_data):
        payroll_id = obj_data['id']
        send_requests_to_gateway_payment.delay(payroll_id, self.user.id)

    def _save_payroll(self, obj_data, payment_plan, payment_cycle, project_names):
        obj_ = self.OBJECT_TYPE(**obj_data)
        dict_representation = self.save_instance(obj_)
        payroll_id = dict_representation["data"]["id"]
        payroll = Payroll.objects.get(id=payroll_id)
        return payroll, dict_representation

    def _get_payment_plan(self, obj_data):
        payment_plan_id = obj_data.get("payment_plan_id")
        payment_plan = PaymentPlan.objects.get(id=payment_plan_id)
        return payment_plan

    def _get_payment_cycle(self, obj_data):
        payment_cycle_id = obj_data.get("payment_cycle_id")
        # Serialise creations in a cycle so the readable sequence is safe when
        # two payrolls with identical criteria are submitted concurrently.
        payment_cycle = PaymentCycle.objects.select_for_update().get(id=payment_cycle_id)
        return payment_cycle

    def _get_project_names(self, obj_data):
        json_ext = obj_data.get("json_ext") or {}
        project_ids = json_ext.get("filter_criteria", {}).get("project_ids", [])
        if not project_ids:
            return ["ALL"]

        project_model = apps.get_model("project_social_protection", "Project")
        project_names = list(
            project_model.objects.filter(id__in=project_ids, is_deleted=False)
            .order_by("name")
            .values_list("name", flat=True)
        )
        if not project_names:
            raise ValidationError("The selected project no longer exists.")
        return project_names

    def _generate_payroll_name(self, payment_plan, payment_cycle, project_names):
        for sequence in range(1, PayrollNameGenerator.GENERATION_ATTEMPTS + 1):
            name = PayrollNameGenerator.generate(
                payment_plan, payment_cycle, project_names, sequence
            )
            if not Payroll.objects.filter(name=name, is_deleted=False).exists():
                return name
        raise ValueError("Unable to generate a unique payroll name, please retry.")

    def _get_dates_parameter(self, obj_data):
        date_valid_from = obj_data.get('date_valid_from', None)
        date_valid_to = obj_data.get('date_valid_to', None)
        return date_valid_from, date_valid_to

    @staticmethod
    def _is_group_payment_plan(payment_plan):
        return payment_plan.benefit_plan.type == "GROUP"

    def _select_group_enrollments_based_on_criteria(self, obj_data, payment_plan):
        """Return the eligible household enrollments for a GROUP payroll.

        A household is paid once, through its primary recipient, so the unit of
        selection must be ``GroupBeneficiaryProjectEnrollment`` rather than the
        individual ``Beneficiary`` model used by individual benefit plans.
        """
        enrollment_model = apps.get_model(
            "project_social_protection", "GroupBeneficiaryProjectEnrollment"
        )
        json_ext = obj_data.get("json_ext", {})
        filter_criteria = json_ext.get("filter_criteria", {})
        enrollments_queryset = enrollment_model.objects.filter(
            group_beneficiary__benefit_plan_id=payment_plan.benefit_plan.id,
            group_beneficiary__status=BeneficiaryStatus.ACTIVE,
            group_beneficiary__is_deleted=False,
            group_beneficiary__group__groupindividuals__recipient_type="PRIMARY",
            group_beneficiary__group__groupindividuals__is_deleted=False,
            project__status="COMPLETED",
            project__is_deleted=False,
            is_deleted=False,
        )

        project_ids = filter_criteria.get("project_ids", [])
        if project_ids:
            enrollments_queryset = enrollments_queryset.filter(project__id__in=project_ids)

        location_ids = filter_criteria.get("location_ids", [])
        if location_ids:
            enrollments_queryset = enrollments_queryset.filter(
                Q(group_beneficiary__group__location__uuid__in=location_ids)
                | Q(group_beneficiary__group__location__parent__uuid__in=location_ids)
                | Q(group_beneficiary__group__location__parent__parent__uuid__in=location_ids)
                | Q(group_beneficiary__group__location__parent__parent__parent__uuid__in=location_ids)
            )

        return enrollments_queryset.distinct()

    def _select_beneficiary_based_on_criteria(self, obj_data, payment_plan):
        json_ext = obj_data.get("json_ext", {})

        beneficiaries_queryset = Beneficiary.objects.filter(
            benefit_plan__id=payment_plan.benefit_plan.id,
            status=BeneficiaryStatus.ACTIVE,
            is_deleted=False,
        )

        filter_criteria = json_ext.get("filter_criteria", {})

        # Payrolls are paid to the primary recipient for a household, never
        # to every member of the household.
        beneficiaries_queryset = beneficiaries_queryset.filter(
            individual__groupindividuals__recipient_type="PRIMARY",
            individual__groupindividuals__is_deleted=False,
        )

        project_ids = filter_criteria.get("project_ids", [])
        if project_ids:
            # A project payroll must use the beneficiaries explicitly assigned
            # to that project.  A micro-catchment describes where an activity
            # takes place; it is not a payment entitlement and must never pull
            # every eligible beneficiary from the whole catchment.
            beneficiaries_queryset = beneficiaries_queryset.filter(
                project_enrollments__project__id__in=project_ids,
                project_enrollments__is_deleted=False,
            )

        location_ids = filter_criteria.get("location_ids", [])
        if location_ids:
            beneficiaries_queryset = beneficiaries_queryset.filter(
                Q(individual__location__uuid__in=location_ids)
                | Q(individual__location__parent__uuid__in=location_ids)
                | Q(individual__location__parent__parent__uuid__in=location_ids)
                | Q(individual__location__parent__parent__parent__uuid__in=location_ids)
            )

        custom_filters = [
            criterion["custom_filter_condition"]
            for criterion in json_ext.get("advanced_criteria", [])
        ]
        if custom_filters:
            beneficiaries_queryset = CustomFilterWizardStorage.build_custom_filters_queryset(
                PayrollConfig.name,
                "BenefitPlan",
                custom_filters,
                beneficiaries_queryset,
            )

        return beneficiaries_queryset.distinct()

    def _generate_benefits(
        self,
        payment_plan,
        date_from,
        date_to,
        payroll,
        payment_cycle,
        beneficiaries_queryset=None,
        enrollments_queryset=None,
    ):
        calculation = get_calculation_object(payment_plan.calculation)
        calculation_kwargs = {
            "user_id": self.user.id,
            "start_date": date_from,
            "end_date": date_to,
            "payroll": payroll,
            "payment_cycle": payment_cycle,
        }
        if beneficiaries_queryset is not None:
            calculation_kwargs["beneficiaries_queryset"] = beneficiaries_queryset
        if enrollments_queryset is not None:
            calculation_kwargs["enrollments_queryset"] = enrollments_queryset
        calculation.calculate_if_active_for_object(
            payment_plan,
            **calculation_kwargs,
        )

    def _assign_group_benefits_to_primary_recipients(self, payroll, enrollments_queryset):
        """Make the primary recipient the payee for every household benefit.

        Timesheet bills deliberately remain attributed to the household because
        the work and entitlement belong to that household.  The benefit record,
        however, is the payment instruction consumed by the PSP and must name
        the household's selected primary recipient (which can differ from the
        household head).
        """
        group_individual_model = apps.get_model("individual", "GroupIndividual")
        group_ids_by_beneficiary = dict(
            enrollments_queryset.values_list("group_beneficiary_id", "group_beneficiary__group_id")
        )
        primary_recipient_by_group = dict(
            group_individual_model.objects.filter(
                group_id__in=group_ids_by_beneficiary.values(),
                recipient_type="PRIMARY",
                is_deleted=False,
            ).values_list("group_id", "individual_id")
        )
        primary_recipient_by_beneficiary = {
            str(group_beneficiary_id): primary_recipient_by_group.get(group_id)
            for group_beneficiary_id, group_id in group_ids_by_beneficiary.items()
        }
        if any(recipient_id is None for recipient_id in primary_recipient_by_beneficiary.values()):
            raise ValidationError(
                "Every household selected for payroll must have a primary recipient."
            )

        attachments = BenefitAttachment.objects.filter(
            benefit__payrollbenefitconsumption__payroll=payroll,
            benefit__payrollbenefitconsumption__is_deleted=False,
            is_deleted=False,
        ).select_related("benefit", "bill")
        benefits_updated = set()
        for attachment in attachments:
            recipient_id = primary_recipient_by_beneficiary.get(str(attachment.bill.subject_id))
            if recipient_id is None:
                # Benefits unrelated to this group calculation must never be
                # reassigned as a side effect of creating the payroll.
                continue
            attachment.benefit.individual_id = recipient_id
            attachment.benefit.save(user=self.user)
            benefits_updated.add(attachment.benefit_id)

        expected_benefit_ids = set(
            PayrollBenefitConsumption.objects.filter(
                payroll=payroll,
                is_deleted=False,
            ).values_list("benefit_id", flat=True)
        )
        if expected_benefit_ids != benefits_updated:
            raise ValidationError(
                "Unable to assign every household benefit to its primary recipient."
            )

    @staticmethod
    def _enforce_beneficiary_scope(payroll, beneficiaries_queryset):
        allowed_individual_ids = beneficiaries_queryset.values("individual_id")
        out_of_scope_links = PayrollBenefitConsumption.objects.filter(
            payroll=payroll,
            is_deleted=False,
        ).exclude(
            benefit__individual_id__in=allowed_individual_ids,
        )
        benefit_data = list(out_of_scope_links.values_list(
            "benefit_id", "benefit__benefitattachment__bill_id",
        ))
        benefit_ids = {benefit_id for benefit_id, _ in benefit_data}
        bill_ids = {bill_id for _, bill_id in benefit_data if bill_id}
        if not benefit_ids:
            return

        BenefitAttachment.objects.filter(benefit_id__in=benefit_ids).delete()
        BillItem.objects.filter(bill_id__in=bill_ids).delete()
        Bill.objects.filter(id__in=bill_ids).delete()
        out_of_scope_links.delete()
        BenefitConsumption.objects.filter(id__in=benefit_ids, is_deleted=False).delete()

    @transaction.atomic
    def _move_benefit_consumptions(self, payroll, from_payroll_id):
        payroll_benefits = PayrollBenefitConsumption.objects.filter(
            payroll_id=from_payroll_id,
            benefit__status__in=[BenefitConsumptionStatus.ACCEPTED, BenefitConsumptionStatus.APPROVE_FOR_PAYMENT]
        )
        payroll_benefits.update(payroll=payroll)
        benefits = BenefitConsumption.objects.filter(payrollbenefitconsumption__payroll=payroll)
        benefits.update(status=BenefitConsumptionStatus.ACCEPTED)


class BenefitConsumptionService(BaseService):
    OBJECT_TYPE = BenefitConsumption

    def __init__(self, user, validation_class=BenefitConsumptionValidation):
        super().__init__(user, validation_class)

    @check_authentication
    @register_service_signal('benefit_consumption_service.create')
    def create(self, obj_data):
        return super().create(obj_data)

    @register_service_signal('benefit_consumption_service.update')
    def update(self, obj_data):
        return super().update(obj_data)

    @check_authentication
    @register_service_signal('benefit_consumption_service.delete')
    def delete(self, obj_data):
        benefit_to_delete = BenefitConsumption.objects.get(id=obj_data['id'])
        benefit_to_delete.status = BenefitConsumptionStatus.PENDING_DELETION
        benefit_to_delete.save(user=self.user)
        data = {'id': benefit_to_delete.id}
        TaskService(self.user).create({
            'source': 'benefit_delete',
            'entity': benefit_to_delete,
            'status': Task.Status.RECEIVED,
            'executor_action_event': TasksManagementConfig.default_executor_event,
            'business_event': PayrollConfig.benefit_delete_event,
            'data': _get_std_task_data_payload(data)
        })

    @check_authentication
    @register_service_signal('benefit_consumption_service.create_or_update_benefit_attachment')
    def create_or_update_benefit_attachment(self, bills_queryset, benefit_id):
        # remove first old attachments and save the new one
        BenefitAttachment.objects.filter(benefit_id=benefit_id).delete()
        # save new bill attachments
        for bill in bills_queryset:
            benefit_attachment = BenefitAttachment(bill_id=bill.id, benefit_id=benefit_id)
            benefit_attachment.save(user=self.user)


class CsvReconciliationService:

    def __init__(self, user: InteractiveUser):
        self.user = user

    @property
    def micro_catchment_column(self):
        # Existing deployments can have a persisted module configuration that
        # predates this setting, so retain the historical header until it is
        # explicitly configured.
        return PayrollConfig.csv_reconciliation_micro_catchment_column or "Micro Catchment"

    def download_reconciliation(self, payroll_id) -> BytesIO:
        payroll = self._resolve_payroll(payroll_id)
        bc_qs = self._get_benefit_consumption_qs(payroll)
        field_mapping = PayrollConfig.csv_reconciliation_field_mapping
        records = list(bc_qs.values(*field_mapping.keys()))
        benefits_by_code = {
            benefit.code: benefit
            for benefit in bc_qs.select_related(
                "individual__location__parent__parent__parent"
            )
        }

        extra_info_keys = set()
        extra_info_dicts = []
        mapped_headers = set(field_mapping.values()) | {self.micro_catchment_column}
        project_micro_catchment = self._get_payroll_micro_catchment(payroll)
        source_payroll_by_benefit = {}
        if payroll.payroll_type == Payroll.PayrollType.CONSOLIDATED:
            member_ids = ConsolidatedPayrollMember.objects.filter(
                consolidated_payroll=payroll, is_deleted=False,
            ).values_list("source_payroll_id", flat=True)
            source_payroll_by_benefit = dict(PayrollBenefitConsumption.objects.filter(
                payroll_id__in=member_ids, is_deleted=False,
            ).select_related("payroll").values_list("benefit_id", "payroll__name"))
        for record in records:
            benefit = benefits_by_code[record["code"]]
            # The payroll's project is the work location. A recipient may live
            # in a different catchment, but that must not make a one-project
            # payroll look as though it contains several project catchments.
            record["micro_catchment"] = (
                project_micro_catchment or self._get_micro_catchment(benefit)
            )
            extra_info = benefit.json_ext.get("extra_info", {}) if benefit.json_ext else {}
            extra_info_keys.update(key for key in extra_info if key not in mapped_headers)
            extra_info_dicts.append(extra_info)
            if source_payroll_by_benefit:
                record["Source Payroll"] = source_payroll_by_benefit.get(benefit.id)

        df = pd.DataFrame.from_records(records)
        df[PayrollConfig.csv_reconciliation_paid_extra_field] = df.apply(
            lambda row: self._fill_paid_column(row), axis=1
        )
        df.rename(
            columns={**field_mapping, "micro_catchment": self.micro_catchment_column},
            inplace=True,
        )
        export_columns = list(field_mapping.values())
        try:
            export_columns.insert(export_columns.index("District Name") + 1, self.micro_catchment_column)
        except ValueError:
            export_columns.append(self.micro_catchment_column)
        export_columns.append(PayrollConfig.csv_reconciliation_paid_extra_field)
        if source_payroll_by_benefit:
            export_columns.append("Source Payroll")
        for key in extra_info_keys:
            df[key] = [extra_info.get(key) for extra_info in extra_info_dicts]
            export_columns.append(key)
        df = df.reindex(columns=export_columns)

        in_memory_file = BytesIO()
        df.to_csv(in_memory_file, index=False)
        return in_memory_file

    @staticmethod
    def _get_payroll_micro_catchment(payroll):
        """Return the selected project's catchment for a single-project payroll."""
        project_ids = {
            str(project_id)
            for project_id in (payroll.json_ext or {}).get("filter_criteria", {}).get("project_ids", [])
            if project_id
        }
        if len(project_ids) != 1:
            return None
        project_model = apps.get_model("project_social_protection", "Project")
        project = project_model.objects.filter(
            id=next(iter(project_ids)), is_deleted=False,
        ).select_related("micro_catchment").first()
        return project.micro_catchment.name if project and project.micro_catchment else None

    def _get_micro_catchment(self, benefit):
        location = benefit.individual.location
        while location and location.type != "W":
            location = location.parent
        if not location:
            return None
        from location.models import MicroCatchmentGVH
        micro_catchment = (
            MicroCatchmentGVH.objects.filter(location=location, validity_to__isnull=True)
            .select_related("micro_catchment")
            .order_by("micro_catchment__name")
            .first()
        )
        return micro_catchment.micro_catchment.name if micro_catchment else None

    @transaction.atomic
    def upload_reconciliation(self, payroll_id, file, upload):
        """Validate and audit an upload without changing payment records.

        Stage 2 will explicitly apply reviewed rows.  Keeping this operation
        validation-only ensures a file can be inspected before finance data is
        changed.
        """
        payroll = self._resolve_payroll(payroll_id)
        # Serialise uploads for a payroll.  This makes superseding a pending
        # review deterministic even when corrected files arrive concurrently.
        payroll = Payroll.objects.select_for_update().get(id=payroll.id)
        if not file:
            raise ValueError(_("csv_reconciliation.validation.file_required"))

        contents = file.read()
        if not contents:
            raise ValueError(_("Import file is empty"))

        upload.payroll = payroll
        upload.checksum = hashlib.sha256(contents).hexdigest()
        upload.status = CsvReconciliationUpload.Status.VALIDATING
        upload.save(username=self.user.login_name)

        duplicate = CsvReconciliationUpload.objects.filter(
            checksum=upload.checksum, is_deleted=False,
        ).exclude(
            id=upload.id,
        ).exclude(
            status__in=[
                CsvReconciliationUpload.Status.FAIL,
                CsvReconciliationUpload.Status.DUPLICATE,
            ],
        ).order_by("date_created").first()
        if duplicate:
            upload.duplicate_of = duplicate
            upload.status = CsvReconciliationUpload.Status.DUPLICATE
            upload.error = {"duplicate_file": {
                "upload_id": str(duplicate.id), "file_name": duplicate.file_name,
            }}
            upload.save(username=self.user.login_name)
            return BytesIO(contents), upload.error, self._upload_summary(upload)

        try:
            df = pd.read_csv(BytesIO(contents), dtype=str, keep_default_na=False)
        except Exception as exc:
            raise ValueError(_("Unable to read CSV file: %(error)s") % {"error": str(exc)})

        self._validate_dataframe(df)
        df.rename(
            columns={v: k for k, v in PayrollConfig.csv_reconciliation_field_mapping.items()},
            inplace=True,
        )
        code_column = PayrollConfig.csv_reconciliation_code_column
        national_id_column = next(
            (field for field, header in PayrollConfig.csv_reconciliation_field_mapping.items()
             if header == "National ID"),
            None,
        )
        codes = df[code_column].map(self._clean_value)
        national_ids = (
            df[national_id_column].map(self._clean_value)
            if national_id_column and national_id_column in df.columns
            else pd.Series([None] * len(df), index=df.index)
        )
        duplicate_codes = set(codes[codes.notna() & codes.duplicated(keep=False)])
        duplicate_national_ids = set(
            national_ids[national_ids.notna() & national_ids.duplicated(keep=False)]
        )

        benefits_by_code, contexts_by_benefit_id = self._load_upload_context(payroll, codes)
        source_payroll_by_benefit = {}
        if payroll.payroll_type == Payroll.PayrollType.CONSOLIDATED:
            member_ids = ConsolidatedPayrollMember.objects.filter(
                consolidated_payroll=payroll, is_deleted=False,
            ).values_list("source_payroll_id", flat=True)
            source_payroll_by_benefit = dict(PayrollBenefitConsumption.objects.filter(
                payroll_id__in=member_ids, is_deleted=False,
            ).values_list("benefit_id", "payroll_id"))
        errors = {}
        valid_indexes = []
        upload_rows = []
        for index, row in df.iterrows():
            row_number = index + 2
            code = self._clean_value(row.get(code_column))
            national_id = self._clean_value(row.get(national_id_column)) if national_id_column else None
            benefit, reasons = self._validate_upload_row(
                payroll, row, code, national_id, duplicate_codes, duplicate_national_ids,
                benefits_by_code, contexts_by_benefit_id,
            )
            upload_row = ReconciliationUploadRow(
                upload=upload,
                user_created=self.user,
                user_updated=self.user,
                row_number=row_number,
                benefit=benefit,
                code=code,
                national_id=national_id,
                submitted_data={
                    **{str(key): self._json_value(value) for key, value in row.items()},
                    **({"_source_payroll_id": str(source_payroll_by_benefit[benefit.id])}
                       if benefit and benefit.id in source_payroll_by_benefit else {}),
                },
                mismatch_reasons=reasons,
                status=(ReconciliationUploadRow.Status.INVALID if self._has_blocking_reason(reasons)
                        else ReconciliationUploadRow.Status.VALID),
            )
            # HistoryModel deliberately has no database UUID default.  These rows
            # are inserted in bulk, so assign the primary key before bulk_create.
            upload_row.set_pk()
            upload_rows.append(upload_row)
            if self._has_blocking_reason(reasons):
                errors[str(row_number)] = reasons
            else:
                valid_indexes.append(index)

        paid_column = PayrollConfig.csv_reconciliation_paid_extra_field
        valid_rows = df.loc[valid_indexes]
        upload.total_records = len(df)
        upload.unmatched_records = len(errors)
        upload.matched_records = len(valid_indexes)
        upload.paid_records = int(
            (valid_rows[paid_column].map(self._clean_value)
             == PayrollConfig.csv_reconciliation_paid_yes).sum()
        )
        upload.unpaid_records = int(
            (valid_rows[paid_column].map(self._clean_value)
             == PayrollConfig.csv_reconciliation_paid_no).sum()
        )
        bulk_create_with_history(upload_rows, ReconciliationUploadRow, default_user=self.user, batch_size=1000)

        # A corrected file replaces, rather than competes with, an outstanding
        # review. Fail its still-open checker task as well, so it cannot later
        # look completed even though its upload was not applied.
        pending_uploads = CsvReconciliationUpload.objects.filter(
            payroll=payroll,
            status=CsvReconciliationUpload.Status.WAITING_FOR_VERIFICATION,
            is_deleted=False,
        ).exclude(id=upload.id)
        for pending_upload in pending_uploads:
            pending_upload.status = CsvReconciliationUpload.Status.FAIL
            pending_upload.error = {
                **(pending_upload.error or {}),
                "superseded_by": {
                    "upload_id": str(upload.id),
                    "file_name": upload.file_name,
                },
            }
            pending_upload.save(username=self.user.login_name)
            pending_tasks = Task.objects.filter(
                entity_id=str(payroll.id),
                business_event=PayrollConfig.payroll_reconciliation_event,
                data__upload_id=str(pending_upload.id),
                status__in=[Task.Status.RECEIVED, Task.Status.ACCEPTED],
            )
            for pending_task in pending_tasks:
                TaskService(self.user).complete_task({"id": pending_task.id, "failed": True})

        upload.error = errors
        upload.status = CsvReconciliationUpload.Status.WAITING_FOR_VERIFICATION
        upload.json_ext = {"extra_info": self._upload_summary(upload)}
        upload.save(username=self.user.login_name)
        task_context = self._reconciliation_task_context(payroll)
        task = TaskService(self.user).create({
            "source": "payroll_reconciliation", "entity": payroll,
            "status": Task.Status.RECEIVED,
            "executor_action_event": TasksManagementConfig.default_executor_event,
            "business_event": PayrollConfig.payroll_reconciliation_event,
            "data": _get_std_task_data_payload({
                "id": payroll.id,
                "upload_id": upload.id,
                "payroll_name": payroll.name,
                "included_payrolls": [{"id": str(payroll.id), "name": payroll.name}],
                "district": task_context["district"],
                "phase": task_context["phase"],
                "summary": self._upload_summary(upload),
            }),
        })
        if not task.get("success", False):
            raise ValueError(task.get("error") or "Unable to create reconciliation review task")
        return BytesIO(contents), errors or None, self._upload_summary(upload)

    @staticmethod
    def _reconciliation_task_context(payroll):
        """Return the selected-project district and the payroll's benefit-plan phase."""
        project_ids = (payroll.json_ext or {}).get("filter_criteria", {}).get("project_ids", [])
        project_model = apps.get_model("project_social_protection", "Project")
        projects = project_model.objects.filter(
            id__in=project_ids,
            is_deleted=False,
        ).select_related("location__parent__parent__parent")

        districts = set()
        for project in projects:
            location = project.location
            while location:
                if location.type == "R":
                    districts.add(location.name)
                    break
                location = location.parent

        payment_plan = payroll.payment_plan
        benefit_plan = payment_plan.benefit_plan if payment_plan else None
        return {
            "district": ", ".join(sorted(districts)) or None,
            "phase": benefit_plan.name if benefit_plan else None,
        }

    def _load_upload_context(self, payroll, codes):
        benefits = list(
            BenefitConsumption.objects.filter(
                code__in=list(codes.dropna().unique()),
                is_deleted=False,
                payrollbenefitconsumption__payroll=payroll,
                payrollbenefitconsumption__is_deleted=False,
                individual__groupindividuals__recipient_type="PRIMARY",
                individual__groupindividuals__is_deleted=False,
            ).select_related("individual", "individual__location").distinct()
        )
        benefits_by_code = {benefit.code: benefit for benefit in benefits}
        project_micro_catchment = self._get_payroll_micro_catchment(payroll)
        if project_micro_catchment:
            return benefits_by_code, {
                benefit.id: {"micro_catchment": project_micro_catchment}
                for benefit in benefits
            }

        # A historical multi-project payroll has no single project catchment.
        # Preserve its legacy recipient-location behavior rather than inventing
        # a catchment that cannot be determined from the payroll criteria.
        from location.models import Location, MicroCatchmentGVH
        locations = {
            benefit.individual.location_id: benefit.individual.location
            for benefit in benefits if benefit.individual.location_id
        }
        while True:
            parent_ids = {
                location.parent_id for location in locations.values()
                if location.parent_id and location.parent_id not in locations
            }
            if not parent_ids:
                break
            parents = {location.id: location for location in Location.objects.filter(id__in=parent_ids)}
            if not parents:
                break
            locations.update(parents)

        gvh_by_benefit = {}
        for benefit in benefits:
            location = locations.get(benefit.individual.location_id)
            while location and location.type != "W":
                location = locations.get(location.parent_id)
            gvh_by_benefit[benefit.id] = location.id if location else None

        micro_catchments = {}
        for item in MicroCatchmentGVH.objects.filter(
            location_id__in={gvh_id for gvh_id in gvh_by_benefit.values() if gvh_id},
            validity_to__isnull=True,
        ).select_related("micro_catchment").order_by("micro_catchment__name"):
            micro_catchments.setdefault(item.location_id, item.micro_catchment.name)
        contexts = {
            benefit.id: {"micro_catchment": micro_catchments.get(gvh_by_benefit[benefit.id])}
            for benefit in benefits
        }
        return benefits_by_code, contexts

    @transaction.atomic
    def apply_upload(self, upload):
        upload = CsvReconciliationUpload.objects.select_for_update().get(id=upload.id)
        if upload.status != CsvReconciliationUpload.Status.WAITING_FOR_VERIFICATION:
            return
        rows = list(ReconciliationUploadRow.objects.filter(
            upload=upload,
            status=ReconciliationUploadRow.Status.VALID,
            is_deleted=False,
        ))
        paid_rows = [
            row for row in rows
            if self._clean_value(row.submitted_data.get(PayrollConfig.csv_reconciliation_paid_extra_field))
            == PayrollConfig.csv_reconciliation_paid_yes
        ]
        locked_benefits = {
            benefit.id: benefit
            for benefit in BenefitConsumption.objects.select_for_update().filter(
                id__in=[row.benefit_id for row in paid_rows if row.benefit_id],
                is_deleted=False,
            )
        }
        for row in paid_rows:
            benefit = locked_benefits.get(row.benefit_id)
            if benefit and benefit.status == BenefitConsumptionStatus.ACCEPTED:
                self._reconcile_bc(pd.Series(row.submitted_data), benefit)
        upload.status = CsvReconciliationUpload.Status.PARTIAL_SUCCESS if upload.unmatched_records else CsvReconciliationUpload.Status.SUCCESS
        upload.save(username=self.user.login_name)

        # A payroll is fully reconciled only when this approved upload has no
        # unmatched rows and every linked benefit has reached RECONCILED.
        # In particular, an all-unpaid or partial upload must leave the payroll
        # available for a later reconciliation upload.
        payroll = upload.payroll
        has_unreconciled_benefits = PayrollBenefitConsumption.objects.filter(
            payroll=payroll,
            is_deleted=False,
        ).exclude(benefit__status=BenefitConsumptionStatus.RECONCILED).exists()
        if paid_rows and not upload.unmatched_records and not has_unreconciled_benefits \
                and payroll.status != PayrollStatus.RECONCILED:
            payroll.status = PayrollStatus.RECONCILED
            payroll.save(username=self.user.login_name)
            if payroll.payroll_type == Payroll.PayrollType.CONSOLIDATED:
                for member in ConsolidatedPayrollMember.objects.filter(
                        consolidated_payroll=payroll, is_deleted=False).select_related("source_payroll"):
                    if not PayrollBenefitConsumption.objects.filter(
                            payroll=member.source_payroll, is_deleted=False).exclude(
                                benefit__status=BenefitConsumptionStatus.RECONCILED).exists():
                        member.source_payroll.status = PayrollStatus.RECONCILED
                        member.source_payroll.save(username=self.user.login_name)

    def download_upload_review(self, upload, contents):
        """Return the original CSV annotated with its saved validation result."""
        try:
            df = pd.read_csv(BytesIO(contents), dtype=str, keep_default_na=False)
        except Exception:
            return BytesIO(contents)

        row_results = {
            row.row_number: row
            for row in ReconciliationUploadRow.objects.filter(upload=upload, is_deleted=False)
        }
        validation_status = []
        mismatch_reasons = []
        matched_records = []
        paid_records = []
        unpaid_records = []
        unmatched_records = []
        paid_column = PayrollConfig.csv_reconciliation_paid_extra_field

        for index, row in df.iterrows():
            result = row_results.get(index + 2)
            reasons = result.mismatch_reasons if result else []
            is_valid = result and result.status == ReconciliationUploadRow.Status.VALID
            paid_value = self._clean_value(row.get(paid_column))
            validation_status.append(result.status if result else "NOT_VALIDATED")
            mismatch_reasons.append("; ".join(reasons) if reasons else "")
            matched_records.append("Yes" if is_valid else "")
            paid_records.append("Yes" if is_valid and paid_value == PayrollConfig.csv_reconciliation_paid_yes else "")
            unpaid_records.append("Yes" if is_valid and paid_value == PayrollConfig.csv_reconciliation_paid_no else "")
            unmatched_records.append("Yes" if result and not is_valid else "")

        df["Validation Status"] = validation_status
        df["Mismatch Reasons"] = mismatch_reasons
        df["Matched Record"] = matched_records
        df["Paid Record"] = paid_records
        df["Unpaid Record"] = unpaid_records
        df["Unmatched Record"] = unmatched_records
        review_file = BytesIO()
        df.to_csv(review_file, index=False)
        review_file.seek(0)
        return review_file

    def _validate_upload_row(self, payroll, row, code, national_id, duplicate_codes, duplicate_national_ids, benefits_by_code=None, contexts_by_benefit_id=None):
        reasons = []
        if not code:
            return None, ["missing_code"]
        if code in duplicate_codes:
            reasons.append("duplicate_code_in_file")
        if national_id and national_id in duplicate_national_ids:
            reasons.append("duplicate_national_id_in_file")

        benefit = (benefits_by_code or {}).get(code)
        if benefit is None and benefits_by_code is None:
            benefit = BenefitConsumption.objects.filter(
                code=code,
                is_deleted=False,
                individual__groupindividuals__recipient_type="PRIMARY",
                individual__groupindividuals__is_deleted=False,
            ).select_related("individual").first()
        if not benefit:
            return None, reasons + ["code_not_found"]
        if benefits_by_code is None and not benefit.payrollbenefitconsumption_set.filter(payroll=payroll).exists():
            reasons.append("code_not_in_payroll")

        individual = benefit.individual
        context = (contexts_by_benefit_id or {}).get(benefit.id)
        if context is None:
            context = {"micro_catchment": self._get_micro_catchment(benefit)}
        checks = (
            ("payroll_name_mismatch", "payrollbenefitconsumption__payroll__name", payroll.name, self._matches_text),
            ("payroll_status_mismatch", "payrollbenefitconsumption__payroll__status", payroll.status, self._matches_text),
            ("first_name_mismatch", "individual__first_name", individual.first_name, self._matches_text),
            ("last_name_mismatch", "individual__last_name", individual.last_name, self._matches_text),
            ("date_of_birth_mismatch", "individual__dob", individual.dob, self._matches_date),
            ("status_mismatch", "status", benefit.status, self._matches_text),
            ("amount_mismatch", "amount", benefit.amount, self._amounts_match),
            ("type_mismatch", "type", benefit.type, self._matches_text),
        )
        for mismatch_reason, column, expected_value, comparator in checks:
            if column not in row.index:
                continue
            if not comparator(row.get(column), expected_value):
                reasons.append(mismatch_reason)

        if self.micro_catchment_column in row.index and not self._matches_text(
                row.get(self.micro_catchment_column), context.get("micro_catchment")):
            reasons.append("micro_catchment_mismatch")

        paid = self._clean_value(row.get(PayrollConfig.csv_reconciliation_paid_extra_field))
        if paid not in [PayrollConfig.csv_reconciliation_paid_yes, PayrollConfig.csv_reconciliation_paid_no]:
            reasons.append("invalid_paid_value")
        if not self._clean_value(row.get(PayrollConfig.csv_reconciliation_receipt_column)):
            reasons.append("missing_payment_reference")
        return benefit, reasons


    @staticmethod
    def _matches_text(submitted_value, expected_value):
        def normalize(value):
            value = CsvReconciliationService._clean_value(value)
            return " ".join(value.casefold().split()) if value is not None else None
        return normalize(submitted_value) == normalize(expected_value)


    @staticmethod
    def _matches_date(submitted_value, expected_value):
        submitted_value = CsvReconciliationService._clean_value(submitted_value)
        if submitted_value is None or expected_value is None:
            return submitted_value is None and expected_value is None
        expected_date = pd.to_datetime(expected_value).date()
        for dayfirst in (False, True):
            try:
                if pd.to_datetime(submitted_value, dayfirst=dayfirst).date() == expected_date:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    @staticmethod
    def _clean_value(value):
        if value is None or pd.isna(value):
            return None
        value = str(value).strip()
        return value or None



    @staticmethod
    def _json_value(value):
        if value is None or pd.isna(value):
            return None
        return value.item() if hasattr(value, "item") else str(value)



    @staticmethod
    def _amounts_match(submitted_amount, benefit_amount):
        try:
            tolerance = Decimal(str(PayrollConfig.csv_reconciliation_amount_tolerance or "0"))
            return abs(Decimal(str(submitted_amount).replace(',', '')) - Decimal(str(benefit_amount))) <= tolerance
        except (InvalidOperation, TypeError, ValueError):
            return False

    @staticmethod
    def _has_blocking_reason(reasons):
        blocking_reasons = set(PayrollConfig.csv_reconciliation_blocking_mismatch_reasons or [])
        warning_reasons = set(PayrollConfig.csv_reconciliation_warning_mismatch_reasons or [])
        # Unknown reasons stay blocking so a new validator cannot silently
        # make an upload applicable before policy has been configured.
        return any(reason in blocking_reasons or reason not in warning_reasons for reason in reasons)



    @staticmethod
    def _upload_summary(upload):
        return {
            "upload_id": str(upload.id), "status": upload.status,
            "total_records": upload.total_records,
            "matched_records": upload.matched_records,
            "paid_records": upload.paid_records,
            "unpaid_records": upload.unpaid_records,
            "unmatched_records": upload.unmatched_records,
        }

    def _get_benefit_consumption_qs(self, payroll):
        qs = BenefitConsumption.objects.filter(
            payrollbenefitconsumption__payroll=payroll,
            is_deleted=False,
            individual__groupindividuals__recipient_type="PRIMARY",
            individual__groupindividuals__is_deleted=False,
        ).distinct()
        if not qs.exists():
            raise ValueError('csv_reconciliation.validation.no_benefit_consumption_for_payroll')
        return qs

    def _validate_dataframe(self, df):
        if df is None:
            raise ValueError(_("Unknown error while loading import file"))
        if df.empty:
            raise ValueError(_("Import file is empty"))
        if PayrollConfig.csv_reconciliation_errors_column in df.columns:
            raise ValueError(_("Column errors in csv."))
        required_columns = set(PayrollConfig.csv_reconciliation_field_mapping.values()) | {
            PayrollConfig.csv_reconciliation_paid_extra_field
        }
        missing_columns = required_columns - set(df.columns)
        if missing_columns:
            raise ValueError(
                _("Missing required columns: %(columns)s") % {
                    "columns": ", ".join(sorted(missing_columns)),
                }
            )
        if 'Status' in df.columns:
            if (df[PayrollConfig.csv_reconciliation_status_column] == BenefitConsumptionStatus.RECONCILED).all():
                raise ValueError(_("All of the Benefit Consumptions have been already reconciled."))

    def _fill_paid_column(self, row):
        if (PayrollConfig.csv_reconciliation_status_column in row
                and row[PayrollConfig.csv_reconciliation_status_column] == BenefitConsumptionStatus.RECONCILED):
            return PayrollConfig.csv_reconciliation_paid_yes
        else:
            return None

    def _resolve_payroll(self, payroll_id):
        if not payroll_id:
            raise ValueError('csv_reconciliation.validation.payroll_id_required')
        payroll = Payroll.objects.filter(id=payroll_id, is_deleted=False).first()
        if not payroll:
            raise ValueError('csv_reconciliation.validation.payroll_not_found')
        return payroll

    def _reconcile_bc(self, row, bc):
        bc.status = BenefitConsumptionStatus.RECONCILED
        bc.receipt = row[PayrollConfig.csv_reconciliation_receipt_column]
        extra_info = {
            k: row[k] for k in row.index
            if k not in PayrollConfig.csv_reconciliation_field_mapping
            and k != self.micro_catchment_column
            and not pd.isna(row[k])
        }
        bc.json_ext = {'extra_info': extra_info}
        bc.save(username=self.user.login_name)
        bill = Bill.objects.filter(benefitattachment__benefit=bc, is_deleted=False).first()
        if bill:
            self._reconcile_bill(row, bill)

    def _reconcile_bill(self, row, bill):
        current_date = datetime.date.today()
        bill.status = Bill.Status.RECONCILIATED
        bill.date_payed = current_date
        bill.save(username=self.user.login_name)

        bill_payment = {
            "code_tp": bill.code_tp,
            "code_ext": bill.code_ext,
            "code_receipt": bill.code,
            "label": bill.terms,
            'reconciliation_status': PaymentInvoice.ReconciliationStatus.RECONCILIATED,
            "fees": 0.0,
            "amount_received": bill.amount_total,
            "date_payment": current_date,
            'payment_origin': "online payment",
            'payer_ref': 'payment reference',
            'payer_name': 'payer name',
            "json_ext": {}
        }

        bill_payment_details = {
            'subject_type': ContentType.objects.get_for_model(bill),
            'subject': bill,
            'status': DetailPaymentInvoice.DetailPaymentStatus.ACCEPTED,
            'fees': 0.0,
            'amount': bill.amount_total,
            'reconcilation_id': row[PayrollConfig.csv_reconciliation_receipt_column],
            'reconcilation_date': current_date,
        }
        bill_payment_details = DetailPaymentInvoice(**bill_payment_details)
        payment_service = PaymentInvoiceService(self.user)
        payment_service.create_with_detail(bill_payment, bill_payment_details)
