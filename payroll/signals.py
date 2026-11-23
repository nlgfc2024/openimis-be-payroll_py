import logging

from django.core.exceptions import PermissionDenied
from core.models import User
from core.service_signals import ServiceSignalBindType
from core.signals import bind_service_signal
from openIMIS.openimisapps import openimis_apps
from tasks_management.models import Task
from tasks_management.services import TaskService
from payroll.apps import PayrollConfig
from payroll.consolidation import (
    ensure_benefit_is_not_in_active_consolidation,
    ensure_payroll_is_not_consolidated_source,
)
from payroll.models import Payroll, BenefitConsumption, BenefitConsumptionStatus, CsvReconciliationUpload
from payroll.payments_registry import PaymentMethodStorage
from payroll.services import PayrollService, CsvReconciliationService
from payroll.benefit_plan_access import get_authorized_payroll, require_benefit_access
from payroll.strategies import StrategyOfPaymentInterface


logger = logging.getLogger(__name__)
imis_modules = openimis_apps()


def _authorize_payroll_task_completion(**kwargs):
    """Validate live Payroll ownership before TaskService changes task state."""
    service = kwargs.get("cls_")
    signal_data = kwargs.get("data") or []
    positional = signal_data[0] if signal_data else ()
    obj_data = positional[0] if positional else None
    if not isinstance(obj_data, dict) or not obj_data.get("id"):
        return

    task = Task.objects.filter(id=obj_data["id"]).first()
    if task is None:
        return
    user = getattr(service, "user", None)
    payroll_events = {
        PayrollConfig.payroll_accept_event,
        PayrollConfig.payroll_reconciliation_event,
        PayrollConfig.payroll_reject_event,
        PayrollConfig.payroll_delete_event,
    }
    if task.business_event in payroll_events:
        get_authorized_payroll(user, task.entity_id)
    elif task.business_event == PayrollConfig.benefit_delete_event:
        benefit = BenefitConsumption.objects.get(id=task.entity_id)
        require_benefit_access(user, benefit)


def _mark_completed_task_failed(task, user):
    if not task or not user or task.get("status") != Task.Status.COMPLETED:
        return
    try:
        result = TaskService(user).complete_task(
            {"id": task["id"], "failed": True},
        )
    except Exception:
        logger.exception("Unable to mark completed task %s as failed", task.get("id"))
        return
    if not result or not result.get("success"):
        logger.error(
            "Unable to mark completed task %s as failed: %s",
            task.get("id"),
            result.get("error") if result else "Task service returned no result.",
        )


def bind_service_signals():
    bind_service_signal(
        'task_service.complete_task',
        _authorize_payroll_task_completion,
        bind_type=ServiceSignalBindType.BEFORE,
    )

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
                payroll = get_authorized_payroll(user, task['entity_id'])
                strategy = PaymentMethodStorage.get_chosen_payment_method(payroll.payment_method)
                if task_status == Task.Status.COMPLETED:
                    accept_payroll(payroll, strategy, user)
                if task_status == Task.Status.FAILED:
                    reject_payroll(payroll, strategy, user)
        except PermissionDenied as exc:
            logger.warning(
                "Benefit Plan access denied while completing payroll approval",
                exc_info=exc,
            )
        except Exception as exc:
            logger.error("Error while executing on_task_complete_accept_payroll", exc_info=exc)

    def on_task_complete_payroll_reconcilation(**kwargs):
        def reconcile_payroll(payroll, user):
            ensure_payroll_is_not_consolidated_source(payroll, "reconcile")
            strategy = PaymentMethodStorage.get_chosen_payment_method(payroll.payment_method)
            if strategy:
                strategy.reconcile_payroll(payroll, user)
        result = None
        user = None
        task = None
        try:
            result = kwargs.get('result', None)
            task = result['data']['task']
            user = User.objects.get(id=result['data']['user']['id'])
            if result \
                    and result['success'] \
                    and task['business_event'] == PayrollConfig.payroll_reconciliation_event:
                task_status = task['status']
                if task_status == Task.Status.COMPLETED:
                    payroll = get_authorized_payroll(user, task['entity_id'])
                    upload_id = task.get('data', {}).get('upload_id')
                    if upload_id:
                        upload = CsvReconciliationUpload.objects.get(id=upload_id, payroll=payroll)
                        CsvReconciliationService(user).apply_upload(upload)
                    else:
                        reconcile_payroll(payroll, user)
                elif task_status == Task.Status.FAILED:
                    get_authorized_payroll(user, task['entity_id'])
                    upload_id = task.get('data', {}).get('upload_id')
                    if upload_id:
                        CsvReconciliationUpload.objects.filter(id=upload_id, status=CsvReconciliationUpload.Status.WAITING_FOR_VERIFICATION).update(status=CsvReconciliationUpload.Status.FAIL)
        except PermissionDenied as exc:
            logger.warning(
                "Benefit Plan access denied while completing payroll reconciliation",
                exc_info=exc,
            )
        except Exception as exc:
            logger.error("Error while executing on_task_complete_payroll_reconciliation", exc_info=exc)
            # A completed checker task has already emitted its completion
            # signal. Make both records reflect an unsuccessful application
            # and leave the payroll eligible for a new upload/review.
            if (
                result and result.get("success") and task
                and task.get("business_event") == PayrollConfig.payroll_reconciliation_event
                and task.get("status") == Task.Status.COMPLETED
            ):
                upload_id = task.get("data", {}).get("upload_id")
                if upload_id:
                    failed_upload = CsvReconciliationUpload.objects.filter(
                        id=upload_id,
                        status=CsvReconciliationUpload.Status.WAITING_FOR_VERIFICATION,
                    ).first()
                    if failed_upload:
                        failed_upload.status = CsvReconciliationUpload.Status.FAIL
                        failed_upload.error = {
                            **(failed_upload.error or {}),
                            "apply_error": str(exc),
                        }
                        failed_upload.save(username=user.login_name if user else None)
                _mark_completed_task_failed(task, user)

    def on_task_complete_payroll_reject_approved_payroll(**kwargs):
        def reject_approved_payroll(payroll, user):
            strategy = PaymentMethodStorage.get_chosen_payment_method(payroll.payment_method)
            if strategy:
                strategy.reject_approved_payroll(payroll, user)
        result = None
        user = None
        task = None
        try:
            result = kwargs.get('result', None)
            task = result['data']['task']
            user = User.objects.get(id=result['data']['user']['id'])
            if result \
                    and result['success'] \
                    and task['business_event'] == PayrollConfig.payroll_reject_event:
                task_status = task['status']
                if task_status == Task.Status.COMPLETED:
                    payroll = get_authorized_payroll(user, task['entity_id'])
                    reject_approved_payroll(payroll, user)
        except Exception as exc:
            logger.error("Error while executing on_task_complete_reject_approved_payroll", exc_info=exc)
            _mark_completed_task_failed(task, user)

    def on_task_delete_payroll(**kwargs):
        def delete_payroll(payroll, user):
            ensure_payroll_is_not_consolidated_source(payroll, "delete")
            strategy = PaymentMethodStorage.get_chosen_payment_method(payroll.payment_method)
            if strategy:
                strategy.remove_benefits_from_rejected_payroll(payroll=payroll, user=user)
                PayrollService(user).delete_instance(payroll)
        result = None
        user = None
        task = None
        try:
            result = kwargs.get('result', None)
            task = result['data']['task']
            user = User.objects.get(id=result['data']['user']['id'])
            if result \
                    and result['success'] \
                    and task['business_event'] == PayrollConfig.payroll_delete_event:
                task_status = task['status']
                if task_status == Task.Status.COMPLETED:
                    payroll = get_authorized_payroll(user, task['entity_id'])
                    delete_payroll(payroll, user)
        except Exception as exc:
            logger.error("Error while executing on_task_complete_delete_payroll", exc_info=exc)
            _mark_completed_task_failed(task, user)

    def on_task_delete_benefit(**kwargs):
        def delete_benefit(benefit, user):
            ensure_benefit_is_not_in_active_consolidation(benefit, "delete")
            StrategyOfPaymentInterface.remove_benefit_from_payroll(benefit=benefit)
        result = None
        user = None
        task = None
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
                    require_benefit_access(user, benefit)
                    delete_benefit(benefit, user)
                if task_status == Task.Status.FAILED:
                    benefit = BenefitConsumption.objects.get(id=task['entity_id'])
                    require_benefit_access(user, benefit)
                    benefit.status = BenefitConsumptionStatus.ACCEPTED
                    benefit.save(username=user.username)
        except Exception as exc:
            logger.error("Error while executing on_task_complete_delete_benefit", exc_info=exc)
            _mark_completed_task_failed(task, user)

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
