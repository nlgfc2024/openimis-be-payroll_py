from django.apps import apps
from django.db import transaction
from django.db.models import Q

from location.models import Location
from payroll.models import (
    BenefitConsumptionStatus,
    ConsolidatedPayrollMember,
    Payroll,
    PayrollBenefitConsumption,
    PayrollStatus,
)


def payrolls_in_district(payrolls, district_id):
    """Return payrolls whose criteria or legacy recipients match a district."""
    payrolls = list(payrolls)
    if not payrolls:
        return []

    criteria_by_payroll_id = {
        payroll.id: (payroll.json_ext or {}).get("filter_criteria") or {}
        for payroll in payrolls
    }
    project_ids = {
        project_id
        for criteria in criteria_by_payroll_id.values()
        for project_id in criteria.get("project_ids") or []
    }
    project_model = apps.get_model("project_social_protection", "Project")
    projects = project_model.objects.filter(
        id__in=project_ids,
        is_deleted=False,
    ).select_related(
        "location",
    )
    project_ids_by_payroll = {}
    project_location_ids = {}
    for payroll_id, criteria in criteria_by_payroll_id.items():
        project_location_ids[payroll_id] = set()
        for project_id in criteria.get("project_ids") or []:
            project_ids_by_payroll.setdefault(
                str(project_id),
                set(),
            ).add(payroll_id)
    for project in projects:
        for payroll_id in project_ids_by_payroll.get(str(project.id), set()):
            if project.location_id:
                project_location_ids[payroll_id].add(str(project.location_id))

    criteria_location_ids = {
        str(location_id)
        for criteria in criteria_by_payroll_id.values()
        for location_id in criteria.get("location_ids") or []
    }
    numeric_location_ids = {
        int(location_id)
        for location_id in criteria_location_ids
        if location_id.isdigit()
    }
    relevant_location_ids = numeric_location_ids.union(
        int(location_id)
        for locations in project_location_ids.values()
        for location_id in locations
    )
    locations = {}
    for location in Location.objects.filter(
        Q(id__in=relevant_location_ids) | Q(uuid__in=criteria_location_ids)
    ):
        locations[str(location.id)] = location
        locations[str(location.uuid)] = location
    while True:
        parent_ids = {
            str(location.parent_id)
            for location in locations.values()
            if location.parent_id and str(location.parent_id) not in locations
        }
        if not parent_ids:
            break
        parents = Location.objects.filter(id__in=parent_ids)
        if not parents:
            break
        locations.update({str(location.id): location for location in parents})

    def includes_district(location_ids):
        for location_id in location_ids:
            location = locations.get(str(location_id))
            while location:
                if str(location.id) == str(district_id):
                    return True
                location = (
                    locations.get(str(location.parent_id))
                    if location.parent_id else None
                )
        return False

    matching_payroll_ids = set()
    legacy_payroll_ids = []
    for payroll in payrolls:
        criteria = criteria_by_payroll_id[payroll.id]
        saved_project_ids = criteria.get("project_ids") or []
        saved_location_ids = criteria.get("location_ids") or []
        if includes_district(
            project_location_ids.get(payroll.id, set()).union(
                str(location_id) for location_id in saved_location_ids
            )
        ):
            matching_payroll_ids.add(payroll.id)
        elif not saved_project_ids and not saved_location_ids:
            legacy_payroll_ids.append(payroll.id)

    if legacy_payroll_ids:
        beneficiary_district = (
            Q(benefit__individual__location__id=district_id)
            | Q(benefit__individual__location__parent__id=district_id)
            | Q(benefit__individual__location__parent__parent__id=district_id)
            | Q(benefit__individual__location__parent__parent__parent__id=district_id)
        )
        matching_payroll_ids.update(
            PayrollBenefitConsumption.objects.filter(
                payroll_id__in=legacy_payroll_ids,
                is_deleted=False,
                benefit__is_deleted=False,
            ).filter(
                beneficiary_district,
            ).values_list(
                "payroll_id",
                flat=True,
            ).distinct()
        )

    return [
        payroll for payroll in payrolls
        if payroll.id in matching_payroll_ids
    ]


def ensure_payroll_is_not_consolidated_source(payroll, action):
    if payroll.payroll_type != Payroll.PayrollType.STANDARD:
        return
    active_memberships = ConsolidatedPayrollMember.objects.filter(
        source_payroll=payroll,
        is_deleted=False,
        consolidated_payroll__is_deleted=False,
    ).exclude(consolidated_payroll__status=PayrollStatus.REJECTED)
    if active_memberships.exists():
        from django.core.exceptions import ValidationError

        raise ValidationError(
            f"Cannot {action} a payroll while it belongs to an active "
            "consolidated payroll."
        )


def ensure_benefit_is_not_in_active_consolidation(benefit, action):
    source_payrolls = Payroll.objects.filter(
        payrollbenefitconsumption__benefit=benefit,
        payrollbenefitconsumption__is_deleted=False,
        is_deleted=False,
    ).distinct()
    for payroll in source_payrolls:
        ensure_payroll_is_not_consolidated_source(payroll, action)


@transaction.atomic
def reconcile_consolidated_sources(payroll, user, only_completed_sources=False):
    """Mark consolidated source payrolls reconciled after their parent is reconciled."""
    if payroll.payroll_type != Payroll.PayrollType.CONSOLIDATED:
        return

    for member in ConsolidatedPayrollMember.objects.filter(
        consolidated_payroll=payroll,
        is_deleted=False,
        source_payroll__is_deleted=False,
    ).select_related("source_payroll"):
        source_payroll = member.source_payroll
        if only_completed_sources and PayrollBenefitConsumption.objects.filter(
            payroll=source_payroll,
            is_deleted=False,
        ).exclude(benefit__status=BenefitConsumptionStatus.RECONCILED).exists():
            continue
        source_payroll.status = PayrollStatus.RECONCILED
        source_payroll.save(username=user.login_name)
