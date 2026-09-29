import logging

from core.models import User
from core.service_signals import ServiceSignalBindType
from core.signals import bind_service_signal
from openIMIS.openimisapps import openimis_apps
from tasks_management.models import Task
from payroll.apps import PayrollConfig
from payroll.models import Payroll, BenefitConsumption, BenefitConsumptionStatus, CsvReconciliationUpload
from payroll.payments_registry import PaymentMethodStorage
from payroll.services import PayrollService, CsvReconciliationService
from payroll.strategies import StrategyOfPaymentInterface


logger = logging.getLogger(__name__)
imis_modules = openimis_apps()


def bind_service_signals():
    def on_task_complete_accept_payroll(**kwargs):
        def accept_payroll(payroll, strategy, user):
            if strategy:
                strategy.accept_payroll(payroll, user)

        def reject_payroll(payroll, strategy, user):
            if strategy:
                strategy.reject_payroll(payroll, user)

        try:
            result = kwargs.get('result', None)
            task = result['data']['task']
            user = User.objects.get(id=result['data']['user']['id'])
            if result \
                    and result['success'] \
                    and task['business_event'] == PayrollConfig.payroll_accept_event:
                task_status = task['status']
                payroll = Payroll.objects.get(id=task['entity_id'])
                strategy = PaymentMethodStorage.get_chosen_payment_method(payroll.payment_method)
                if task_status == Task.Status.COMPLETED:
                    accept_payroll(payroll, strategy, user)
                if task_status == Task.Status.FAILED:
                    reject_payroll(payroll, strategy, user)
        except Exception as exc:
            logger.error("Error while executing on_task_complete_accept_payroll", exc_info=exc)

    def on_task_complete_payroll_reconcilation(**kwargs):
        def reconcile_payroll(payroll, user):
            strategy = PaymentMethodStorage.get_chosen_payment_method(payroll.payment_method)
            if strategy:
                strategy.reconcile_payroll(payroll, user)
        try:
            result = kwargs.get('result', None)
            task = result['data']['task']
            user = User.objects.get(id=result['data']['user']['id'])
            if result \
                    and result['success'] \
                    and task['business_event'] == PayrollConfig.payroll_reconciliation_event:
                task_status = task['status']
                if task_status == Task.Status.COMPLETED:
                    payroll = Payroll.objects.get(id=task['entity_id'])
                    upload_id = task.get('data', {}).get('upload_id')
                    if upload_id:
                        upload = CsvReconciliationUpload.objects.get(id=upload_id, payroll=payroll)
                        CsvReconciliationService(user).apply_upload(upload)
                    else:
                        reconcile_payroll(payroll, user)
                elif task_status == Task.Status.FAILED:
                    upload_id = task.get('data', {}).get('upload_id')
                    if upload_id:
                        CsvReconciliationUpload.objects.filter(id=upload_id, status=CsvReconciliationUpload.Status.WAITING_FOR_VERIFICATION).update(status=CsvReconciliationUpload.Status.FAIL)
        except Exception as exc:
            logger.error("Error while executing on_task_complete_payroll_reconciliation", exc_info=exc)

    def on_task_complete_payroll_reject_approved_payroll(**kwargs):
        def reject_approved_payroll(payroll, user):
            strategy = PaymentMethodStorage.get_chosen_payment_method(payroll.payment_method)
            if strategy:
                strategy.reject_approved_payroll(payroll, user)
        try:
            result = kwargs.get('result', None)
            task = result['data']['task']
            user = User.objects.get(id=result['data']['user']['id'])
            if result \
                    and result['success'] \
                    and task['business_event'] == PayrollConfig.payroll_reject_event:
                task_status = task['status']
                if task_status == Task.Status.COMPLETED:
                    payroll = Payroll.objects.get(id=task['entity_id'])
                    reject_approved_payroll(payroll, user)
        except Exception as exc:
            logger.error("Error while executing on_task_complete_reject_approved_payroll", exc_info=exc)

    def on_task_delete_payroll(**kwargs):
        def delete_payroll(payroll, user):
            strategy = PaymentMethodStorage.get_chosen_payment_method(payroll.payment_method)
            if strategy:
                strategy.remove_benefits_from_rejected_payroll(payroll=payroll)
                PayrollService(user).delete_instance(payroll)
        try:
            result = kwargs.get('result', None)
            task = result['data']['task']
            user = User.objects.get(id=result['data']['user']['id'])
            if result \
                    and result['success'] \
                    and task['business_event'] == PayrollConfig.payroll_delete_event:
                task_status = task['status']
                if task_status == Task.Status.COMPLETED:
                    payroll = Payroll.objects.get(id=task['entity_id'])
                    delete_payroll(payroll, user)
        except Exception as exc:
            logger.error("Error while executing on_task_complete_delete_payroll", exc_info=exc)

    def on_task_delete_benefit(**kwargs):
        def delete_benefit(benefit, user):
            StrategyOfPaymentInterface.remove_benefit_from_payroll(benefit=benefit)
        try:
            result = kwargs.get('result', None)
            task = result['data']['task']
            user = User.objects.get(id=result['data']['user']['id'])
            if result \
                    and result['success'] \
                    and task['business_event'] == PayrollConfig.benefit_delete_event:
                task_status = task['status']
                if task_status == Task.Status.COMPLETED:
                    benefit = BenefitConsumption.objects.get(id=task['entity_id'])
                    delete_benefit(benefit, user)
                if task_status == Task.Status.FAILED:
                    benefit = BenefitConsumption.objects.get(id=task['entity_id'])
                    benefit.status = BenefitConsumptionStatus.ACCEPTED
                    benefit.save(username=user.username)
        except Exception as exc:
            logger.error("Error while executing on_task_complete_delete_benefit", exc_info=exc)

    bind_service_signal(
        'task_service.complete_task',
        on_task_complete_accept_payroll,
        bind_type=ServiceSignalBindType.AFTER
    )

    bind_service_signal(
        'task_service.complete_task',
        on_task_complete_payroll_reconcilation,
        bind_type=ServiceSignalBindType.AFTER
    )

    bind_service_signal(
        'task_service.complete_task',
        on_task_complete_payroll_reject_approved_payroll,
        bind_type=ServiceSignalBindType.AFTER
    )

    bind_service_signal(
        'task_service.complete_task',
        on_task_delete_payroll,
        bind_type=ServiceSignalBindType.AFTER
    )

    bind_service_signal(
        'task_service.complete_task',
        on_task_delete_benefit,
        bind_type=ServiceSignalBindType.AFTER
    )
