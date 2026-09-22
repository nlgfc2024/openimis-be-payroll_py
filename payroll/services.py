import logging
import pandas as pd
from io import BytesIO

from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import transaction
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
    PayrollBenefitConsumption,
    BenefitConsumption,
    BenefitAttachment,
    BenefitConsumptionStatus
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
        sequence = 1
        while True:
            name = PayrollNameGenerator.generate(
                payment_plan, payment_cycle, project_names, sequence
            )
            if not Payroll.objects.filter(name=name, is_deleted=False).exists():
                return name
            sequence += 1

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

        project_ids = filter_criteria.get("project_ids", [])
        if project_ids:
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


class CsvReconciliationService:
    def __init__(self, user: InteractiveUser):
        self.user = user

    def download_reconciliation(self, payroll_id) -> BytesIO:
        payroll = self._resolve_payroll(payroll_id)
        bc_qs = self._get_benefit_consumption_qs(payroll)
        # Retrieve the basic fields
        field_keys = list(PayrollConfig.csv_reconciliation_field_mapping.keys())
        records = list(bc_qs.values(*field_keys))

        # Collect all extra_info keys to ensure all columns are present in the DataFrame
        extra_info_keys = set()
        extra_info_dicts = []  # To store extra_info dicts for each record
        for record in records:
            bc = bc_qs.get(code=record['code'])
            extra_info = bc.json_ext.get('extra_info', {}) if bc.json_ext else {}
            extra_info_keys.update(extra_info.keys())
            extra_info_dicts.append(extra_info)

        # Convert to DataFrame
        df = pd.DataFrame.from_records(records)

        for key in extra_info_keys:
            if key not in df.columns:
                df[key] = None

        # Add paid extra field
        df[PayrollConfig.csv_reconciliation_paid_extra_field] = df.apply(
            lambda row: self._fill_paid_column(row), axis=1
        )
        df.rename(columns=PayrollConfig.csv_reconciliation_field_mapping, inplace=True)

        # Add extra_info fields at the end of the DataFrame
        for key in extra_info_keys:
            df[key] = [extra_info_dict.get(key, None) for extra_info_dict in extra_info_dicts]

        in_memory_file = BytesIO()
        # BytesIO is duck-typed as a file object, so it can be passed to df.to_csv
        # noinspection PyTypeChecker
        df.to_csv(in_memory_file, index=False)
        return in_memory_file

    def upload_reconciliation(self, payroll_id, file, upload):
        payroll = self._resolve_payroll(payroll_id)
        upload.payroll = payroll
        upload.status = upload.Status.IN_PROGRESS
        upload.save(username=self.user.login_name)
        if not file:
            raise ValueError(_('csv_reconciliation.validation.file_required'))
        df = pd.read_csv(file)
        self._validate_dataframe(df)
        df.rename(columns={v: k for k, v in PayrollConfig.csv_reconciliation_field_mapping.items()}, inplace=True)

        affected_rows = 0
        skipped_items = 0
        total_number_of_benefits_in_file = len(df)

        df[PayrollConfig.csv_reconciliation_errors_column] = df.apply(lambda row: self._reconcile_row(payroll, row),
                                                                      axis=1)

        for __, row in df.iterrows():
            if not pd.isna(row[PayrollConfig.csv_reconciliation_errors_column]):
                skipped_items += 1
            else:
                affected_rows += 1

        summary = {
            'affected_rows': affected_rows,
            'total_number_of_benefits_in_file': total_number_of_benefits_in_file,
            'skipped_items': skipped_items
        }

        error_df = df[df[PayrollConfig.csv_reconciliation_errors_column].apply(lambda x: bool(x))]
        if not error_df.empty:
            in_memory_file = BytesIO()
            df.rename(columns={k: v for k, v in PayrollConfig.csv_reconciliation_field_mapping.items()}, inplace=True)
            df.to_csv(in_memory_file, index=False)
            return in_memory_file, error_df.set_index(PayrollConfig.csv_reconciliation_code_column)[
                PayrollConfig.csv_reconciliation_errors_column
            ].to_dict(), summary
        return file, None, summary

    def _get_benefit_consumption_qs(self, payroll):
        qs = BenefitConsumption.objects.filter(payrollbenefitconsumption__payroll=payroll, is_deleted=False)
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

    def _reconcile_row(self, payroll, row):
        errors = []
        bc = BenefitConsumption.objects.filter(code=row['code'], is_deleted=False).first()
        if not bc:
            errors.append(_('benefit_consumption_not_found'))
        if not bc.payrollbenefitconsumption_set.filter(payroll=payroll).exists():
            errors.append(_('benefit_consumption_not_in_payroll'))
        if (row[PayrollConfig.csv_reconciliation_paid_extra_field]
                and row[PayrollConfig.csv_reconciliation_paid_extra_field]
                not in [PayrollConfig.csv_reconciliation_paid_yes, PayrollConfig.csv_reconciliation_paid_no]):
            errors.append(_('paid_column_invalid_value'))

        if not row[PayrollConfig.csv_reconciliation_receipt_column]:
            errors.append(_('receipt_required'))

        if bc and bc.status != row['status']:
            errors.append(_('status_not_matching'))

        if (not errors
                and (row[PayrollConfig.csv_reconciliation_paid_extra_field] == PayrollConfig.csv_reconciliation_paid_yes
                     and bc.status == BenefitConsumptionStatus.ACCEPTED)):
            self._reconcile_bc(row, bc)

        return errors if errors else None

    def _reconcile_bc(self, row, bc):
        bc.status = BenefitConsumptionStatus.RECONCILED
        bc.receipt = row[PayrollConfig.csv_reconciliation_receipt_column]
        extra_info = {k: row[k] for k in row.index
                      if k not in PayrollConfig.csv_reconciliation_field_mapping and not pd.isna(row[k])}
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
