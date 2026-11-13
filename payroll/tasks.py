import logging
from celery import shared_task

from core.models import User
from payroll.models import Payroll, PayrollStatus, BenefitConsumptionStatus
from payroll.consolidation import (
    ensure_payroll_is_not_consolidated_source,
)
from payroll.strategies import StrategyOnlinePayment
from payroll.payments_registry import PaymentMethodStorage

logger = logging.getLogger(__name__)


@shared_task
def send_requests_to_gateway_payment(payroll_id, user_id):
    payroll = Payroll.objects.get(id=payroll_id)
    ensure_payroll_is_not_consolidated_source(payroll, "pay")
    strategy = PaymentMethodStorage.get_chosen_payment_method(payroll.payment_method)
    if strategy:
        user = User.objects.get(id=user_id)
        strategy.initialize_payment_gateway(payroll.payment_point)
        strategy.make_payment_for_payroll(payroll, user)


@shared_task
def send_request_to_reconcile(payroll_id, user_id):
    payroll = Payroll.objects.get(id=payroll_id)
    ensure_payroll_is_not_consolidated_source(payroll, "reconcile")
    user = User.objects.get(id=user_id)
    strategy = StrategyOnlinePayment
    strategy.initialize_payment_gateway(payroll.payment_point)
    benefits = strategy.get_benefits_attached_to_payroll(payroll, BenefitConsumptionStatus.APPROVE_FOR_PAYMENT)
    payment_gateway_connector = strategy.PAYMENT_GATEWAY
    benefits_to_reconcile = []
    for benefit in benefits:
        is_reconciled = payment_gateway_connector.reconcile(benefit.code, benefit.amount)
        # Initialize json_ext if it is None
        if benefit.json_ext is None:
            benefit.json_ext = {}
        if is_reconciled:
            new_json_ext = benefit.json_ext.copy() if benefit.json_ext else {}
            new_json_ext['output_gateway'] = is_reconciled
            new_json_ext['gateway_reconciliation_success'] = True
            benefit.json_ext = {**benefit.json_ext, **new_json_ext}
            benefits_to_reconcile.append(benefit)
        else:
            # Handle the case where a benefit payment is rejected
            new_json_ext = benefit.json_ext.copy() if benefit.json_ext else {}
            new_json_ext['output_gateway'] = is_reconciled
            new_json_ext['gateway_reconciliation_success'] = False
            benefit.json_ext = {**benefit.json_ext, **new_json_ext}
            benefit.save(username=user.login_name)
            logger.info(f"Payment for benefit ({benefit.code}) was rejected.")
    if benefits_to_reconcile:
        strategy.reconcile_benefit_consumption(benefits_to_reconcile, user)
    strategy.change_status_of_payroll(payroll, PayrollStatus.RECONCILED, user)
