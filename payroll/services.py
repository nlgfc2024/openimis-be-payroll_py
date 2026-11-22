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
from django.db.models import Count, Exists, OuterRef, Q, Sum
from django.utils import timezone
from django.utils.translation import gettext as _

from core import datetime
from core.custom_filters import CustomFilterWizardStorage
from core.models import InteractiveUser
from core.services import BaseService
from core.signals import register_service_signal
from invoice.models import Bill, BillItem, PaymentInvoice, DetailPaymentInvoice
from location.models import Location, LocationManager
from invoice.services import PaymentInvoiceService
from individual.data_source import KYC_PAYMENT_DETAILS_FIELDS
from payment_cycle.models import PaymentCycle
from payroll.apps import (
    GROUP_BENEFIT_PAYEE_PRIMARY_WORKER,
    LEGACY_KYC_PAYMENT_DETAILS_FIELD_MAPPING,
    PayrollConfig,
    KYC_PAYMENT_DETAILS_FIELD_MAPPING,
)
from payroll.consolidation import (
    ensure_benefit_is_not_in_active_consolidation,
    ensure_payroll_is_not_consolidated_source,
    payrolls_in_district,
    reconcile_consolidated_sources,
)
from payroll.models import (
    PaymentPoint,
    PaymentProvider,
    Payroll,
    PayrollBenefitConsumption,
    BenefitConsumption,
    BenefitAttachment,
    BenefitConsumptionStatus,
    ConsolidatedPayrollMember,
    PayrollStatus,
    CsvReconciliationUpload,
    ReconciliationUploadRow,
    WithdrawalFeeBand,
)
from payroll.tasks import send_requests_to_gateway_payment
from payroll.utils import PayrollNameGenerator
from payroll.validation import PaymentPointValidation, PayrollValidation, BenefitConsumptionValidation
from calculation.services import get_calculation_object
from core.services.utils import output_exception, check_authentication
from contribution_plan.models import PaymentPlan
from individual.primary_recipients import authoritative_primary_worker_memberships
from social_protection.models import Beneficiary, BeneficiaryStatus
from tasks_management.apps import TasksManagementConfig
from tasks_management.models import Task
from tasks_management.services import TaskService, _get_std_task_data_payload

logger = logging.getLogger(__name__)

RECONCILIATION_EXPORT_FIELD_MAPPING = {
    "individual__location__parent__parent__parent__name": "District Name",
    "individual__json_ext__form_number": "Form Number",
    "individual__json_ext__household_mobile_number": "Phone Number",
    "individual__first_name": "First Name",
    "individual__last_name": "Last Name",
    "individual__json_ext__national_id": "National ID",
    "json_ext__kyc_payment_details__gender": "Gender",
    "json_ext__kyc_payment_details__account_number": "Account Number",
    "json_ext__kyc_payment_details__account_name": "Account Name",
    "json_ext__kyc_payment_details__bank": "Bank",
    "code": "Code",
    "status": "Status",
    "amount": "Amount",
    "type": "Type",
}
RECONCILIATION_PAID_COLUMN = "Paid"


class WithdrawalFeeConfigService:
    def __init__(self, user):
        self.user = user

    @staticmethod
    def build_fee_preview_catalog(as_of=None):
        as_of = as_of or datetime.date.today()
        providers = list(PaymentProvider.objects.filter(is_deleted=False, is_active=True))
        provider_ids = [provider.id for provider in providers]
        fee_bands = WithdrawalFeeBand.objects.filter(
            provider_id__in=provider_ids,
            is_deleted=False,
            effective_from__lte=as_of,
        ).filter(
            Q(effective_to__isnull=True) | Q(effective_to__gte=as_of),
        )
        providers_by_code = {}
        providers_by_name = {}
        for provider in providers:
            providers_by_code.setdefault(provider.code.strip().casefold(), []).append(provider)
            providers_by_name.setdefault(provider.name.strip().casefold(), []).append(provider)
        bands_by_provider = {}
        for band in fee_bands:
            bands_by_provider.setdefault(band.provider_id, []).append(band)
        return {
            "providers_by_code": providers_by_code,
            "providers_by_name": providers_by_name,
            "bands_by_provider": bands_by_provider,
        }

    @staticmethod
    def preview_fee_for_benefit(benefit, catalog):
        withdrawal_fee = getattr(benefit, "withdrawal_fee", None)
        withdrawal_fee_error = getattr(benefit, "withdrawal_fee_error", None)
        amount = getattr(benefit, "amount", None)
        if withdrawal_fee_error:
            return withdrawal_fee, withdrawal_fee_error
        if withdrawal_fee is not None:
            return withdrawal_fee, None
        if getattr(benefit, "withdrawal_fee_prepared_at", None):
            return None, "The saved withdrawal fee snapshot is incomplete."
        if amount is None:
            return None, "The benefit amount is missing."

        details = ((getattr(benefit, "json_ext", None) or {}).get("kyc_payment_details") or {})
        provider_code = details.get("payment_provider_code")
        provider_name = details.get("bank")
        if provider_code:
            providers = catalog["providers_by_code"].get(
                str(provider_code).strip().casefold(), []
            )
        elif provider_name:
            providers = catalog["providers_by_name"].get(
                str(provider_name).strip().casefold(), []
            )
        else:
            return None, "The KYC payment provider is missing."
        if len(providers) != 1:
            return None, "The KYC payment provider is unavailable or ambiguous."

        provider = providers[0]
        fee_bands = [
            band for band in catalog["bands_by_provider"].get(provider.id, [])
            if band.minimum_amount <= amount
            and (band.maximum_amount is None or amount <= band.maximum_amount)
        ]
        if len(fee_bands) != 1:
            return None, (
                f"Expected one effective fee band for {provider.name} and amount "
                f"{amount}, found {len(fee_bands)}."
            )
        return fee_bands[0].transfer_cost, None

    @transaction.atomic
    def create_provider(self, data):
        name = (data.get("name") or "").strip()
        code = (data.get("code") or "").strip().upper()
        provider_type = (data.get("provider_type") or "").strip().upper()
        self._validate_provider(name, code, provider_type)
        if PaymentProvider.objects.filter(code=code).exists():
            raise ValidationError(_("A provider with this code already exists."))
        if PaymentProvider.objects.filter(name__iexact=name, is_deleted=False).exists():
            raise ValidationError(_("A provider with this name already exists."))
        provider = PaymentProvider(
            name=name,
            code=code,
            provider_type=provider_type,
            is_active=data.get("is_active", True),
        )
        provider.save(username=self.user.username)
        return provider

    @transaction.atomic
    def save_provider_with_fee_band(self, provider_data, fee_band_data):
        provider_id = provider_data.pop("id", None)
        if provider_id:
            provider = self.update_provider(provider_id, provider_data)
        else:
            provider = self.create_provider(provider_data)

        fee_band_data["provider_id"] = provider.id
        fee_band_id = fee_band_data.pop("id", None)
        if fee_band_id:
            fee_band = self.update_fee_band(fee_band_id, fee_band_data)
        else:
            fee_band = self.create_fee_band(fee_band_data)
        return provider, fee_band

    @transaction.atomic
    def update_provider(self, provider_id, data):
        provider = PaymentProvider.objects.select_for_update().filter(
            id=provider_id, is_deleted=False,
        ).first()
        if not provider:
            raise ValidationError(_("Payment provider was not found."))
        name = (data.get("name") or provider.name).strip()
        code = (data.get("code") or provider.code).strip().upper()
        provider_type = (data.get("provider_type") or provider.provider_type).strip().upper()
        self._validate_provider(name, code, provider_type)
        if PaymentProvider.objects.filter(code=code).exclude(id=provider.id).exists():
            raise ValidationError(_("A provider with this code already exists."))
        if PaymentProvider.objects.filter(
            name__iexact=name, is_deleted=False,
        ).exclude(id=provider.id).exists():
            raise ValidationError(_("A provider with this name already exists."))
        provider.name = name
        provider.code = code
        provider.provider_type = provider_type
        if data.get("is_active") is not None:
            provider.is_active = data["is_active"]
        provider.save(username=self.user.username)
        return provider

    @staticmethod
    def _validate_provider(name, code, provider_type):
        if not name or not code:
            raise ValidationError(_("Provider name and code are required."))
        if name.strip().casefold() in {"n/a", "unknown"} or code.strip().casefold() in {"n/a", "unknown"}:
            raise ValidationError(_("N/A and Unknown cannot be configured as payment providers."))
        if provider_type not in PaymentProvider.ProviderType.values:
            raise ValidationError(_("Provider type is invalid."))

    @transaction.atomic
    def create_fee_band(self, data):
        provider = PaymentProvider.objects.select_for_update().filter(
            id=data.get("provider_id"),
            is_deleted=False,
            is_active=True,
        ).first()
        if not provider:
            raise ValidationError(_("An active payment provider is required."))
        band = WithdrawalFeeBand(
            provider=provider,
            minimum_amount=data["minimum_amount"],
            maximum_amount=data.get("maximum_amount"),
            transfer_cost=data["transfer_cost"],
            effective_from=data["effective_from"],
            effective_to=data.get("effective_to"),
        )
        self._validate_fee_band(band)
        band.save(username=self.user.username)
        return band

    @transaction.atomic
    def update_fee_band(self, band_id, data):
        band = WithdrawalFeeBand.objects.select_for_update().filter(
            id=band_id, is_deleted=False,
        ).first()
        if not band:
            raise ValidationError(_("Withdrawal fee band was not found."))
        if band.prepared_benefits.filter(
            is_deleted=False,
            withdrawal_fee_prepared_at__isnull=False,
        ).exists():
            raise ValidationError(_("A fee band used by a prepared payment cannot be changed."))
        provider_id = data.get("provider_id", band.provider_id)
        provider = PaymentProvider.objects.select_for_update().filter(
            id=provider_id, is_deleted=False,
        ).first()
        if not provider or (provider.id != band.provider_id and not provider.is_active):
            raise ValidationError(_("An active payment provider is required."))
        band.provider = provider
        for field in (
            "minimum_amount", "maximum_amount", "transfer_cost",
            "effective_from", "effective_to",
        ):
            if field in data:
                setattr(band, field, data[field])
        self._validate_fee_band(band, exclude_id=band.id)
        band.save(username=self.user.username)
        return band

    @staticmethod
    def _validate_fee_band(band, exclude_id=None):
        if band.minimum_amount < 0 or band.transfer_cost < 0:
            raise ValidationError(_("Amounts and transfer costs cannot be negative."))
        if band.maximum_amount is not None and band.maximum_amount < band.minimum_amount:
            raise ValidationError(_("Maximum amount must be greater than or equal to minimum amount."))
        if band.effective_to and band.effective_to < band.effective_from:
            raise ValidationError(_("Effective end date must not precede the start date."))

        candidates = WithdrawalFeeBand.objects.select_for_update().filter(
            provider=band.provider,
            is_deleted=False,
        )
        if exclude_id:
            candidates = candidates.exclude(id=exclude_id)
        for existing in candidates:
            amount_ranges_overlap = (
                existing.maximum_amount is None
                or band.minimum_amount <= existing.maximum_amount
            ) and (
                band.maximum_amount is None
                or existing.minimum_amount <= band.maximum_amount
            )
            effective_ranges_overlap = (
                band.effective_to is None
                or existing.effective_from <= band.effective_to
            ) and (
                existing.effective_to is None
                or band.effective_from <= existing.effective_to
            )
            if amount_ranges_overlap and effective_ranges_overlap:
                raise ValidationError(
                    _("Fee bands for the same provider cannot overlap in amount and effective date.")
                )

    @transaction.atomic
    def delete_fee_band(self, band_id):
        band = WithdrawalFeeBand.objects.select_for_update().filter(
            id=band_id, is_deleted=False,
        ).first()
        if not band:
            raise ValidationError(_("Withdrawal fee band was not found."))
        if band.prepared_benefits.filter(is_deleted=False).exists():
            raise ValidationError(_("A fee band used by a prepared payment cannot be deleted."))
        band.delete(username=self.user.username)


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
                                "No active household beneficiaries are assigned to the selected project, "
                                "or the project is a work project that is not completed yet. Ensure the "
                                "household assignments were saved before creating the payroll."
                            )
                        # The timesheet GROUP strategy creates one benefit per group
                        # enrollment. Passing the explicit enrollment queryset is
                        # essential: it limits the run to the chosen project instead
                        # of allowing the strategy to calculate every completed
                        # project in the benefit plan.
                        expected_workers = self._lock_primary_workers(enrollments_queryset)
                        self._generate_benefits(
                            payment_plan,
                            date_valid_from,
                            date_valid_to,
                            payroll,
                            payment_cycle,
                            enrollments_queryset=enrollments_queryset,
                        )
                        self._assign_group_benefits_to_primary_recipients(
                            payroll, enrollments_queryset, expected_workers
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
                    self._snapshot_kyc_payment_details(payroll)
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
        ensure_payroll_is_not_consolidated_source(payroll_to_delete, "delete")
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
        payroll = Payroll.objects.get(id=payroll_id, is_deleted=False)
        ensure_payroll_is_not_consolidated_source(payroll, "change")
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
        task = TaskService(self.user).create({
            'source': 'payroll',
            'entity': payroll_to_accept,
            'status': Task.Status.RECEIVED,
            'executor_action_event': TasksManagementConfig.default_executor_event,
            'business_event': PayrollConfig.payroll_accept_event,
            'data': _get_std_task_data_payload(data)
        })
        if not task.get("success", False):
            raise ValueError(task.get("error") or "Unable to create payroll approval task")
        return task

    @transaction.atomic
    def create_consolidated_payroll(self, district_id, benefit_plan_id, source_payroll_ids):
        """Create a parent payroll without cloning the source benefits."""
        if not source_payroll_ids:
            raise ValidationError("Select at least one approved payroll to consolidate.")
        district = Location.objects.select_for_update().get(
            uuid=district_id,
            validity_to__isnull=True,
        )
        if not LocationManager().is_allowed(self.user, [district.id]):
            raise ValidationError(
                "You are not allowed to consolidate payrolls in this district."
            )
        sources = list(Payroll.objects.select_for_update().filter(
            id__in=source_payroll_ids,
            payment_plan__benefit_plan_id=benefit_plan_id,
            status=PayrollStatus.APPROVE_FOR_PAYMENT,
            payroll_type=Payroll.PayrollType.STANDARD,
            is_deleted=False,
        ))
        # Load related objects separately after locking to avoid "FOR UPDATE cannot be 
        # applied to the nullable side of an outer join" error on PostgreSQL.
        # Fetch payment plans, cycles, and points for all sources in bulk.
        payment_plan_ids = {s.payment_plan_id for s in sources if s.payment_plan_id}
        payment_cycle_ids = {s.payment_cycle_id for s in sources if s.payment_cycle_id}
        payment_point_ids = {s.payment_point_id for s in sources if s.payment_point_id}
        
        payment_plans = {p.id: p for p in PaymentPlan.objects.filter(id__in=payment_plan_ids)}
        payment_cycles = {c.id: c for c in PaymentCycle.objects.filter(id__in=payment_cycle_ids)}
        payment_points = {pt.id: pt for pt in PaymentPoint.objects.filter(id__in=payment_point_ids)}
        
        for source in sources:
            source.payment_plan = payment_plans.get(source.payment_plan_id)
            source.payment_cycle = payment_cycles.get(source.payment_cycle_id)
            source.payment_point = payment_points.get(source.payment_point_id)
        if len(sources) != len(source_payroll_ids):
            raise ValidationError("Every selected payroll must still be approved and belong to the selected phase.")
        eligible_source_ids = {
            source.id for source in payrolls_in_district(sources, district.id)
        }
        if eligible_source_ids != {source.id for source in sources}:
            raise ValidationError(
                "Every selected payroll must belong to the selected district."
            )
        source_ids = [source.id for source in sources]
        pending_source_tasks = Task.objects.filter(
            entity_type=ContentType.objects.get_for_model(Payroll),
            entity_id__in=[str(source_id) for source_id in source_ids],
            status__in=[Task.Status.RECEIVED, Task.Status.ACCEPTED],
            is_deleted=False,
        )
        if pending_source_tasks.exists():
            raise ValidationError(
                "A selected payroll has a pending task. Resolve its pending work before consolidation."
            )
        source_links = list(
            PayrollBenefitConsumption.objects.filter(
                payroll_id__in=source_ids,
                is_deleted=False,
            ).values_list("payroll_id", "benefit_id")
        )
        source_benefit_ids = {benefit_id for _, benefit_id in source_links}
        pending_benefit_tasks = Task.objects.filter(
            entity_type=ContentType.objects.get_for_model(BenefitConsumption),
            entity_id__in=[str(benefit_id) for benefit_id in source_benefit_ids],
            status__in=[Task.Status.RECEIVED, Task.Status.ACCEPTED],
            is_deleted=False,
        )
        if pending_benefit_tasks.exists():
            raise ValidationError(
                "A selected payroll has a benefit with pending work. Resolve it before consolidation."
            )
        source_benefits = list(
            BenefitConsumption.objects.select_for_update().filter(
                id__in=source_benefit_ids,
                is_deleted=False,
            )
        )
        if (
            len(source_benefits) != len(source_benefit_ids)
            or any(
                not any(linked_payroll_id == source.id for linked_payroll_id, _ in source_links)
                for source in sources
            )
        ):
            raise ValidationError(
                "Every selected payroll must have active, non-deleted benefits."
            )
        if any(
            benefit.status in (
                BenefitConsumptionStatus.PENDING_DELETION,
                BenefitConsumptionStatus.RECONCILED,
            )
            for benefit in source_benefits
        ):
            raise ValidationError(
                "Selected payrolls cannot contain benefits pending deletion or already reconciled."
            )
        first = sources[0]
        duplicate_recipients = PayrollBenefitConsumption.objects.filter(
            payroll_id__in=source_ids,
            is_deleted=False,
            benefit__is_deleted=False,
        ).values(
            "benefit__individual_id",
        ).annotate(
            source_count=Count("payroll_id", distinct=True),
        ).filter(
            source_count__gt=1,
        )
        if duplicate_recipients.exists():
            raise ValidationError(
                "Selected payrolls contain benefits for the same recipient. "
                "Remove overlapping source payrolls before consolidation."
            )
        if PayrollBenefitConsumption.objects.filter(
            payroll__payroll_type=Payroll.PayrollType.CONSOLIDATED,
            payroll__payment_cycle_id=first.payment_cycle_id,
            payroll__payment_plan__benefit_plan_id=first.payment_plan.benefit_plan_id,
            payroll__is_deleted=False,
            is_deleted=False,
            benefit__is_deleted=False,
            benefit__individual_id__in={
                benefit.individual_id for benefit in source_benefits
            },
            payroll__consolidation_members__source_payroll__is_deleted=False,
        ).exclude(payroll__status=PayrollStatus.REJECTED).exists():
            raise ValidationError(
                "A selected recipient was already included in a consolidated payroll."
            )
        if ConsolidatedPayrollMember.objects.filter(
                source_payroll_id__in=source_ids,
                is_deleted=False,
                consolidated_payroll__is_deleted=False,
        ).exclude(consolidated_payroll__status=PayrollStatus.REJECTED).exists():
            raise ValidationError("A selected payroll already belongs to a consolidated payroll.")
        if any((source.payment_method, source.payment_point_id, source.payment_cycle_id) !=
               (first.payment_method, first.payment_point_id, first.payment_cycle_id) for source in sources):
            raise ValidationError("Selected payrolls must use the same payment method, point, and cycle.")
        phase = first.payment_plan.benefit_plan.name or first.payment_plan.benefit_plan.code
        name_base = f"{district.name}-{phase}-Consolidated Payroll"
        name_prefix = f"{name_base}-"
        used_sequences = []
        for existing_name in Payroll.objects.filter(
            name__startswith=name_prefix, is_deleted=False,
        ).values_list("name", flat=True):
            suffix = existing_name[len(name_prefix):]
            if suffix.isdigit():
                used_sequences.append(int(suffix))
        sequence = max(used_sequences, default=0) + 1
        payroll = Payroll(
            name=f"{name_base}-{sequence:03d}", payment_plan=first.payment_plan,
            payment_cycle=first.payment_cycle, payment_point=first.payment_point,
            payment_method=first.payment_method, status=PayrollStatus.PENDING_APPROVAL,
            payroll_type=Payroll.PayrollType.CONSOLIDATED, district=district,
            json_ext={"consolidated_source_payroll_ids": [str(source.id) for source in sources]},
        )
        payroll.save(username=self.user.login_name)
        consolidated_links = []
        for source in sources:
            benefits = PayrollBenefitConsumption.objects.filter(payroll=source, is_deleted=False)
            benefit_ids = list(benefits.values_list("benefit_id", flat=True))
            total = BenefitConsumption.objects.filter(id__in=benefit_ids).aggregate(total=Sum("amount"))["total"] or 0
            ConsolidatedPayrollMember(
                consolidated_payroll=payroll, source_payroll=source,
                beneficiaries_count=len(benefit_ids), total_amount=total,
            ).save(username=self.user.login_name)
            for benefit_id in benefit_ids:
                link = PayrollBenefitConsumption(
                    payroll=payroll,
                    benefit_id=benefit_id,
                    user_created=self.user,
                    user_updated=self.user,
                )
                link.set_pk()
                consolidated_links.append(link)
        if consolidated_links:
            bulk_create_with_history(
                consolidated_links,
                PayrollBenefitConsumption,
                default_user=self.user,
                batch_size=1000,
            )
        self._prepare_withdrawal_fee_snapshots(payroll)
        self.create_accept_payroll_task(payroll.id, {
            "id": payroll.id, "district": district.name, "phase": phase,
            "included_payrolls": [{"id": str(source.id), "name": source.name} for source in sources],
            "consolidated": True,
        })
        return payroll

    @register_service_signal('payroll_service.close_payroll')
    def close_payroll(self, obj_data):
        payroll_to_close = Payroll.objects.get(id=obj_data['id'])
        ensure_payroll_is_not_consolidated_source(payroll_to_close, "reconcile")
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
        ensure_payroll_is_not_consolidated_source(payroll_to_reject, "reject")
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
        payroll = Payroll.objects.get(id=obj_data['id'], is_deleted=False)
        ensure_payroll_is_not_consolidated_source(payroll, "pay")
        self._prepare_withdrawal_fee_snapshots(payroll)
        transaction.on_commit(
            lambda: send_requests_to_gateway_payment.delay(payroll.id, self.user.id)
        )

    @transaction.atomic
    def refresh_withdrawal_fee_snapshots(self, payroll_id):
        payroll = Payroll.objects.select_for_update().get(
            id=payroll_id,
            is_deleted=False,
        )
        if (
            payroll.payroll_type != Payroll.PayrollType.CONSOLIDATED
            or payroll.status != PayrollStatus.PENDING_APPROVAL
        ):
            raise ValidationError(
                "Withdrawal fees can only be refreshed for a consolidated payroll "
                "awaiting approval."
            )
        if not LocationManager().is_allowed(self.user, [payroll.district_id]):
            raise ValidationError(
                "You are not allowed to refresh fees for this district."
            )
        self._prepare_withdrawal_fee_snapshots(payroll, retry_unresolved=True)
        return payroll

    @transaction.atomic
    def _prepare_withdrawal_fee_snapshots(self, payroll, retry_unresolved=False):
        benefit_ids = PayrollBenefitConsumption.objects.filter(
            payroll_id=payroll.id,
            is_deleted=False,
        ).values("benefit_id").distinct()
        snapshot_filter = (
            Q(withdrawal_fee_prepared_at__isnull=True)
            | Q(withdrawal_fee_error__isnull=False)
            if retry_unresolved
            else Q(withdrawal_fee_prepared_at__isnull=True)
        )
        benefits = list(
            BenefitConsumption.objects.select_for_update(of=("self",)).filter(
                id__in=benefit_ids,
                is_deleted=False,
                status__in=[
                    BenefitConsumptionStatus.ACCEPTED,
                    BenefitConsumptionStatus.APPROVE_FOR_PAYMENT,
                ],
            ).filter(snapshot_filter).select_related("individual")
        )
        as_of = datetime.date.today()
        prepared = []
        errors = []

        def use_zero_fee_snapshot(benefit, reason):
            logger.warning(
                "No withdrawal fee snapshot available for benefit %s; using zero fee: %s",
                benefit.code,
                reason,
            )
            prepared.append((benefit, None, None, Decimal("0.00"), reason))

        for benefit in benefits:
            if benefit.amount is None:
                errors.append(f"{benefit.code}: missing benefit amount")
                continue
            if benefit.withdrawal_fee_prepared_at:
                if benefit.withdrawal_fee is None and not (
                    retry_unresolved and benefit.withdrawal_fee_error
                ):
                    errors.append(f"{benefit.code}: incomplete withdrawal fee snapshot")
                    continue
                if not (retry_unresolved and benefit.withdrawal_fee_error):
                    continue
            details = ((benefit.json_ext or {}).get("kyc_payment_details") or {})
            account_number = details.get("account_number")
            account_name = details.get("account_name")
            if not account_number or not account_name:
                use_zero_fee_snapshot(benefit, "missing KYC account details")
                continue

            provider_code = details.get("payment_provider_code")
            provider_name = details.get("bank")
            provider_query = PaymentProvider.objects.select_for_update().filter(
                is_deleted=False,
                is_active=True,
            )
            if provider_code:
                provider_query = provider_query.filter(code__iexact=str(provider_code).strip())
            elif provider_name:
                provider_query = provider_query.filter(name__iexact=str(provider_name).strip())
            else:
                use_zero_fee_snapshot(benefit, "missing KYC payment provider")
                continue
            providers = list(provider_query[:2])
            if len(providers) != 1:
                use_zero_fee_snapshot(benefit, "KYC payment provider is unavailable or ambiguous")
                continue
            provider = providers[0]

            fee_bands = list(
                WithdrawalFeeBand.objects.select_for_update().filter(
                    provider=provider,
                    is_deleted=False,
                    effective_from__lte=as_of,
                ).filter(
                    Q(effective_to__isnull=True) | Q(effective_to__gte=as_of),
                    minimum_amount__lte=benefit.amount,
                ).filter(
                    Q(maximum_amount__isnull=True) | Q(maximum_amount__gte=benefit.amount),
                )[:2]
            )
            if len(fee_bands) != 1:
                use_zero_fee_snapshot(
                    benefit,
                    f"expected one effective fee band for {provider.name} "
                    f"and amount {benefit.amount}, found {len(fee_bands)}",
                )
                continue
            prepared.append((benefit, provider, fee_bands[0], fee_bands[0].transfer_cost, None))

        if errors:
            raise ValidationError(
                _("Payment preparation blocked: %(errors)s")
                % {"errors": "; ".join(errors)}
            )

        now = timezone.now()
        for benefit, provider, fee_band, withdrawal_fee, withdrawal_fee_error in prepared:
            benefit.withdrawal_provider = provider
            benefit.withdrawal_fee_band = fee_band
            benefit.withdrawal_fee = withdrawal_fee
            benefit.withdrawal_fee_prepared_at = now
            benefit.withdrawal_fee_error = withdrawal_fee_error
            benefit.save(username=self.user.username)

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

    @staticmethod
    def _pays_group_benefits_to_primary_workers():
        return PayrollConfig.group_benefit_payee == GROUP_BENEFIT_PAYEE_PRIMARY_WORKER

    @staticmethod
    def _payable_project_filter(enrollment_model):
        """WORK projects are paid once COMPLETED. A training is paid whatever its
        status: its calculation rule decides which status counts."""
        payable = Q(project__status="COMPLETED")
        project_model = enrollment_model._meta.get_field("project").related_model
        if any(field.name == "project_type" for field in project_model._meta.get_fields()):
            payable |= Q(project__project_type="TRAINING")
        return payable

    @staticmethod
    def _households_with_one_primary_worker():
        """Group IDs whose verified household has exactly one primary worker, as
        resolved for enrolment and KYC."""
        return authoritative_primary_worker_memberships().values("group_id").annotate(
            workers=Count("id"),
        ).filter(workers=1).values("group_id")

    def _select_group_enrollments_based_on_criteria(self, obj_data, payment_plan):
        """Return the eligible household enrollments for a GROUP payroll.

        A household is paid once, through its payee (``group_benefit_payee``), so
        the unit of selection must be ``GroupBeneficiaryProjectEnrollment`` rather
        than the individual ``Beneficiary`` model used by individual benefit plans.
        """
        enrollment_model = apps.get_model(
            "project_social_protection", "GroupBeneficiaryProjectEnrollment"
        )
        json_ext = obj_data.get("json_ext", {})
        filter_criteria = json_ext.get("filter_criteria", {})
        if self._pays_group_benefits_to_primary_workers():
            payee_filter = {"group_beneficiary__group_id__in": self._households_with_one_primary_worker()}
        else:
            payee_filter = {
                "group_beneficiary__group__groupindividuals__recipient_type": "PRIMARY",
                "group_beneficiary__group__groupindividuals__is_deleted": False,
            }
        enrollments_queryset = enrollment_model.objects.filter(
            self._payable_project_filter(enrollment_model),
            group_beneficiary__benefit_plan_id=payment_plan.benefit_plan.id,
            group_beneficiary__status=BeneficiaryStatus.ACTIVE,
            group_beneficiary__is_deleted=False,
            project__is_deleted=False,
            is_deleted=False,
            **payee_filter,
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

        # A household member is paid only as its PRIMARY recipient, never as one
        # of several members. An individual who belongs to no household (an
        # INDIVIDUAL programme such as UMEP enrols only those) is paid directly.
        group_individual_model = apps.get_model("individual", "GroupIndividual")
        is_primary_recipient = Exists(group_individual_model.objects.filter(
            individual_id=OuterRef("individual_id"),
            recipient_type="PRIMARY",
            is_deleted=False,
        ))
        belongs_to_a_household = Exists(group_individual_model.objects.filter(
            individual_id=OuterRef("individual_id"),
            is_deleted=False,
            group__is_deleted=False,
        ))
        beneficiaries_queryset = beneficiaries_queryset.filter(
            is_primary_recipient | ~belongs_to_a_household
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

    def _snapshot_kyc_payment_details(self, payroll):
        benefits = BenefitConsumption.objects.filter(
            payrollbenefitconsumption__payroll=payroll,
            payrollbenefitconsumption__is_deleted=False,
            is_deleted=False,
        ).select_related("individual")
        snapshot_fields = set(KYC_PAYMENT_DETAILS_FIELDS) | {"gender"}
        for benefit in benefits:
            benefit_json_ext = benefit.json_ext or {}
            if benefit_json_ext.get("kyc_payment_details") is not None:
                continue
            individual_json_ext = (
                benefit.individual.json_ext if benefit.individual else {}
            ) or {}
            benefit_json_ext = dict(benefit_json_ext)
            benefit_json_ext["kyc_payment_details"] = {
                field: individual_json_ext.get(field)
                for field in snapshot_fields
            }
            benefit.json_ext = benefit_json_ext
            benefit.save(user=self.user)

    def _lock_primary_workers(self, enrollments_queryset):
        """Lock the selected households and their members until the payroll is saved,
        and return each household's primary worker as the calculation will see it.

        Only when benefits are paid to primary workers. The calculation rule and the
        assignment each look the worker up; the locks stop a member being changed or
        removed in between, and the assignment compares its lookup with this one to
        catch a member added in between. Returns None otherwise.
        """
        if not self._pays_group_benefits_to_primary_workers():
            return None
        group_ids_by_beneficiary = dict(
            enrollments_queryset.values_list("group_beneficiary_id", "group_beneficiary__group_id")
        )
        group_ids = sorted(set(group_ids_by_beneficiary.values()), key=str)
        group_model = apps.get_model("individual", "Group")
        group_individual_model = apps.get_model("individual", "GroupIndividual")
        individual_model = apps.get_model("individual", "Individual")
        members = group_individual_model.objects.filter(group_id__in=group_ids)
        # Locked in one order (households, memberships, people) to avoid deadlocks.
        list(group_model.objects.select_for_update().filter(id__in=group_ids).order_by("id").values_list("id"))
        list(members.select_for_update().order_by("id").values_list("id"))
        list(individual_model.objects.select_for_update().filter(
            id__in=members.values("individual_id")).order_by("id").values_list("id"))
        return self._primary_worker_by_group(group_ids_by_beneficiary)

    def _assign_group_benefits_to_primary_recipients(self, payroll, enrollments_queryset, expected_workers=None):
        """Make each generated household benefit payable to its payee: the PRIMARY
        member, or the primary worker when ``group_benefit_payee`` says so.

        ``expected_workers`` is what _lock_primary_workers returned before the
        calculation; a household whose worker differs now fails the payroll, so a
        benefit never moves to another member than the one it was calculated for."""
        group_ids_by_beneficiary = dict(
            enrollments_queryset.values_list("group_beneficiary_id", "group_beneficiary__group_id")
        )
        if self._pays_group_benefits_to_primary_workers():
            primary_recipient_by_group = self._primary_worker_by_group(group_ids_by_beneficiary)
            if expected_workers is not None:
                changed = {
                    group_id for group_id in group_ids_by_beneficiary.values()
                    if primary_recipient_by_group.get(group_id) != expected_workers.get(group_id)
                }
                if changed:
                    codes = apps.get_model("individual", "Group").objects.filter(
                        id__in=changed).values_list("code", flat=True)
                    raise ValidationError(
                        "The primary worker of these households changed while the payroll was being "
                        "calculated: %s. Create the payroll again." % ", ".join(sorted(map(str, codes)))
                    )
        else:
            group_individual_model = apps.get_model("individual", "GroupIndividual")
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
            if self._pays_group_benefits_to_primary_workers():
                missing = sorted(
                    beneficiary_id for beneficiary_id, recipient_id in primary_recipient_by_beneficiary.items()
                    if recipient_id is None
                )
                raise ValidationError(
                    "Every household selected for payroll must have exactly one verified "
                    "primary worker. Households without one: %s." % ", ".join(missing)
                )
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
    def _primary_worker_by_group(group_ids_by_beneficiary):
        """The single verified primary worker of each household; a household with
        none or several has no entry."""
        workers = {}
        for group_id, individual_id in authoritative_primary_worker_memberships().filter(
                group_id__in=group_ids_by_beneficiary.values(),
        ).values_list("group_id", "individual_id"):
            workers.setdefault(group_id, set()).add(individual_id)
        return {group_id: ids.pop() for group_id, ids in workers.items() if len(ids) == 1}

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
        source_payroll = Payroll.objects.select_for_update().get(id=from_payroll_id)
        if source_payroll.payroll_type == Payroll.PayrollType.CONSOLIDATED:
            raise ValidationError("Cannot move benefits from a consolidated payroll.")
        ensure_payroll_is_not_consolidated_source(source_payroll, "move benefits from")
        payroll_benefits = list(PayrollBenefitConsumption.objects.select_for_update().filter(
            payroll_id=from_payroll_id,
            is_deleted=False,
            benefit__status__in=[BenefitConsumptionStatus.ACCEPTED, BenefitConsumptionStatus.APPROVE_FOR_PAYMENT]
        ).select_related("benefit"))
        benefit_ids = {link.benefit_id for link in payroll_benefits}
        for link in payroll_benefits:
            link.payroll = payroll
            link.save(username=self.user.login_name)
        reconciled_consolidated_payroll_ids = ConsolidatedPayrollMember.objects.filter(
            source_payroll=source_payroll,
            consolidated_payroll__status=PayrollStatus.RECONCILED,
            consolidated_payroll__is_deleted=False,
        ).values_list("consolidated_payroll_id", flat=True)
        consolidated_links = PayrollBenefitConsumption.objects.select_for_update().filter(
            benefit_id__in=benefit_ids,
            payroll__payroll_type=Payroll.PayrollType.CONSOLIDATED,
            payroll_id__in=reconciled_consolidated_payroll_ids,
            is_deleted=False,
        )
        for link in consolidated_links:
            link.delete(user=self.user)
        BenefitConsumption.objects.filter(
            id__in=benefit_ids,
            is_deleted=False,
        ).update(status=BenefitConsumptionStatus.ACCEPTED)


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
        benefit = BenefitConsumption.objects.get(id=obj_data["id"], is_deleted=False)
        ensure_benefit_is_not_in_active_consolidation(benefit, "update")
        return super().update(obj_data)

    @check_authentication
    @register_service_signal('benefit_consumption_service.delete')
    def delete(self, obj_data):
        benefit_to_delete = BenefitConsumption.objects.get(id=obj_data['id'])
        ensure_benefit_is_not_in_active_consolidation(benefit_to_delete, "delete")
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
        benefit = BenefitConsumption.objects.get(id=benefit_id, is_deleted=False)
        ensure_benefit_is_not_in_active_consolidation(benefit, "change attachments for")
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

    @staticmethod
    def _currency_label():
        import core
        from django.core.exceptions import ImproperlyConfigured

        currency = getattr(core, "currency", None)
        if not currency:
            raise ImproperlyConfigured("The core currency is not configured.")
        return str(currency)

    @classmethod
    def _withdrawal_fee_header(cls):
        return f"Withdrawal fee ({cls._currency_label()})"

    @classmethod
    def _amount_to_pay_header(cls):
        return f"Amount to pay (gross + fee) ({cls._currency_label()})"

    @classmethod
    def _reconciliation_export_columns(cls):
        return [
            "Source Payroll",
            "Payroll Name",
            "Payroll Status",
            "District Name",
            "Micro Catchment",
            "Form Number",
            "Phone Number",
            "First Name",
            "Last Name",
            "National ID",
            "Gender",
            "Account Number",
            "Account Name",
            "Bank",
            "Code",
            "Status",
            "Amount",
            cls._withdrawal_fee_header(),
            cls._amount_to_pay_header(),
            "Type",
            "Paid",
        ]

    @classmethod
    def _reconciliation_field_mapping(cls):
        """Return configured fields, adding KYC fields for older installations."""
        field_mapping = dict(PayrollConfig.csv_reconciliation_field_mapping or {})
        snapshot_field_by_legacy = {
            legacy_field: snapshot_field
            for legacy_field, legacy_header in LEGACY_KYC_PAYMENT_DETAILS_FIELD_MAPPING.items()
            for snapshot_field, snapshot_header in KYC_PAYMENT_DETAILS_FIELD_MAPPING.items()
            if legacy_header == snapshot_header
        }
        mapped_fields = {
            snapshot_field_by_legacy.get(field, field): header
            for field, header in field_mapping.items()
        }
        mapped_headers = set(mapped_fields.values())
        for field, header in KYC_PAYMENT_DETAILS_FIELD_MAPPING.items():
            if field not in mapped_fields and header not in mapped_headers:
                mapped_fields[field] = header
                mapped_headers.add(header)
        mapped_fields["withdrawal_fee"] = cls._withdrawal_fee_header()
        if not mapped_fields.get("type"):
            mapped_fields["type"] = "Type"
        code_field = PayrollConfig.csv_reconciliation_code_column or "code"
        if code_field not in mapped_fields:
            mapped_fields[code_field] = "Code"
        return mapped_fields

    @classmethod
    def _payment_reference_headers(cls):
        return {
            header
            for field, header in cls._reconciliation_field_mapping().items()
            if field.rsplit("__", 1)[-1].lower() == "receipt"
        } | {"receipt", "provider transaction reference"}

    def download_reconciliation(self, payroll_id) -> BytesIO:
        payroll = self._resolve_payroll(payroll_id)
        ensure_payroll_is_not_consolidated_source(payroll, "download a reconciliation file for")
        bc_qs = self._get_benefit_consumption_qs(payroll)
        field_mapping = RECONCILIATION_EXPORT_FIELD_MAPPING
        fee_header = self._withdrawal_fee_header()
        amount_to_pay_header = self._amount_to_pay_header()
        records = list(bc_qs.values(*field_mapping.keys()))
        fee_preview_catalog = None
        benefits_by_code = {
            benefit.code: benefit
            for benefit in bc_qs.select_related(
                "individual__location__parent__parent__parent"
            )
        }

        source_payroll_by_benefit = self._get_source_payrolls_by_benefit(payroll)
        project_micro_catchments = {}
        for record in records:
            benefit = benefits_by_code[record["code"]]
            source_payroll = source_payroll_by_benefit.get(benefit.id)
            payroll_for_context = source_payroll or payroll
            record["Source Payroll"] = source_payroll.name if source_payroll else ""
            record["Payroll Name"] = payroll.name
            record["Payroll Status"] = payroll.status
            if payroll_for_context.id not in project_micro_catchments:
                project_micro_catchments[payroll_for_context.id] = self._get_payroll_micro_catchment(
                    payroll_for_context
                )
            # The payroll's project is the work location. A recipient may live
            # in a different catchment, but that must not make a one-project
            # payroll look as though it contains several project catchments.
            record["Micro Catchment"] = (
                project_micro_catchments[payroll_for_context.id]
                or self._get_micro_catchment(benefit)
            )
            if (
                getattr(benefit, "withdrawal_fee", None) is None
                and not getattr(benefit, "withdrawal_fee_prepared_at", None)
                and fee_preview_catalog is None
            ):
                fee_preview_catalog = WithdrawalFeeConfigService.build_fee_preview_catalog()
            withdrawal_fee, fee_error = WithdrawalFeeConfigService.preview_fee_for_benefit(
                benefit,
                fee_preview_catalog,
            )
            if fee_error:
                logger.warning(
                    "Withdrawal fee preview unavailable for benefit %s: %s",
                    benefit.code,
                    fee_error,
                )
            amount = getattr(benefit, "amount", None)
            record[fee_header] = withdrawal_fee
            record[amount_to_pay_header] = (
                amount + withdrawal_fee
                if amount is not None and withdrawal_fee is not None
                else None
            )

        df = pd.DataFrame.from_records(records)
        df.rename(columns=field_mapping, inplace=True)
        df[RECONCILIATION_PAID_COLUMN] = df.apply(
            lambda row: self._fill_paid_column(row), axis=1
        )
        df = df.reindex(columns=self._reconciliation_export_columns())

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

    @staticmethod
    def _get_source_payrolls_by_benefit(payroll):
        if payroll.payroll_type != Payroll.PayrollType.CONSOLIDATED:
            return {}
        members = list(ConsolidatedPayrollMember.objects.filter(
            consolidated_payroll=payroll,
            consolidated_payroll__is_deleted=False,
        ).exclude(
            consolidated_payroll__status=PayrollStatus.REJECTED,
        ).select_related("source_payroll"))
        source_payrolls = {member.source_payroll_id: member.source_payroll for member in members}
        return {
            benefit_id: source_payrolls[source_payroll_id]
            for benefit_id, source_payroll_id in PayrollBenefitConsumption.objects.filter(
                payroll_id__in=source_payrolls, is_deleted=False,
            ).values_list("benefit_id", "payroll_id")
            if source_payroll_id in source_payrolls
        }

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
        """Validate and audit a reconciliation upload before review or application."""
        payroll = self._resolve_payroll(payroll_id)
        # Serialise uploads for a payroll.  This makes superseding a pending
        # review deterministic even when corrected files arrive concurrently.
        payroll = Payroll.objects.select_for_update().get(id=payroll.id)
        ensure_payroll_is_not_consolidated_source(payroll, "upload reconciliation for")
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

        field_mapping = self._reconciliation_field_mapping()
        self._validate_dataframe(df)
        df.rename(
            columns={v: k for k, v in field_mapping.items()},
            inplace=True,
        )
        code_column = PayrollConfig.csv_reconciliation_code_column
        national_id_column = next(
            (field for field, header in field_mapping.items()
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
                consolidated_payroll=payroll,
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
                status=(ReconciliationUploadRow.Status.INVALID
                        if self._has_blocking_reason(reasons, payroll.payroll_type)
                        else ReconciliationUploadRow.Status.VALID),
            )
            # HistoryModel deliberately has no database UUID default.  These rows
            # are inserted in bulk, so assign the primary key before bulk_create.
            upload_row.set_pk()
            upload_rows.append(upload_row)
            if self._has_blocking_reason(reasons, payroll.payroll_type):
                errors[str(row_number)] = reasons
            else:
                valid_indexes.append(index)

        paid_column = RECONCILIATION_PAID_COLUMN
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

        if errors:
            upload.error = errors
            upload.status = CsvReconciliationUpload.Status.FAIL
            upload.json_ext = {"extra_info": self._upload_summary(upload)}
            upload.save(username=self.user.login_name)
            return BytesIO(contents), errors, self._upload_summary(upload)

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
        if not PayrollConfig.enable_maker_checker_for_reconciliation_upload:
            applied_upload = self.apply_upload(upload)
            # The caller retains this instance and saves it after this method;
            # mirror the locked instance updated by apply_upload to avoid restoring
            # the transient verification status.
            upload.status = applied_upload.status
            upload.error = applied_upload.error
            upload.json_ext = applied_upload.json_ext
            return BytesIO(contents), errors or None, self._upload_summary(upload)

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
                "included_payrolls": task_context["included_payrolls"],
                "consolidated": task_context["consolidated"],
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
        """Return the payroll's phase, district, and source-payroll review context."""
        consolidated = payroll.payroll_type == Payroll.PayrollType.CONSOLIDATED
        if consolidated:
            members = list(ConsolidatedPayrollMember.objects.filter(
                consolidated_payroll=payroll,
                source_payroll__is_deleted=False,
            ).select_related("source_payroll").order_by("source_payroll__name"))
            source_payrolls = [member.source_payroll for member in members]
        else:
            members = []
            source_payrolls = [payroll]

        project_ids_by_payroll = {}
        for source_payroll in source_payrolls:
            extension = source_payroll.json_ext or {}
            filter_criteria = extension.get("filter_criteria", {}) if isinstance(extension, dict) else {}
            project_ids = filter_criteria.get("project_ids", []) if isinstance(filter_criteria, dict) else []
            if isinstance(project_ids, (list, tuple)):
                project_ids_by_payroll[str(source_payroll.id)] = set(project_ids)
            else:
                project_ids_by_payroll[str(source_payroll.id)] = set()

        all_project_ids = set().union(*project_ids_by_payroll.values()) if project_ids_by_payroll else set()
        project_model = apps.get_model("project_social_protection", "Project")
        projects = project_model.objects.filter(
            id__in=all_project_ids,
            is_deleted=False,
        ).select_related("location__parent__parent__parent") if all_project_ids else []

        districts_by_payroll = {payroll_id: set() for payroll_id in project_ids_by_payroll}
        project_ids_to_payrolls = {}
        for payroll_id, project_ids in project_ids_by_payroll.items():
            for project_id in project_ids:
                project_ids_to_payrolls.setdefault(str(project_id), set()).add(payroll_id)
        for project in projects:
            district = CsvReconciliationService._district_name_for_location(project.location)
            if district:
                for payroll_id in project_ids_to_payrolls.get(str(project.id), set()):
                    districts_by_payroll[payroll_id].add(district)

        # Older payrolls may not have retained their project criteria. In that
        # case derive the district from the beneficiaries actually linked to
        # each source payroll instead of leaving the task district blank.
        fallback_payroll_ids = {
            str(source_payroll.id)
            for source_payroll in source_payrolls
            if not project_ids_by_payroll.get(str(source_payroll.id))
            or not districts_by_payroll.get(str(source_payroll.id))
        }
        source_payroll_id_by_value = {
            str(source_payroll.id): source_payroll.id for source_payroll in source_payrolls
        }
        if fallback_payroll_ids:
            fallback_memberships = PayrollBenefitConsumption.objects.filter(
                payroll_id__in=[
                    source_payroll_id_by_value[payroll_id] for payroll_id in fallback_payroll_ids
                ],
                is_deleted=False,
                benefit__is_deleted=False,
            ).select_related(
                "benefit__individual__location__parent__parent__parent",
            )
            for membership in fallback_memberships:
                payroll_id = str(membership.payroll_id)
                district = CsvReconciliationService._district_name_for_location(
                    membership.benefit.individual.location,
                )
                if district:
                    districts_by_payroll.setdefault(payroll_id, set()).add(district)

        districts = set().union(*districts_by_payroll.values()) if districts_by_payroll else set()

        payment_plan = payroll.payment_plan
        benefit_plan = payment_plan.benefit_plan if payment_plan else None
        if consolidated:
            included_payrolls = [
                {"id": str(member.source_payroll_id), "name": member.source_payroll.name}
                for member in members
            ]
        else:
            included_payrolls = [{"id": str(payroll.id), "name": payroll.name}]
        return {
            "district": ", ".join(sorted(districts)) or None,
            "phase": benefit_plan.name if benefit_plan else None,
            "consolidated": consolidated,
            "included_payrolls": included_payrolls,
        }

    @staticmethod
    def _district_name_for_location(location):
        visited = set()
        while location and location.id not in visited:
            visited.add(location.id)
            if location.type == "R":
                return location.name
            location = location.parent
        return None

    def _load_upload_context(self, payroll, codes):
        benefits = list(
            BenefitConsumption.objects.filter(
                code__in=list(codes.dropna().unique()),
                is_deleted=False,
                payrollbenefitconsumption__payroll=payroll,
                payrollbenefitconsumption__is_deleted=False,
            ).select_related("individual", "individual__location").distinct()
        )
        benefits_by_code = {benefit.code: benefit for benefit in benefits}
        source_payroll_by_benefit = self._get_source_payrolls_by_benefit(payroll)
        project_micro_catchments = {}
        contexts = {}
        for benefit in benefits:
            source_payroll = source_payroll_by_benefit.get(benefit.id, payroll)
            if source_payroll.id not in project_micro_catchments:
                project_micro_catchments[source_payroll.id] = self._get_payroll_micro_catchment(
                    source_payroll
                )
            project_micro_catchment = project_micro_catchments[source_payroll.id]
            if project_micro_catchment:
                contexts[benefit.id] = {"micro_catchment": project_micro_catchment}
        fallback_benefits = [
            benefit for benefit in benefits if benefit.id not in contexts
        ]
        if not fallback_benefits:
            return benefits_by_code, contexts

        # A historical multi-project payroll has no single project catchment.
        # Preserve its legacy recipient-location behavior rather than inventing
        # a catchment that cannot be determined from the payroll criteria.
        from location.models import Location, MicroCatchmentGVH
        locations = {
            benefit.individual.location_id: benefit.individual.location
            for benefit in fallback_benefits if benefit.individual.location_id
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
        for benefit in fallback_benefits:
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
        contexts.update({
            benefit.id: {"micro_catchment": micro_catchments.get(gvh_by_benefit[benefit.id])}
            for benefit in fallback_benefits
        })
        return benefits_by_code, contexts

    @transaction.atomic
    def apply_upload(self, upload):
        payroll = Payroll.objects.select_for_update().get(id=upload.payroll_id)
        upload = CsvReconciliationUpload.objects.select_for_update().get(id=upload.id)
        if upload.status != CsvReconciliationUpload.Status.WAITING_FOR_VERIFICATION:
            return
        if upload.unmatched_records:
            raise ValidationError(
                "A reconciliation upload with blocking validation errors cannot be applied."
            )
        if payroll.payroll_type == Payroll.PayrollType.CONSOLIDATED:
            if payroll.is_deleted or payroll.status == PayrollStatus.REJECTED:
                raise ValidationError(
                    "A reconciliation upload cannot be applied to an inactive consolidated payroll."
                )
        else:
            ensure_payroll_is_not_consolidated_source(payroll, "reconcile")
        rows = list(ReconciliationUploadRow.objects.filter(
            upload=upload,
            status=ReconciliationUploadRow.Status.VALID,
            is_deleted=False,
        ))
        if payroll.payroll_type == Payroll.PayrollType.CONSOLIDATED:
            benefit_ids = {row.benefit_id for row in rows if row.benefit_id}
            active_benefit_ids = set(
                PayrollBenefitConsumption.objects.select_for_update().filter(
                    payroll=payroll,
                    benefit_id__in=benefit_ids,
                    is_deleted=False,
                ).values_list("benefit_id", flat=True)
            )
            if benefit_ids != active_benefit_ids:
                raise ValidationError(
                    "A reconciliation upload contains benefits no longer linked to its consolidated payroll."
                )
        paid_rows = [
            row for row in rows
            if self._clean_value(row.submitted_data.get(RECONCILIATION_PAID_COLUMN))
            == PayrollConfig.csv_reconciliation_paid_yes
        ]
        locked_benefits = {
            benefit.id: benefit
            for benefit in BenefitConsumption.objects.select_for_update().filter(
                id__in=[row.benefit_id for row in paid_rows if row.benefit_id],
                is_deleted=False,
            )
        }
        field_mapping = self._reconciliation_field_mapping()
        reconciliation_fields = (
            set(field_mapping)
            | set(field_mapping.values())
            | set(self._reconciliation_export_columns())
        )
        for row in paid_rows:
            benefit = locked_benefits.get(row.benefit_id)
            if benefit and benefit.status == BenefitConsumptionStatus.ACCEPTED:
                self._reconcile_bc(
                    pd.Series(row.submitted_data),
                    benefit,
                    reconciliation_fields,
                )
        upload.status = CsvReconciliationUpload.Status.SUCCESS
        upload.save(username=self.user.login_name)

        # A payroll is fully reconciled only when this approved upload has no
        # unmatched rows and every linked benefit has reached RECONCILED.
        # In particular, an all-unpaid or partial upload must leave the payroll
        # available for a later reconciliation upload.
        has_unreconciled_benefits = PayrollBenefitConsumption.objects.filter(
            payroll=payroll,
            is_deleted=False,
        ).exclude(benefit__status=BenefitConsumptionStatus.RECONCILED).exists()
        if paid_rows and not upload.unmatched_records and not has_unreconciled_benefits \
                and payroll.status != PayrollStatus.RECONCILED:
            payroll.status = PayrollStatus.RECONCILED
            payroll.save(username=self.user.login_name)
        reconcile_consolidated_sources(payroll, self.user)
        return upload

    def download_upload_review(self, upload, contents):
        """Return the uploaded CSV using the fixed payroll export schema."""
        try:
            df = pd.read_csv(BytesIO(contents), dtype=str, keep_default_na=False)
        except Exception as exc:
            raise ValueError(
                "Unable to safely hide payment references from this reconciliation review."
            ) from exc
        hidden_headers = {
            header.lower() for header in self._payment_reference_headers()
        }
        df.drop(
            columns=[
                column for column in df.columns if column.strip().lower() in hidden_headers
            ],
            inplace=True,
            errors="ignore",
        )
        df = df.reindex(columns=self._reconciliation_export_columns())
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
                payrollbenefitconsumption__payroll=payroll,
                payrollbenefitconsumption__is_deleted=False,
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
            ("national_id_mismatch", "individual__json_ext__national_id",
             (individual.json_ext or {}).get("national_id"), self._matches_text),
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

        paid = self._clean_value(row.get(RECONCILIATION_PAID_COLUMN))
        if paid not in [PayrollConfig.csv_reconciliation_paid_yes, PayrollConfig.csv_reconciliation_paid_no]:
            reasons.append("invalid_paid_value")
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
    def _has_blocking_reason(reasons, payroll_type=None):
        blocking_reasons = set(PayrollConfig.csv_reconciliation_blocking_mismatch_reasons or [])
        warning_reasons = set(PayrollConfig.csv_reconciliation_warning_mismatch_reasons or [])
        if payroll_type == Payroll.PayrollType.STANDARD:
            warning_reasons.add("national_id_mismatch")
            blocking_reasons.discard("national_id_mismatch")
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
        kyc_fields = (
            set(KYC_PAYMENT_DETAILS_FIELD_MAPPING)
            | set(LEGACY_KYC_PAYMENT_DETAILS_FIELD_MAPPING)
        )
        kyc_headers = set(KYC_PAYMENT_DETAILS_FIELD_MAPPING.values())
        export_headers = set(self._reconciliation_export_columns())
        required_columns = {
            header
            for field, header in (PayrollConfig.csv_reconciliation_field_mapping or {}).items()
            if (
                header in export_headers
                and field not in kyc_fields
                and header not in kyc_headers
                and field.rsplit("__", 1)[-1].lower() != "receipt"
                and header.strip().lower() not in {
                    item.lower() for item in self._payment_reference_headers()
                }
            )
        } | {RECONCILIATION_PAID_COLUMN}
        missing_columns = required_columns - set(df.columns)
        if missing_columns:
            raise ValueError(
                _("Missing required columns: %(columns)s") % {
                    "columns": ", ".join(sorted(missing_columns)),
                }
            )
        if "Status" in df.columns:
            if (df["Status"] == BenefitConsumptionStatus.RECONCILED).all():
                raise ValueError(_("All of the Benefit Consumptions have been already reconciled."))

    def _fill_paid_column(self, row):
        if (
            "Status" in row
            and row["Status"] == BenefitConsumptionStatus.RECONCILED
        ):
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

    def _reconcile_bc(self, row, bc, reconciliation_fields=None):
        bc.status = BenefitConsumptionStatus.RECONCILED
        receipt = self._clean_value(row.get(PayrollConfig.csv_reconciliation_receipt_column))
        if receipt is not None:
            bc.receipt = receipt
        if reconciliation_fields is None:
            field_mapping = self._reconciliation_field_mapping()
            reconciliation_fields = (
                set(field_mapping)
                | set(field_mapping.values())
                | set(self._reconciliation_export_columns())
            )
        extra_info = {
            k: row[k] for k in row.index
            if k not in reconciliation_fields
            and k != self.micro_catchment_column
            and not pd.isna(row[k])
        }
        bc.json_ext = {
            **(bc.json_ext or {}),
            'extra_info': extra_info,
        }
        bc.save(username=self.user.login_name)
        bill = Bill.objects.filter(benefitattachment__benefit=bc, is_deleted=False).first()
        if bill:
            self._reconcile_bill(row, bill, bc.withdrawal_fee or Decimal("0.00"))

    def _reconcile_bill(self, row, bill, withdrawal_fee=Decimal("0.00")):
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
            "fees": withdrawal_fee,
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
            'fees': withdrawal_fee,
            'amount': bill.amount_total,
            'reconcilation_id': self._clean_value(
                row.get(PayrollConfig.csv_reconciliation_receipt_column)
            ),
            'reconcilation_date': current_date,
        }
        bill_payment_details = DetailPaymentInvoice(**bill_payment_details)
        payment_service = PaymentInvoiceService(self.user)
        payment_service.create_with_detail(bill_payment, bill_payment_details)
