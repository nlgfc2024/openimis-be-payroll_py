"""Benefit Plan authorization helpers for payroll-owned data."""
from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Exists, OuterRef, Q
from uuid import UUID

from social_protection.benefit_plan_access import (
    benefit_plan_access_control_enabled,
    get_accessible_benefit_plan_ids,
    require_benefit_plan_access as require_shared_benefit_plan_access,
)


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


def require_benefit_plan_access(user, benefit_plan_id, include_deleted=False):
    return require_shared_benefit_plan_access(
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
    if not payment_plan:
        return
    if payment_plan.benefit_plan_type_id != _benefit_plan_content_type().id:
        return
    require_shared_benefit_plan_access(
        _user(user), str(payment_plan.benefit_plan_id)
    )


def task_ownership_for_payment_plan(payment_plan):
    """Return the explicit Task ownership for one Payment Plan."""
    if not benefit_plan_access_control_enabled():
        return {}
    if payment_plan is None or payment_plan.benefit_plan_type_id is None:
        raise ValidationError("Payroll Task ownership cannot be resolved.")
    if payment_plan.benefit_plan_type_id != _benefit_plan_content_type().id:
        return {"benefit_plan_is_shared": True}
    try:
        benefit_plan_id = str(UUID(str(payment_plan.benefit_plan_id)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValidationError("Payroll Task Benefit Plan ID is invalid.") from exc
    return {"benefit_plan_id": benefit_plan_id}


def task_ownership_for_payroll(payroll):
    if not benefit_plan_access_control_enabled():
        return {}
    return task_ownership_for_payment_plan(payroll.payment_plan)


def task_ownership_for_benefit(benefit):
    if not benefit_plan_access_control_enabled():
        return {}
    links = benefit.payrollbenefitconsumption_set.filter(
        is_deleted=False,
    ).select_related("payroll__payment_plan__benefit_plan_type")
    ownerships = [task_ownership_for_payroll(link.payroll) for link in links]
    if not ownerships:
        raise ValidationError("Benefit Task ownership cannot be resolved.")
    plan_ids = {
        ownership["benefit_plan_id"]
        for ownership in ownerships
        if ownership.get("benefit_plan_id")
    }
    has_shared = any(
        ownership.get("benefit_plan_is_shared", False)
        for ownership in ownerships
    )
    if len(plan_ids) == 1 and not has_shared:
        return {"benefit_plan_id": plan_ids.pop()}
    if not plan_ids and has_shared:
        return {"benefit_plan_is_shared": True}
    raise ValidationError("Benefit Task refers to multiple programme owners.")


def require_payroll_access(user, payroll):
    require_payment_plan_access(user, payroll.payment_plan)
    return payroll


def get_authorized_payroll(user, payroll_id, *, for_update=False):
    from payroll.models import Payroll

    queryset = Payroll.objects.filter(id=payroll_id, is_deleted=False).select_related("payment_plan")
    if for_update:
        queryset = queryset.select_for_update(of=("self",))
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
