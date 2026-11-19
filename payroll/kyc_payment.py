import re
from datetime import date
from decimal import Decimal, InvalidOperation

from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils import timezone

from individual.data_source import KYC_DETAILS_WORKFLOW_NAME
from individual.models import (
    GroupIndividual,
    IndividualDataSource,
    IndividualDataSourceUpload,
    IndividualDataUploadRecords,
)
from payroll.apps import PayrollConfig
from payroll.models import (
    BenefitAttachment,
    BenefitConsumptionStatus,
    PayrollBenefitConsumption,
)


_FORBIDDEN_PROVIDERS = {"n/a", "na", "unknown"}
_INVALID_ACCOUNT_NUMBER = re.compile(
    r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)[eE][+-]?\d+$|^[+-]?\d+\.\d+$"
)


def _decimal(value, setting_name):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{setting_name} must be a valid decimal amount") from None
    if not result.is_finite():
        raise ValueError(f"{setting_name} must be a finite amount")
    return result


def _config_date(value, setting_name, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{setting_name} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{setting_name} must be an ISO date") from None


def validate_kyc_payment_config(providers, fee_bands):
    if not isinstance(providers, list) or not isinstance(fee_bands, list):
        raise ValueError("KYC providers and fee bands must be lists")

    providers_by_name = {}
    for index, provider in enumerate(providers):
        if not isinstance(provider, dict):
            raise ValueError(f"KYC provider {index} must be an object")
        name = provider.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"KYC provider {index} must have a non-empty name")
        normalized_name = name.strip().casefold()
        if normalized_name in _FORBIDDEN_PROVIDERS:
            raise ValueError(f"KYC provider {name!r} cannot be used for payments")
        if normalized_name in providers_by_name:
            raise ValueError(f"Duplicate KYC provider name: {name}")
        if "active" in provider and not isinstance(provider["active"], bool):
            raise ValueError(f"KYC provider {name!r} active must be a boolean")
        providers_by_name[normalized_name] = provider

    parsed_bands = []
    for index, band in enumerate(fee_bands):
        if not isinstance(band, dict):
            raise ValueError(f"KYC fee band {index} must be an object")
        provider_name = band.get("provider")
        if not isinstance(provider_name, str) or provider_name.strip().casefold() not in providers_by_name:
            raise ValueError(f"KYC fee band {index} references an unknown provider")
        minimum = _decimal(band.get("min_amount"), f"KYC fee band {index} min_amount")
        fee = _decimal(band.get("fee_mwk"), f"KYC fee band {index} fee_mwk")
        maximum = (
            _decimal(band["max_amount"], f"KYC fee band {index} max_amount")
            if band.get("max_amount") is not None
            else None
        )
        if minimum < 0 or fee < 0 or (maximum is not None and maximum < minimum):
            raise ValueError(f"KYC fee band {index} has a negative or inverted range")
        start = _config_date(band.get("effective_from"), f"KYC fee band {index} effective_from")
        end = _config_date(
            band.get("effective_to"),
            f"KYC fee band {index} effective_to",
            nullable=True,
        )
        if end is not None and end < start:
            raise ValueError(f"KYC fee band {index} has an inverted effective date range")
        parsed_bands.append({
            "provider": provider_name.strip().casefold(),
            "min_amount": minimum,
            "max_amount": maximum,
            "effective_from": start,
            "effective_to": end,
            "index": index,
        })

    for position, left in enumerate(parsed_bands):
        for right in parsed_bands[position + 1:]:
            same_provider = left["provider"] == right["provider"]
            date_ranges_overlap = (
                (left["effective_to"] is None or right["effective_from"] <= left["effective_to"])
                and (right["effective_to"] is None or left["effective_from"] <= right["effective_to"])
            )
            amount_ranges_overlap = (
                left["max_amount"] is None or right["min_amount"] <= left["max_amount"]
            ) and (
                right["max_amount"] is None or left["min_amount"] <= right["max_amount"]
            )
            if same_provider and date_ranges_overlap and amount_ranges_overlap:
                raise ValueError(
                    f"KYC fee bands {left['index']} and {right['index']} overlap"
                )


def _provider_config(provider_name):
    if not isinstance(provider_name, str):
        return None
    normalized_name = provider_name.strip().casefold()
    if not normalized_name or normalized_name in _FORBIDDEN_PROVIDERS:
        return None
    for provider in PayrollConfig.kyc_payment_providers or []:
        if provider.get("name", "").strip().casefold() == normalized_name and provider.get("active", True):
            return provider
    return None


def _matching_fee_band(provider_name, amount, on_date):
    provider = _provider_config(provider_name)
    if not provider:
        return None
    normalized_name = provider["name"].strip().casefold()
    amount = _decimal(amount, "benefit amount")
    for band in PayrollConfig.kyc_cash_out_fee_bands or []:
        if band.get("provider", "").strip().casefold() != normalized_name:
            continue
        minimum = _decimal(band.get("min_amount"), "KYC fee band min_amount")
        maximum = (
            _decimal(band["max_amount"], "KYC fee band max_amount")
            if band.get("max_amount") is not None
            else None
        )
        start = _config_date(band.get("effective_from"), "KYC fee band effective_from")
        end = _config_date(
            band.get("effective_to"),
            "KYC fee band effective_to",
            nullable=True,
        )
        if amount < minimum or (maximum is not None and amount > maximum):
            continue
        if on_date < start or (end is not None and on_date > end):
            continue
        return band
    return None


def _json_object(value):
    return value if isinstance(value, dict) else {}


def _get_kyc_details(individual):
    data = _json_object(getattr(individual, "json_ext", None))
    account_number = data.get("account_number")
    account_name = data.get("account_name")
    provider = data.get("bank")
    gender = data.get("gender") or data.get("sex")
    missing = [
        key for key, value in (
            ("account_number", account_number),
            ("account_name", account_name),
            ("bank", provider),
        )
        if not isinstance(value, str) or not value.strip()
    ]
    invalid = []
    if (
        isinstance(account_number, str)
        and _INVALID_ACCOUNT_NUMBER.fullmatch(account_number.strip())
    ):
        invalid.append("invalid_account_number")
    return {
        "account_number": account_number.strip() if isinstance(account_number, str) else None,
        "account_name": account_name.strip() if isinstance(account_name, str) else None,
        "bank": provider.strip() if isinstance(provider, str) else None,
        "gender": gender,
        "missing": missing,
        "invalid": invalid,
    }


class PayrollKycPaymentPreparationService:
    def __init__(self, user=None):
        self.user = user

    @staticmethod
    def _benefit_links(payroll):
        return list(PayrollBenefitConsumption.objects.filter(
            payroll=payroll,
            is_deleted=False,
            benefit__is_deleted=False,
            benefit__status__in=[
                BenefitConsumptionStatus.ACCEPTED,
                BenefitConsumptionStatus.APPROVE_FOR_PAYMENT,
            ],
        ).select_related("benefit__individual"))

    @staticmethod
    def _group_beneficiaries_by_benefit(benefits):
        if not benefits:
            return {}, set()
        GroupBeneficiary = apps.get_model("social_protection", "GroupBeneficiary")
        group_beneficiary_content_type = ContentType.objects.get_for_model(GroupBeneficiary)
        attachments = BenefitAttachment.objects.filter(
            benefit_id__in=[benefit.id for benefit in benefits],
            is_deleted=False,
        ).select_related("bill")
        group_beneficiary_id_by_benefit = {
            str(attachment.benefit_id): str(attachment.bill.subject_id)
            for attachment in attachments
            if attachment.bill
            and attachment.bill.subject_type_id == group_beneficiary_content_type.id
        }
        group_beneficiaries = {
            str(item.id): item
            for item in GroupBeneficiary.objects.filter(
                id__in=group_beneficiary_id_by_benefit.values(),
                is_deleted=False,
            ).select_related("group")
        }
        return {
            benefit_id: group_beneficiaries.get(group_beneficiary_id)
            for benefit_id, group_beneficiary_id in group_beneficiary_id_by_benefit.items()
        }, set(group_beneficiary_id_by_benefit)

    @staticmethod
    def _primary_members_by_group(group_ids):
        memberships = GroupIndividual.objects.filter(
            group_id__in=group_ids,
            recipient_type=GroupIndividual.RecipientType.PRIMARY,
            is_deleted=False,
            individual__is_deleted=False,
            group__is_deleted=False,
        ).select_related("individual", "group")
        result = {}
        for membership in memberships:
            result.setdefault(str(membership.group_id), []).append(membership)
        return result

    @staticmethod
    def _kyc_upload_individual_ids(individual_ids, statuses):
        if not individual_ids:
            return set()
        upload_ids = IndividualDataUploadRecords.objects.filter(
            workflow=KYC_DETAILS_WORKFLOW_NAME,
            is_deleted=False,
            data_upload__is_deleted=False,
            data_upload__status__in=statuses,
        ).values_list("data_upload_id", flat=True)
        id_strings = [str(identifier) for identifier in individual_ids]
        sources = IndividualDataSource.objects.filter(
            upload_id__in=upload_ids,
            is_deleted=False,
        ).filter(
            Q(individual_id__in=individual_ids)
            | Q(json_ext__ID__in=id_strings)
        ).values_list("individual_id", "json_ext")
        matched_ids = set()
        for individual_id, json_ext in sources:
            if individual_id:
                matched_ids.add(str(individual_id))
            source_json_ext = _json_object(json_ext)
            if source_json_ext.get("ID"):
                matched_ids.add(str(source_json_ext["ID"]))
        return matched_ids

    @staticmethod
    def _stored_snapshot(benefit):
        snapshot = _json_object(benefit.json_ext).get("kyc_payment_preparation")
        required_fields = {
            "version",
            "recipient_id",
            "account_number",
            "account_name",
            "bank",
            "fee_mwk",
            "fee_band",
            "gross_benefit_amount",
            "recipient_amount",
            "programme_total_cost",
            "prepared_at",
        }
        if (
            isinstance(snapshot, dict)
            and required_fields.issubset(snapshot)
            and snapshot.get("version") == 1
        ):
            try:
                fee = Decimal(str(snapshot["fee_mwk"]))
                gross = Decimal(str(snapshot["gross_benefit_amount"]))
                recipient_amount = Decimal(str(snapshot["recipient_amount"]))
                programme_total = Decimal(str(snapshot["programme_total_cost"]))
            except (InvalidOperation, TypeError, ValueError):
                return None
            if (
                not all(value.is_finite() for value in (fee, gross, recipient_amount, programme_total))
                or fee < 0
                or gross < 0
                or recipient_amount != gross
                or programme_total != gross + fee
            ):
                return None
            return snapshot
        return None

    def _evaluate_payroll(self, payroll, on_date=None):
        if on_date is None:
            now = timezone.now()
            on_date = (
                timezone.localtime(now).date()
                if timezone.is_aware(now)
                else now.date()
            )
        links = self._benefit_links(payroll)
        benefits = [link.benefit for link in links]
        group_beneficiaries, group_benefit_ids = self._group_beneficiaries_by_benefit(benefits)
        groups = [
            item.group for item in group_beneficiaries.values()
            if item is not None
        ]
        memberships_by_group = self._primary_members_by_group({
            str(group.id) for group in groups
        })
        individual_ids = {
            benefit.individual_id for benefit in benefits if benefit.individual_id
        }
        pending_ids = self._kyc_upload_individual_ids(
            individual_ids,
            [IndividualDataSourceUpload.Status.WAITING_FOR_VERIFICATION],
        )
        rejected_ids = self._kyc_upload_individual_ids(
            individual_ids,
            [IndividualDataSourceUpload.Status.FAIL],
        )

        rows = []
        for benefit in benefits:
            stored_snapshot = self._stored_snapshot(benefit)
            if stored_snapshot:
                rows.append({
                    "benefit": benefit,
                    "ready": True,
                    "issues": [],
                    "state": "usable",
                    "pending": False,
                    "rejected": False,
                    "snapshot": stored_snapshot,
                    "details": None,
                    "fee_band": None,
                })
                continue

            issues = []
            benefit_id = str(benefit.id)
            group_beneficiary = group_beneficiaries.get(benefit_id)
            individual = benefit.individual
            details = _get_kyc_details(individual) if individual else {
                "account_number": None,
                "account_name": None,
                "bank": None,
                "gender": None,
                "missing": ["recipient"],
                "invalid": [],
            }

            if benefit_id in group_benefit_ids and not group_beneficiary:
                issues.append("group_beneficiary_not_found")
            elif group_beneficiary:
                group = group_beneficiary.group
                group_json_ext = _json_object(group.json_ext)
                if group_json_ext.get("validation_status") != "VERIFIED":
                    issues.append("household_not_verified")
                primary_members = memberships_by_group.get(str(group.id), [])
                if len(primary_members) != 1:
                    issues.append(
                        "missing_primary_recipient" if not primary_members
                        else "ambiguous_primary_recipient"
                    )
                elif str(primary_members[0].individual_id) != str(benefit.individual_id):
                    issues.append("recipient_is_no_longer_primary")

            issues.extend(details["missing"])
            issues.extend(details["invalid"])
            fee_band = None
            if not details["missing"]:
                if not _provider_config(details["bank"]):
                    issues.append("provider_not_configured_or_inactive")
                elif benefit.amount is None:
                    issues.append("missing_benefit_amount")
                else:
                    fee_band = _matching_fee_band(details["bank"], benefit.amount, on_date)
                    if not fee_band:
                        issues.append("no_matching_fee_band")

            individual_id = str(benefit.individual_id) if benefit.individual_id else ""
            has_pending = individual_id in pending_ids
            has_rejected = individual_id in rejected_ids
            missing_only = bool(details["missing"]) and not any(
                issue not in details["missing"] for issue in issues
            )
            if not issues:
                state = "usable"
            elif has_pending:
                state = "pending"
            elif missing_only:
                state = "missing"
            else:
                state = "unusable"

            rows.append({
                "benefit": benefit,
                "ready": not issues,
                "issues": sorted(set(issues)),
                "state": state,
                "pending": has_pending,
                "rejected": has_rejected,
                "snapshot": None,
                "details": details,
                "fee_band": fee_band,
            })

        return rows

    def readiness(self, payroll, on_date=None):
        rows = self._evaluate_payroll(payroll, on_date)
        blocked = [
            {
                "benefit_code": row["benefit"].code,
                "issues": row["issues"],
            }
            for row in rows
            if not row["ready"]
        ]
        cash_out_fee_total = Decimal("0")
        programme_total_cost = Decimal("0")
        for row in rows:
            if not row["ready"]:
                continue
            snapshot = row.get("snapshot")
            fee_band = row.get("fee_band")
            if snapshot:
                cash_out_fee_total += _decimal(
                    snapshot.get("fee_mwk"),
                    "prepared cash-out fee",
                )
                programme_total_cost += _decimal(
                    snapshot.get("programme_total_cost"),
                    "prepared programme total cost",
                )
            elif fee_band:
                fee = _decimal(fee_band.get("fee_mwk"), "KYC fee band fee_mwk")
                cash_out_fee_total += fee
                programme_total_cost += (
                    _decimal(row["benefit"].amount, "benefit amount") + fee
                )
        return {
            "payroll_id": str(payroll.id),
            "eligible_benefits": len(rows),
            "benefits_with_usable_approved_payment_details": sum(
                row["state"] == "usable" for row in rows
            ),
            "missing_payment_details": sum(
                row["state"] == "missing" for row in rows
            ),
            "payment_detail_update_pending_approval": sum(
                row["pending"] for row in rows
            ),
            "rejected_or_unusable_payment_details": sum(
                row["state"] == "unusable" or (
                    row["rejected"] and row["state"] != "usable"
                )
                for row in rows
            ),
            "ready_benefits_cash_out_fee_total": str(cash_out_fee_total),
            "ready_benefits_programme_total_cost": str(programme_total_cost),
            "ready_for_payment": bool(rows) and not blocked,
            "blocked_benefits": blocked,
        }

    def prepare(self, payroll, on_date=None):
        rows = self._evaluate_payroll(payroll, on_date)
        if not rows:
            raise ValidationError("Payroll has no eligible benefits to prepare for payment.")
        blocked = [row for row in rows if not row["ready"]]
        if blocked:
            details = "; ".join(
                f"{row['benefit'].code}: {', '.join(row['issues'])}"
                for row in blocked[:20]
            )
            remaining = len(blocked) - min(len(blocked), 20)
            if remaining:
                details += f"; and {remaining} additional benefit(s)"
            raise ValidationError(
                "Payroll KYC payment readiness failed. Resolve these benefits before payment: "
                + details
            )

        for row in rows:
            if row["snapshot"]:
                continue
            benefit = row["benefit"]
            details = row["details"]
            fee_band = row["fee_band"]
            amount = _decimal(benefit.amount, "benefit amount")
            fee = _decimal(fee_band.get("fee_mwk"), "KYC fee band fee_mwk")
            prepared_at = timezone.now().isoformat()
            snapshot = {
                "version": 1,
                "recipient_id": str(benefit.individual_id),
                "gender": details["gender"],
                "account_number": details["account_number"],
                "account_name": details["account_name"],
                "bank": details["bank"],
                "fee_band": dict(fee_band),
                "fee_mwk": str(fee),
                "gross_benefit_amount": str(amount),
                "recipient_amount": str(amount),
                "programme_total_cost": str(amount + fee),
                "prepared_at": prepared_at,
            }
            benefit_json_ext = dict(_json_object(benefit.json_ext))
            benefit_json_ext["kyc_payment_preparation"] = snapshot
            benefit_json_ext["kyc_payment_details"] = {
                field: details[field]
                for field in ("gender", "account_number", "account_name", "bank")
            }
            benefit.json_ext = benefit_json_ext
            benefit.save(user=self.user)
        return self.readiness(payroll, on_date)


def get_payroll_kyc_payment_readiness(payroll):
    return PayrollKycPaymentPreparationService().readiness(payroll)


def get_prepared_cash_out_fee(benefit):
    snapshot = PayrollKycPaymentPreparationService._stored_snapshot(benefit)
    if snapshot is None:
        raise ValidationError(
            f"Benefit {benefit.code} has no prepared KYC payment-fee snapshot."
        )
    try:
        fee = Decimal(str(snapshot["fee_mwk"]))
    except (InvalidOperation, TypeError, ValueError):
        raise ValidationError(
            f"Benefit {benefit.code} has an invalid prepared cash-out fee."
        ) from None
    if not fee.is_finite() or fee < 0:
        raise ValidationError(
            f"Benefit {benefit.code} has an invalid prepared cash-out fee."
        )
    return fee
