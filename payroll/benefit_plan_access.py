"""Benefit Plan authorization helpers for payroll-owned data.

The shared service is optional so payroll remains compatible with installations
using a social-protection release from before the service was introduced.
"""
from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied
from django.db.models import Q

try:
    from social_protection.benefit_plan_access import (
        get_accessible_benefit_plan_ids,
        require_benefit_plan_access,
    )
except ImportError:  # pragma: no cover - compatibility with older module releases
    def get_accessible_benefit_plan_ids(user):
        return None

    def require_benefit_plan_access(user, benefit_plan_id, include_deleted=False):
        return None


def _user(user_or_info):
    return getattr(getattr(user_or_info, "context", None), "user", user_or_info)


def _benefit_plan_content_type():
    model = apps.get_model("social_protection", "BenefitPlan")
    return ContentType.objects.get_for_model(model)


def _accessible_ids(user):
    ids = get_accessible_benefit_plan_ids(_user(user))
    if ids is None:
        return None
    return [str(value) for value in ids]


def filter_payroll_queryset(queryset, user, payroll_prefix=""):
    """Scope payrolls while retaining legacy, non-SP Payment Plans."""
    ids = _accessible_ids(user)
    if ids is None:
        return queryset
    prefix = f"{payroll_prefix}__" if payroll_prefix else ""
    content_type = _benefit_plan_content_type()
    sp_type = f"{prefix}payment_plan__benefit_plan_type_id"
    sp_id = f"{prefix}payment_plan__benefit_plan_id__in"
    return queryset.filter(
        ~Q(**{sp_type: content_type.id}) | Q(**{sp_id: ids})
    )


def require_payment_plan_access(user, payment_plan):
    if not payment_plan or payment_plan.benefit_plan_type_id != _benefit_plan_content_type().id:
        return
    require_benefit_plan_access(_user(user), str(payment_plan.benefit_plan_id))


def require_payroll_access(user, payroll):
    require_payment_plan_access(user, payroll.payment_plan)
    return payroll


def get_authorized_payroll(user, payroll_id, *, for_update=False):
    from payroll.models import Payroll

    queryset = Payroll.objects.filter(id=payroll_id, is_deleted=False).select_related("payment_plan")
    if for_update:
        queryset = queryset.select_for_update()
    payroll = filter_payroll_queryset(queryset, user).first()
    if payroll is None:
        raise PermissionDenied("unauthorized")
    return payroll


def filter_benefit_queryset(queryset, user, benefit_prefix=""):
    ids = _accessible_ids(user)
    if ids is None:
        return queryset
    prefix = f"{benefit_prefix}__" if benefit_prefix else ""
    return filter_payroll_queryset(
        queryset, user, f"{prefix}payrollbenefitconsumption__payroll"
    ).filter(**{f"{prefix}payrollbenefitconsumption__is_deleted": False}).distinct()


def require_benefit_access(user, benefit):
    if not filter_benefit_queryset(
        benefit.__class__.objects.filter(id=benefit.id, is_deleted=False), user
    ).exists():
        raise PermissionDenied("unauthorized")
    return benefit
