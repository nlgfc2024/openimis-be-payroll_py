import hashlib
import logging
import uuid
from decimal import Decimal, InvalidOperation
import pandas as pd
from io import BytesIO

from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import transaction
from simple_history.utils import bulk_create_with_history
from django.db.models import Q
from django.utils.translation import gettext as _

from core import datetime
from core.custom_filters import CustomFilterWizardStorage
from core.models import InteractiveUser
from core.services import BaseService
from core.signals import register_service_signal
from invoice.models import Bill, PaymentInvoice, DetailPaymentInvoice
from invoice.services import PaymentInvoiceService
from payment_cycle.models import PaymentCycle
from payroll.apps import PayrollConfig
from payroll.models import (
    PaymentPoint,
    Payroll,
    PayrollStatus,
    PayrollBenefitConsumption,
    BenefitConsumption,
    BenefitAttachment,
    BenefitConsumptionStatus,
    CsvReconciliationUpload,
    ReconciliationUploadRow,
    DistrictPayrollBatch,
    DistrictPayrollBatchPayroll,
    DistrictPayrollBatchStatus,
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
                    beneficiaries_queryset = self._select_beneficiary_based_on_criteria(obj_data, payment_plan)
                    self._generate_benefits(
                        payment_plan,
                        beneficiaries_queryset,
                        date_valid_from,
                        date_valid_to,
                        payroll,
                        payment_cycle
                    )
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
            # Project-scoped payrolls use the intersection: the household's
            # primary recipient must also be explicitly enrolled in one of
            # the projects selected for the payroll.
            beneficiaries_queryset = beneficiaries_queryset.filter(
                project_enrollments__project__id__in=project_ids,
                project_enrollments__is_deleted=False
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

    def _generate_benefits(self, payment_plan, beneficiaries_queryset, date_from, date_to, payroll, payment_cycle):
        calculation = get_calculation_object(payment_plan.calculation)
        calculation.calculate_if_active_for_object(
            payment_plan,
            user_id=self.user.id,
            start_date=date_from, end_date=date_to,
            beneficiaries_queryset=beneficiaries_queryset,
            payroll=payroll,
            payment_cycle=payment_cycle
        )

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


class DistrictWagesheetService:
    """Build and reconcile a District Wagesheet without merging source payrolls."""

    source_payroll_id_column = "Source Payroll ID"
    source_payroll_name_column = "Source Payroll Name"
    project_name_column = "Project Name"
    catchment_column = "Catchment Name"
    district_approval_source = "district_wagesheet_district_approval"
    national_approval_source = "district_wagesheet_national_approval"

    def __init__(self, user: InteractiveUser):
        self.user = user

    @staticmethod
    def _project_id(payroll):
        criteria = (payroll.json_ext or {}).get("filter_criteria", {})
        project_ids = {str(project_id) for project_id in criteria.get("project_ids", []) if project_id}
        # District reconciliation is only safe for a single-project payroll.
        return next(iter(project_ids)) if len(project_ids) == 1 else None

    @staticmethod
    def _district_id(project):
        from location.models import Location

        location = project.location
        while location and location.type != "R":
            location = Location.objects.filter(id=location.parent_id).only("id", "type", "parent_id").first()
        return str(location.id) if location else None

    @staticmethod
    def _financial_year(payment_cycle):
        """Return the government financial year containing the cycle start date."""
        if not payment_cycle or not payment_cycle.start_date:
            return None
        start_month = int(PayrollConfig.financial_year_start_month or 7)
        start_year = payment_cycle.start_date.year
        if payment_cycle.start_date.month < start_month:
            start_year -= 1
        return f"{start_year}/{str(start_year + 1)[-2:]}"

    def _eligible_payrolls(self, district_id, benefit_plan_id, financial_year=None):
        project_model = apps.get_model("project_social_protection", "Project")
        payrolls = Payroll.objects.filter(
            payment_plan__benefit_plan_id=str(benefit_plan_id),
            status=PayrollStatus.APPROVE_FOR_PAYMENT,
            is_deleted=False,
        ).select_related("payment_cycle", "payment_plan")
        project_ids = {project_id for payroll in payrolls if (project_id := self._project_id(payroll))}
        projects = {
            str(project.id): project
            for project in project_model.objects.filter(id__in=project_ids, is_deleted=False).select_related("location")
        }
        return [
            payroll for payroll in payrolls
            if (project := projects.get(self._project_id(payroll)))
            and self._district_id(project) == str(district_id)
            and (not financial_year or self._financial_year(payroll.payment_cycle) == financial_year)
        ]

    def available_financial_years(self, district_id, benefit_plan_id):
        return sorted({
            year for payroll in self._eligible_payrolls(district_id, benefit_plan_id)
            if (year := self._financial_year(payroll.payment_cycle))
        }, reverse=True)

    def _refresh_summary(self, batch):
        links = list(DistrictPayrollBatchPayroll.objects.filter(batch=batch).select_related("payroll"))
        reconciliation_service = CsvReconciliationService(self.user)
        project_model = apps.get_model("project_social_protection", "Project")
        project_ids = {self._project_id(link.payroll) for link in links if self._project_id(link.payroll)}
        projects = project_model.objects.filter(id__in=project_ids, is_deleted=False).select_related("micro_catchment")
        catchments = sorted({project.micro_catchment.name for project in projects if project.micro_catchment})
        participants = 0
        total_wages = Decimal("0")
        for link in links:
            benefits = reconciliation_service._get_benefit_consumption_qs(link.payroll)
            participants += benefits.count()
            total_wages += sum((benefit.amount or Decimal("0") for benefit in benefits.only("amount")), Decimal("0"))
        total_fees = Decimal(batch.cash_out_fee_per_participant or 0) * participants
        batch.total_records = participants
        batch.total_wages = total_wages
        batch.total_cash_out_fees = total_fees
        batch.summary = {
            "catchments": catchments,
            "projects": projects.count(),
            "participants": participants,
            "total_wages": str(total_wages),
            "total_cash_out_fees": str(total_fees),
        }
        batch.save(update_fields=[
            "total_records", "total_wages", "total_cash_out_fees", "summary", "updated_at",
        ])

    @transaction.atomic
    def get_or_create_batch(self, district_id, benefit_plan_id, financial_year=None):
        financial_years = self.available_financial_years(district_id, benefit_plan_id)
        if not financial_year:
            if len(financial_years) != 1:
                raise ValueError("Select a Financial Year for this district and phase.")
            financial_year = financial_years[0]
        eligible_payrolls = self._eligible_payrolls(district_id, benefit_plan_id, financial_year)
        if not eligible_payrolls:
            raise ValueError("No approved, single-project payrolls were found for this district, phase, and Financial Year.")

        batch = DistrictPayrollBatch.objects.select_for_update().filter(
            district_id=district_id,
            benefit_plan_id=str(benefit_plan_id),
            financial_year=financial_year,
        ).first()
        if not batch:
            batch = DistrictPayrollBatch(
                district_id=district_id,
                benefit_plan_id=str(benefit_plan_id),
                financial_year=financial_year,
                status=DistrictPayrollBatchStatus.DRAFT,
            )
            batch.id = uuid.uuid4()
            batch.save(force_insert=True)

        if batch.status != DistrictPayrollBatchStatus.DRAFT:
            return batch

        DistrictPayrollBatchPayroll.objects.filter(batch=batch).delete()
        links = []
        for payroll in eligible_payrolls:
            link = DistrictPayrollBatchPayroll(batch=batch, payroll=payroll)
            link.id = uuid.uuid4()
            links.append(link)
        DistrictPayrollBatchPayroll.objects.bulk_create(links)
        self._refresh_summary(batch)
        return batch

    def _create_approval_task(self, batch, stage):
        district_stage = stage == "district"
        TaskService(self.user).create({
            "source": self.district_approval_source if district_stage else self.national_approval_source,
            "entity": batch,
            "status": Task.Status.RECEIVED,
            "executor_action_event": TasksManagementConfig.default_executor_event,
            "business_event": (
                PayrollConfig.district_wagesheet_district_approval_event
                if district_stage else PayrollConfig.district_wagesheet_national_approval_event
            ),
            "data": _get_std_task_data_payload({
                "id": batch.id,
                "district": batch.district.name,
                "phase": batch.benefit_plan_id,
                "financial_year": batch.financial_year,
                "approval_stage": stage,
            }),
        })

    @transaction.atomic
    def submit_for_district_approval(
        self, district_id, benefit_plan_id, financial_year=None,
        bank_name=None, account_number=None, cash_out_fee_per_participant=None,
    ):
        batch = self.get_or_create_batch(district_id, benefit_plan_id, financial_year)
        if batch.status == DistrictPayrollBatchStatus.DRAFT:
            batch.bank_name = bank_name or batch.bank_name
            batch.account_number = account_number or batch.account_number
            if cash_out_fee_per_participant not in (None, ""):
                try:
                    batch.cash_out_fee_per_participant = Decimal(str(cash_out_fee_per_participant))
                except InvalidOperation as exc:
                    raise ValueError("Cash-out fee per participant must be a valid amount.") from exc
            batch.save(update_fields=["bank_name", "account_number", "cash_out_fee_per_participant", "updated_at"])
            self._refresh_summary(batch)
            batch.status = DistrictPayrollBatchStatus.PENDING_DISTRICT_APPROVAL
            batch.save(update_fields=["status", "updated_at"])
            self._create_approval_task(batch, "district")
        return batch

    @transaction.atomic
    def resolve_approval(self, batch_id, stage, approved):
        batch = DistrictPayrollBatch.objects.select_for_update().get(id=batch_id)
        expected = (
            DistrictPayrollBatchStatus.PENDING_DISTRICT_APPROVAL
            if stage == "district" else DistrictPayrollBatchStatus.PENDING_NATIONAL_APPROVAL
        )
        if batch.status != expected:
            return batch
        if not approved:
            batch.status = DistrictPayrollBatchStatus.DRAFT
            batch.save(update_fields=["status", "updated_at"])
            return batch
        if stage == "district":
            batch.status = DistrictPayrollBatchStatus.PENDING_NATIONAL_APPROVAL
            batch.save(update_fields=["status", "updated_at"])
            self._create_approval_task(batch, "national")
        else:
            batch.status = DistrictPayrollBatchStatus.READY_FOR_PSP
            batch.save(update_fields=["status", "updated_at"])
        return batch

    @transaction.atomic
    def download_reconciliation(self, district_id, benefit_plan_id, financial_year=None):
        batch = self.get_or_create_batch(district_id, benefit_plan_id, financial_year)
        if batch.status != DistrictPayrollBatchStatus.READY_FOR_PSP:
            raise ValueError("The District Wagesheet must complete district and national approval before the PSP file can be downloaded.")
        records = []
        reconciliation_service = CsvReconciliationService(self.user)
        project_model = apps.get_model("project_social_protection", "Project")
        project_ids = {
            self._project_id(link.payroll)
            for link in DistrictPayrollBatchPayroll.objects.filter(batch=batch).select_related("payroll")
            if self._project_id(link.payroll)
        }
        projects = {
            str(project.id): project
            for project in project_model.objects.filter(id__in=project_ids, is_deleted=False).select_related("micro_catchment")
        }
        field_mapping = PayrollConfig.csv_reconciliation_field_mapping
        for link in DistrictPayrollBatchPayroll.objects.filter(batch=batch).select_related("payroll"):
            payroll = link.payroll
            project = projects.get(self._project_id(payroll))
            benefits = reconciliation_service._get_benefit_consumption_qs(payroll)
            for benefit in benefits.select_related("individual", "individual__location"):
                record = BenefitConsumption.objects.filter(id=benefit.id).values(*field_mapping.keys()).first()
                record[self.source_payroll_id_column] = str(payroll.id)
                record[self.source_payroll_name_column] = payroll.name
                record[self.project_name_column] = project.name if project else None
                record[self.catchment_column] = (
                    project.micro_catchment.name if project and project.micro_catchment else None
                )
                record[PayrollConfig.csv_reconciliation_paid_extra_field] = reconciliation_service._fill_paid_column(record)
                records.append(record)

        df = pd.DataFrame.from_records(records)
        df.rename(columns=field_mapping, inplace=True)
        export_columns = [
            self.source_payroll_id_column,
            self.source_payroll_name_column,
            "Payroll Name",
            "Payroll Status",
            "District Name",
            self.catchment_column,
            self.project_name_column,
            "Form Number",
            "Phone Number",
            "First Name",
            "Last Name",
            "National ID",
            "Date of Birth",
            "Code",
            "Status",
            "Amount",
            "Type",
            "Receipt",
            PayrollConfig.csv_reconciliation_paid_extra_field,
        ]
        df = df.reindex(columns=export_columns)
        in_memory_file = BytesIO()
        df.to_csv(in_memory_file, index=False)
        batch.status = DistrictPayrollBatchStatus.SUBMITTED_TO_PSP
        batch.save(update_fields=["status", "updated_at"])
        return batch, in_memory_file

    @transaction.atomic
    def upload_reconciliation(self, district_id, benefit_plan_id, file, upload, financial_year=None):
        """Validate a returned provider file against the immutable batch membership."""
        if not file:
            raise ValueError("A reconciliation CSV file is required.")
        contents = file.read()
        if not contents:
            raise ValueError("Import file is empty")
        batch = self.get_or_create_batch(district_id, benefit_plan_id, financial_year)
        if batch.status != DistrictPayrollBatchStatus.SUBMITTED_TO_PSP:
            raise ValueError("This district batch is not available for a new reconciliation upload.")
        try:
            df = pd.read_csv(BytesIO(contents), dtype=str, keep_default_na=False)
        except Exception as exc:
            raise ValueError(_("Unable to read CSV file: %(error)s") % {"error": str(exc)})

        required = set(PayrollConfig.csv_reconciliation_field_mapping.values()) | {
            self.source_payroll_id_column, PayrollConfig.csv_reconciliation_paid_extra_field,
        }
        missing = required - set(df.columns)
        if missing:
            raise ValueError("Missing required columns: %s" % ", ".join(sorted(missing)))

        df.rename(columns={v: k for k, v in PayrollConfig.csv_reconciliation_field_mapping.items()}, inplace=True)
        links = {
            str(link.payroll_id): link.payroll
            for link in DistrictPayrollBatchPayroll.objects.select_related("payroll").filter(batch=batch)
        }
        upload.district_wagesheet = batch
        upload.checksum = hashlib.sha256(contents).hexdigest()
        upload.status = CsvReconciliationUpload.Status.VALIDATING
        upload.save(username=self.user.login_name)
        duplicate = CsvReconciliationUpload.objects.filter(
            district_wagesheet=batch,
            checksum=upload.checksum,
            is_deleted=False,
        ).exclude(id=upload.id).exclude(
            status__in=[CsvReconciliationUpload.Status.FAIL, CsvReconciliationUpload.Status.DUPLICATE],
        ).first()
        if duplicate:
            upload.duplicate_of = duplicate
            upload.status = CsvReconciliationUpload.Status.DUPLICATE
            upload.error = {"duplicate_file": {"upload_id": str(duplicate.id), "file_name": duplicate.file_name}}
            upload.save(username=self.user.login_name)
            return upload, CsvReconciliationService._upload_summary(upload)

        rows, errors = [], {}
        paid_column = PayrollConfig.csv_reconciliation_paid_extra_field
        code_column = PayrollConfig.csv_reconciliation_code_column
        for index, row in df.iterrows():
            row_number = index + 2
            payroll = links.get(str(row.get(self.source_payroll_id_column)).strip())
            code = CsvReconciliationService._clean_value(row.get(code_column))
            reasons = []
            benefit = None
            if not payroll:
                reasons.append("source_payroll_not_in_district_batch")
            elif not code:
                reasons.append("missing_code")
            else:
                benefit = BenefitConsumption.objects.filter(
                    code=code,
                    is_deleted=False,
                    payrollbenefitconsumption__payroll=payroll,
                    payrollbenefitconsumption__is_deleted=False,
                ).select_related("individual").first()
                if not benefit:
                    reasons.append("code_not_in_source_payroll")
                else:
                    if not CsvReconciliationService._amounts_match(row.get("amount"), benefit.amount):
                        reasons.append("amount_mismatch")
                    paid = CsvReconciliationService._clean_value(row.get(paid_column))
                    if paid not in (PayrollConfig.csv_reconciliation_paid_yes, PayrollConfig.csv_reconciliation_paid_no):
                        reasons.append("invalid_paid_value")
                    if not CsvReconciliationService._clean_value(row.get(PayrollConfig.csv_reconciliation_receipt_column)):
                        reasons.append("missing_payment_reference")
            record = ReconciliationUploadRow(
                upload=upload, user_created=self.user, user_updated=self.user,
                row_number=row_number, benefit=benefit, code=code,
                submitted_data={str(key): CsvReconciliationService._json_value(value) for key, value in row.items()},
                mismatch_reasons=reasons,
                status=ReconciliationUploadRow.Status.INVALID if reasons else ReconciliationUploadRow.Status.VALID,
            )
            record.set_pk()
            rows.append(record)
            if reasons:
                errors[str(row_number)] = reasons

        bulk_create_with_history(rows, ReconciliationUploadRow, default_user=self.user, batch_size=1000)
        upload.total_records = len(rows)
        upload.unmatched_records = len(errors)
        upload.matched_records = len(rows) - len(errors)
        valid_rows = [row for row in rows if row.status == ReconciliationUploadRow.Status.VALID]
        upload.paid_records = sum(row.submitted_data.get(paid_column) == PayrollConfig.csv_reconciliation_paid_yes for row in valid_rows)
        upload.unpaid_records = sum(row.submitted_data.get(paid_column) == PayrollConfig.csv_reconciliation_paid_no for row in valid_rows)
        upload.error = errors
        upload.status = CsvReconciliationUpload.Status.WAITING_FOR_VERIFICATION
        upload.save(username=self.user.login_name)
        batch.status = DistrictPayrollBatchStatus.PENDING_REVIEW
        batch.total_records = upload.total_records
        batch.matched_records = upload.matched_records
        batch.paid_records = upload.paid_records
        batch.unpaid_records = upload.unpaid_records
        batch.unmatched_records = upload.unmatched_records
        batch.save()
        benefit_plan = apps.get_model("social_protection", "BenefitPlan").objects.filter(
            id=benefit_plan_id, is_deleted=False,
        ).first()
        included_payrolls = [
            {"id": str(link.payroll_id), "name": link.payroll.name}
            for link in DistrictPayrollBatchPayroll.objects.filter(batch=batch).select_related("payroll")
        ]
        task = TaskService(self.user).create({
            "source": "payroll_reconciliation",
            "entity": batch,
            "status": Task.Status.RECEIVED,
            "executor_action_event": TasksManagementConfig.default_executor_event,
            "business_event": PayrollConfig.payroll_reconciliation_event,
            "data": _get_std_task_data_payload({
                "id": batch.id,
                "upload_id": upload.id,
                "district": batch.district.name,
                "phase": f"{benefit_plan.code} {benefit_plan.name}" if benefit_plan else batch.benefit_plan_id,
                "included_payrolls": included_payrolls,
                "summary": CsvReconciliationService._upload_summary(upload),
            }),
        })
        if not task.get("success", False):
            raise ValueError(task.get("error") or "Unable to create district reconciliation review task")
        return upload, CsvReconciliationService._upload_summary(upload)


# Backwards-compatible import name for this unmerged feature branch.
DistrictPayrollBatchService = DistrictWagesheetService


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
        for record in records:
            benefit = benefits_by_code[record["code"]]
            record["micro_catchment"] = self._get_micro_catchment(benefit)
            extra_info = benefit.json_ext.get("extra_info", {}) if benefit.json_ext else {}
            extra_info_keys.update(key for key in extra_info if key not in mapped_headers)
            extra_info_dicts.append(extra_info)

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
        for key in extra_info_keys:
            df[key] = [extra_info.get(key) for extra_info in extra_info_dicts]
            export_columns.append(key)
        df = df.reindex(columns=export_columns)

        in_memory_file = BytesIO()
        df.to_csv(in_memory_file, index=False)
        return in_memory_file

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
        if DistrictPayrollBatchPayroll.objects.filter(
            payroll=payroll,
            batch__status__in=[
                DistrictPayrollBatchStatus.PENDING_DISTRICT_APPROVAL,
                DistrictPayrollBatchStatus.PENDING_NATIONAL_APPROVAL,
                DistrictPayrollBatchStatus.READY_FOR_PSP,
                DistrictPayrollBatchStatus.SUBMITTED_TO_PSP,
                DistrictPayrollBatchStatus.PENDING_REVIEW,
            ],
        ).exists():
            raise ValueError(
                "This payroll belongs to an active District Wagesheet and must be reconciled through that wagesheet."
            )
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
                submitted_data={str(key): self._json_value(value) for key, value in row.items()},
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
        task = TaskService(self.user).create({
            "source": "payroll_reconciliation", "entity": payroll,
            "status": Task.Status.RECEIVED,
            "executor_action_event": TasksManagementConfig.default_executor_event,
            "business_event": PayrollConfig.payroll_reconciliation_event,
            "data": _get_std_task_data_payload({"id": payroll.id, "upload_id": upload.id}),
        })
        if not task.get("success", False):
            raise ValueError(task.get("error") or "Unable to create reconciliation review task")
        return BytesIO(contents), errors or None, self._upload_summary(upload)

    def _load_upload_context(self, payroll, codes):
        from location.models import Location, MicroCatchmentGVH

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
        if upload.district_wagesheet_id:
            batch = upload.district_wagesheet
            for link in DistrictPayrollBatchPayroll.objects.filter(batch=batch).select_related("payroll"):
                unresolved = PayrollBenefitConsumption.objects.filter(
                    payroll=link.payroll,
                    is_deleted=False,
                ).exclude(benefit__status=BenefitConsumptionStatus.RECONCILED).exists()
                if not unresolved:
                    link.payroll.status = PayrollStatus.RECONCILED
                    link.payroll.save(username=self.user.login_name)
            batch.status = DistrictPayrollBatchStatus.APPLIED
            batch.save()

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
