"""Benefit Plan authorization helpers for payroll-owned data.

The shared service is optional so payroll remains compatible with installations
using a social-protection release from before the service was introduced.
"""
from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.db.models import Exists, OuterRef, Q


def _user(user_or_info):
    return getattr(getattr(user_or_info, "context", None), "user", user_or_info)


def _configured_benefit_plan_access_control_enabled():
    if not apps.is_installed("social_protection"):
        return False
    app_config = apps.get_app_config("social_protection")
    return bool(
        getattr(app_config, "enable_benefit_plan_access_control", False)
    )


def _shared_benefit_plan_access_helpers():
    try:
        from social_protection.benefit_plan_access import (
            benefit_plan_access_control_enabled,
            get_accessible_benefit_plan_ids,
            require_benefit_plan_access,
        )
    except ImportError as exc:
        if _configured_benefit_plan_access_control_enabled():
            raise ImproperlyConfigured(
                "Benefit Plan access control is enabled, but the shared Social "
                "Protection authorization service is unavailable."
            ) from exc
        return None, None
    # Resolve feature state through the shared service. This keeps Payroll from
    # growing a second authorization-policy implementation.
    benefit_plan_access_control_enabled()
    return get_accessible_benefit_plan_ids, require_benefit_plan_access


def _benefit_plan_content_type():
    model = apps.get_model("social_protection", "BenefitPlan")
    return ContentType.objects.get_for_model(model)


def _accessible_ids(user):
    get_accessible_benefit_plan_ids, _ = _shared_benefit_plan_access_helpers()
    if get_accessible_benefit_plan_ids is None:
        return None
    ids = get_accessible_benefit_plan_ids(_user(user))
    if ids is None:
        return None
    return [str(value) for value in ids]


def require_benefit_plan_access(user, benefit_plan_id, include_deleted=False):
    _, require_access = _shared_benefit_plan_access_helpers()
    if require_access is None:
        return
    return require_access(
        _user(user),
        benefit_plan_id,
        include_deleted=include_deleted,
    )


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
    _, require_access = _shared_benefit_plan_access_helpers()
    if require_access is None or not payment_plan:
        return
    if payment_plan.benefit_plan_type_id != _benefit_plan_content_type().id:
        return
    require_access(_user(user), str(payment_plan.benefit_plan_id))


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
    content_type = _benefit_plan_content_type()
    payroll_benefit = apps.get_model("payroll", "PayrollBenefitConsumption")
    outer_benefit_id = f"{benefit_prefix}_id" if benefit_prefix else "pk"
    authorized_membership = payroll_benefit.objects.filter(
        Q(benefit_id=OuterRef(outer_benefit_id), is_deleted=False)
        & (
            ~Q(payroll__payment_plan__benefit_plan_type_id=content_type.id)
            | Q(payroll__payment_plan__benefit_plan_id__in=ids)
        )
    )
    return queryset.filter(Exists(authorized_membership))


def require_benefit_access(user, benefit):
    if not filter_benefit_queryset(
        benefit.__class__.objects.filter(id=benefit.id, is_deleted=False), user
    ).exists():
        raise PermissionDenied("unauthorized")
    return benefit
