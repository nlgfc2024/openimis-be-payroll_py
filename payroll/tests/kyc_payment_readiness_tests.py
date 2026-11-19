from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from payroll.kyc_payment import (
    PayrollKycPaymentPreparationService,
    _get_kyc_details,
    _matching_fee_band,
    get_prepared_cash_out_fee,
    validate_kyc_payment_config,
)


class KycPaymentConfigurationTests(SimpleTestCase):
    def test_empty_configuration_is_valid_but_has_no_usable_fee_rules(self):
        validate_kyc_payment_config([], [])
        with (
            patch("payroll.kyc_payment.PayrollConfig.kyc_payment_providers", []),
            patch("payroll.kyc_payment.PayrollConfig.kyc_cash_out_fee_bands", []),
        ):
            self.assertIsNone(_matching_fee_band("Example Provider", "100", None))

    def test_rejects_forbidden_provider_names(self):
        with self.assertRaisesRegex(ValueError, "cannot be used"):
            validate_kyc_payment_config([{"name": "Unknown"}], [])

    def test_rejects_overlapping_amount_and_effective_date_ranges(self):
        providers = [{"name": "Example Provider", "active": True}]
        fee_bands = [
            {
                "provider": "Example Provider",
                "min_amount": "100",
                "max_amount": "999",
                "fee_mwk": "10",
                "effective_from": "2026-01-01",
                "effective_to": None,
            },
            {
                "provider": "example provider",
                "min_amount": "999",
                "max_amount": None,
                "fee_mwk": "20",
                "effective_from": "2026-03-01",
                "effective_to": None,
            },
        ]
        with self.assertRaisesRegex(ValueError, "overlap"):
            validate_kyc_payment_config(providers, fee_bands)

    def test_rejects_negative_fees_and_inverted_ranges(self):
        provider = [{"name": "Example Provider"}]
        for minimum, maximum, fee in [
            ("100", "99", "1"),
            ("100", "200", "-1"),
        ]:
            with self.subTest(minimum=minimum, maximum=maximum, fee=fee):
                with self.assertRaisesRegex(ValueError, "negative or inverted"):
                    validate_kyc_payment_config(provider, [{
                        "provider": "Example Provider",
                        "min_amount": minimum,
                        "max_amount": maximum,
                        "fee_mwk": fee,
                        "effective_from": "2026-01-01",
                    }])

    def test_fee_band_boundaries_are_inclusive_and_provider_must_be_active(self):
        providers = [{"name": "Example Provider", "active": True}]
        fee_bands = [{
            "provider": "Example Provider",
            "min_amount": "100",
            "max_amount": "999",
            "fee_mwk": "25",
            "effective_from": "2026-01-01",
            "effective_to": "2026-12-31",
        }]
        with (
            patch("payroll.kyc_payment.PayrollConfig.kyc_payment_providers", providers),
            patch("payroll.kyc_payment.PayrollConfig.kyc_cash_out_fee_bands", fee_bands),
        ):
            self.assertEqual(
                _matching_fee_band("example provider", "100", date(2026, 12, 31)),
                fee_bands[0],
            )
            self.assertIsNone(
                _matching_fee_band("Example Provider", "1000", date(2026, 6, 1))
            )
            inactive_provider = [{"name": "Example Provider", "active": False}]
            with patch("payroll.kyc_payment.PayrollConfig.kyc_payment_providers", inactive_provider):
                self.assertIsNone(
                    _matching_fee_band("Example Provider", "100", date(2026, 6, 1))
                )


class KycPaymentPreparationTests(SimpleTestCase):
    def test_account_numbers_in_scientific_or_decimal_notation_are_unusable(self):
        for account_number in ("1.23E+8", "1234.0"):
            with self.subTest(account_number=account_number):
                details = _get_kyc_details(SimpleNamespace(json_ext={
                    "account_number": account_number,
                    "account_name": "A Recipient",
                    "bank": "Example Provider",
                }))
                self.assertEqual(details["invalid"], ["invalid_account_number"])

    def test_readiness_summary_counts_ready_missing_pending_and_unusable(self):
        def row(code, state, *, ready=False, pending=False, rejected=False, issues=None):
            return {
                "benefit": SimpleNamespace(code=code),
                "ready": ready,
                "issues": issues or [],
                "state": state,
                "pending": pending,
                "rejected": rejected,
                "snapshot": None,
                "details": None,
                "fee_band": None,
            }

        service = PayrollKycPaymentPreparationService()
        payroll = SimpleNamespace(id="payroll-1")
        with patch.object(service, "_evaluate_payroll", return_value=[
            row("READY", "usable", ready=True),
            row("MISSING", "missing", issues=["account_number"]),
            row("PENDING", "pending", pending=True, issues=["account_number"]),
            row("UNUSABLE", "unusable", rejected=True, issues=["no_matching_fee_band"]),
        ]):
            result = service.readiness(payroll)

        self.assertEqual(result["eligible_benefits"], 4)
        self.assertEqual(result["benefits_with_usable_approved_payment_details"], 1)
        self.assertEqual(result["missing_payment_details"], 1)
        self.assertEqual(result["payment_detail_update_pending_approval"], 1)
        self.assertEqual(result["rejected_or_unusable_payment_details"], 1)
        self.assertFalse(result["ready_for_payment"])
        self.assertEqual([item["benefit_code"] for item in result["blocked_benefits"]], [
            "MISSING", "PENDING", "UNUSABLE",
        ])

    def test_payment_preparation_blocks_before_writing_any_snapshot(self):
        benefit = SimpleNamespace(code="BEN-1", save=MagicMock())
        service = PayrollKycPaymentPreparationService(user=SimpleNamespace(login_name="tester"))
        with patch.object(service, "_evaluate_payroll", return_value=[{
            "benefit": benefit,
            "ready": False,
            "issues": ["no_matching_fee_band"],
            "state": "unusable",
            "pending": False,
            "rejected": False,
            "snapshot": None,
            "details": None,
            "fee_band": None,
        }]):
            with self.assertRaisesRegex(ValidationError, "no_matching_fee_band"):
                service.prepare(SimpleNamespace(id="payroll-1"))
        benefit.save.assert_not_called()

    def test_readiness_returns_fee_totals_for_ready_benefits_only(self):
        ready_benefit = SimpleNamespace(code="BEN-READY", amount=Decimal("1000.00"))
        blocked_benefit = SimpleNamespace(code="BEN-BLOCKED", amount=Decimal("500.00"))
        rows = [
            {
                "benefit": ready_benefit,
                "ready": True,
                "issues": [],
                "state": "usable",
                "pending": False,
                "rejected": False,
                "fee_band": {"fee_mwk": "25.00"},
            },
            {
                "benefit": blocked_benefit,
                "ready": False,
                "issues": ["payment_detail_update_pending"],
                "state": "pending",
                "pending": True,
                "rejected": False,
                "fee_band": {"fee_mwk": "15.00"},
            },
        ]
        service = PayrollKycPaymentPreparationService()
        with patch.object(service, "_evaluate_payroll", return_value=rows):
            readiness = service.readiness(SimpleNamespace(id="payroll-1"))

        self.assertEqual(readiness["ready_benefits_cash_out_fee_total"], "25.00")
        self.assertEqual(readiness["ready_benefits_programme_total_cost"], "1025.00")

    def test_preparation_snapshots_fee_and_programme_total(self):
        benefit = SimpleNamespace(
            id="benefit-1",
            code="BEN-1",
            individual_id="individual-1",
            amount=Decimal("1000.00"),
            json_ext={"extra_info": {"keep": True}},
            save=MagicMock(),
        )
        fee_band = {
            "provider": "Example Provider",
            "min_amount": "100",
            "max_amount": None,
            "fee_mwk": "25.00",
            "effective_from": "2026-01-01",
            "effective_to": None,
        }
        row = {
            "benefit": benefit,
            "ready": True,
            "issues": [],
            "state": "usable",
            "pending": False,
            "rejected": False,
            "snapshot": None,
            "details": {
                "gender": "Female",
                "account_number": "001234",
                "account_name": "A Recipient",
                "bank": "Example Provider",
            },
            "fee_band": fee_band,
        }
        service = PayrollKycPaymentPreparationService(user=SimpleNamespace(login_name="tester"))
        with patch.object(service, "_evaluate_payroll", return_value=[row]):
            service.prepare(SimpleNamespace(id="payroll-1"))

        snapshot = benefit.json_ext["kyc_payment_preparation"]
        self.assertEqual(snapshot["fee_mwk"], "25.00")
        self.assertEqual(snapshot["gross_benefit_amount"], "1000.00")
        self.assertEqual(snapshot["recipient_amount"], "1000.00")
        self.assertEqual(snapshot["programme_total_cost"], "1025.00")
        self.assertEqual(benefit.json_ext["extra_info"], {"keep": True})
        self.assertEqual(
            benefit.json_ext["kyc_payment_details"]["account_number"],
            "001234",
        )
        self.assertEqual(get_prepared_cash_out_fee(benefit), Decimal("25.00"))

    def test_missing_or_invalid_fee_snapshot_is_an_explicit_error(self):
        benefit = SimpleNamespace(code="BEN-1", json_ext={})
        with self.assertRaisesRegex(ValidationError, "no prepared"):
            get_prepared_cash_out_fee(benefit)
