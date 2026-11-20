from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase

from payroll.gql_mutations import (
    CreatePaymentProviderMutation,
    SavePaymentProviderWithFeeBandMutation,
)
from payroll.models import BenefitConsumption, PaymentProvider, WithdrawalFeeBand
from payroll.services import (
    CsvReconciliationService,
    PayrollService,
    WithdrawalFeeConfigService,
)


class WithdrawalFeeProviderInputTests(SimpleTestCase):
    def test_create_provider_input_accepts_active_status(self):
        self.assertIn(
            "is_active",
            CreatePaymentProviderMutation.Input._meta.fields,
        )

    def test_combined_provider_and_fee_band_input_accepts_both_records(self):
        fields = SavePaymentProviderWithFeeBandMutation.Input._meta.fields
        self.assertTrue({
            "provider_id",
            "code",
            "name",
            "provider_type",
            "is_active",
            "fee_band_id",
            "minimum_amount",
            "maximum_amount",
            "transfer_cost",
            "effective_from",
            "effective_to",
        }.issubset(fields))

    def test_preview_calculates_fee_from_provider_and_amount_band(self):
        provider = SimpleNamespace(id="provider-1", name="Mpamba", code="MP")
        band = SimpleNamespace(
            minimum_amount=Decimal("5000.00"),
            maximum_amount=Decimal("10000.00"),
            transfer_cost=Decimal("250.00"),
        )
        benefit = SimpleNamespace(
            amount=Decimal("7200.00"),
            withdrawal_fee=None,
            withdrawal_fee_prepared_at=None,
            json_ext={"kyc_payment_details": {"payment_provider_code": "mp"}},
        )
        catalog = {
            "providers_by_code": {"mp": [provider]},
            "providers_by_name": {},
            "bands_by_provider": {provider.id: [band]},
        }

        self.assertEqual(
            WithdrawalFeeConfigService.preview_fee_for_benefit(benefit, catalog),
            (Decimal("250.00"), None),
        )

    def test_preview_reports_when_no_fee_band_matches(self):
        provider = SimpleNamespace(id="provider-1", name="Mpamba", code="MP")
        benefit = SimpleNamespace(
            amount=Decimal("7200.00"),
            withdrawal_fee=None,
            withdrawal_fee_prepared_at=None,
            json_ext={"kyc_payment_details": {"payment_provider_code": "MP"}},
        )
        catalog = {
            "providers_by_code": {"mp": [provider]},
            "providers_by_name": {},
            "bands_by_provider": {provider.id: []},
        }

        fee, error = WithdrawalFeeConfigService.preview_fee_for_benefit(benefit, catalog)

        self.assertIsNone(fee)
        self.assertIn("found 0", error)


class WithdrawalFeeBandValidationTests(TestCase):
    def setUp(self):
        self.provider = PaymentProvider(code="BANK_A", name="Bank A")

    def make_band(self, **overrides):
        values = {
            "provider": self.provider,
            "minimum_amount": Decimal("0.00"),
            "maximum_amount": Decimal("100.00"),
            "transfer_cost": Decimal("5.00"),
            "effective_from": date(2026, 1, 1),
            "effective_to": None,
        }
        values.update(overrides)
        return WithdrawalFeeBand(**values)

    def test_rejects_reversed_amount_range(self):
        with self.assertRaises(ValidationError):
            WithdrawalFeeConfigService._validate_fee_band(
                self.make_band(
                    minimum_amount=Decimal("101.00"),
                    maximum_amount=Decimal("100.00"),
                )
            )

    @patch("payroll.services.WithdrawalFeeBand.objects")
    def test_rejects_overlapping_inclusive_amount_and_date_ranges(self, fee_band_manager):
        existing = self.make_band(
            minimum_amount=Decimal("0.00"),
            maximum_amount=Decimal("100.00"),
            effective_to=date(2026, 12, 31),
        )
        fee_band_manager.select_for_update.return_value.filter.return_value = [existing]

        with self.assertRaises(ValidationError):
            WithdrawalFeeConfigService._validate_fee_band(
                self.make_band(
                    minimum_amount=Decimal("100.00"),
                    maximum_amount=Decimal("200.00"),
                )
            )

    @patch("payroll.services.WithdrawalFeeBand.objects")
    def test_allows_same_amount_range_after_existing_effective_period(self, fee_band_manager):
        existing = self.make_band(effective_to=date(2026, 12, 31))
        fee_band_manager.select_for_update.return_value.filter.return_value = [existing]

        WithdrawalFeeConfigService._validate_fee_band(
            self.make_band(effective_from=date(2027, 1, 1))
        )

    def test_rejects_placeholder_provider_values(self):
        with self.assertRaises(ValidationError):
            WithdrawalFeeConfigService._validate_provider("Unknown", "UNKNOWN", "BANK")

    def test_used_fee_band_cannot_be_updated(self):
        band = SimpleNamespace(
            prepared_benefits=MagicMock(),
        )
        band.prepared_benefits.filter.return_value.exists.return_value = True
        fee_band_manager = MagicMock()
        fee_band_manager.select_for_update.return_value.filter.return_value.first.return_value = band

        with patch("payroll.services.WithdrawalFeeBand.objects", fee_band_manager):
            with self.assertRaises(ValidationError):
                WithdrawalFeeConfigService(
                    SimpleNamespace(username="tester"),
                ).update_fee_band("band-id", {})

    @patch("payroll.services.PaymentProvider.objects")
    @patch("payroll.services.WithdrawalFeeBand.objects")
    def test_inactive_provider_band_can_be_updated_without_reassignment(
        self, fee_band_manager, provider_manager,
    ):
        provider = SimpleNamespace(id="provider-id", is_active=False)
        band = self.make_band(provider=provider)
        band.id = "band-id"
        band.provider_id = provider.id
        band.prepared_benefits = MagicMock()
        band.prepared_benefits.filter.return_value.exists.return_value = False
        band.save = MagicMock()
        fee_band_manager.select_for_update.return_value.filter.side_effect = [
            SimpleNamespace(first=MagicMock(return_value=band)),
            [],
        ]
        provider_manager.select_for_update.return_value.filter.return_value.first.return_value = provider

        WithdrawalFeeConfigService(
            SimpleNamespace(username="tester"),
        ).update_fee_band("band-id", {})

        band.save.assert_called_once_with(username="tester")


class WithdrawalFeePreparationTests(TestCase):
    def setUp(self):
        self.user = SimpleNamespace(username="test-user")
        self.service = PayrollService(self.user)
        self.payroll = SimpleNamespace(id="00000000-0000-0000-0000-000000000001")
        self.provider = PaymentProvider(code="BANK_A", name="Bank A")
        self.fee_band = WithdrawalFeeBand(
            transfer_cost=Decimal("5.25"),
        )
        self.benefit = SimpleNamespace(
            code="BENEFIT-1",
            amount=Decimal("100.00"),
            json_ext={
                "kyc_payment_details": {
                    "account_number": "000123",
                    "account_name": "Test Beneficiary",
                    "payment_provider_code": "BANK_A",
                },
            },
            withdrawal_fee_prepared_at=None,
            withdrawal_provider=None,
            withdrawal_fee_band=None,
            withdrawal_fee=None,
            save=MagicMock(),
        )

    def mock_fee_queries(self, benefit=None):
        benefits = [benefit or self.benefit]
        benefit_query = MagicMock()
        benefit_query.filter.return_value.select_related.return_value = benefits

        provider_query = MagicMock()
        provider_query.filter.return_value = provider_query
        provider_query.__getitem__.return_value = [self.provider]

        band_query = MagicMock()
        band_query.filter.return_value = band_query
        band_query.__getitem__.return_value = [self.fee_band]
        return benefit_query, provider_query, band_query

    def test_gateway_task_is_queued_after_transaction_commit(self):
        user = SimpleNamespace(id="user-id", username="test-user")
        payroll = SimpleNamespace(id="payroll-id")
        service = PayrollService(user)

        with (
            patch("payroll.services.Payroll.objects.get", return_value=payroll),
            patch("payroll.services.ensure_payroll_is_not_consolidated_source"),
            patch.object(service, "_prepare_withdrawal_fee_snapshots"),
            patch("payroll.services.transaction.on_commit") as on_commit,
            patch("payroll.services.send_requests_to_gateway_payment.delay") as send_payment,
        ):
            service.make_payment_for_payroll({"id": payroll.id})

            on_commit.assert_called_once()
            send_payment.assert_not_called()
            on_commit.call_args.args[0]()
            send_payment.assert_called_once_with(payroll.id, user.id)

    @patch("payroll.services.WithdrawalFeeBand.objects")
    @patch("payroll.services.PaymentProvider.objects")
    @patch("payroll.services.BenefitConsumption.objects")
    def test_snapshots_provider_fee_and_preserves_gross_benefit(
        self, benefit_manager, provider_manager, fee_band_manager,
    ):
        benefit_query, provider_query, band_query = self.mock_fee_queries()
        benefit_manager.select_for_update.return_value = benefit_query
        provider_manager.select_for_update.return_value = provider_query
        fee_band_manager.select_for_update.return_value = band_query

        self.service._prepare_withdrawal_fee_snapshots(self.payroll)

        benefit_manager.select_for_update.assert_called_once_with(of=("self",))
        self.assertEqual(self.benefit.withdrawal_provider, self.provider)
        self.assertEqual(self.benefit.withdrawal_fee_band, self.fee_band)
        self.assertEqual(self.benefit.withdrawal_fee, Decimal("5.25"))
        self.assertIsNotNone(self.benefit.withdrawal_fee_prepared_at)
        self.assertEqual(self.benefit.amount, Decimal("100.00"))
        self.benefit.save.assert_called_once_with(username="test-user")

    @patch("payroll.services.WithdrawalFeeBand.objects")
    @patch("payroll.services.PaymentProvider.objects")
    @patch("payroll.services.BenefitConsumption.objects")
    def test_uses_zero_fee_snapshot_when_kyc_account_details_are_missing(
        self, benefit_manager, provider_manager, fee_band_manager,
    ):
        self.benefit.json_ext["kyc_payment_details"]["account_number"] = ""
        benefit_query, _, _ = self.mock_fee_queries()
        benefit_manager.select_for_update.return_value = benefit_query

        with self.assertLogs("payroll.services", level="WARNING") as logs:
            self.service._prepare_withdrawal_fee_snapshots(self.payroll)

        self.assertIn("using zero fee", logs.output[0])
        self.assertIsNone(self.benefit.withdrawal_provider)
        self.assertIsNone(self.benefit.withdrawal_fee_band)
        self.assertEqual(self.benefit.withdrawal_fee, Decimal("0.00"))
        self.assertIsNotNone(self.benefit.withdrawal_fee_prepared_at)
        self.benefit.save.assert_called_once_with(username="test-user")
        provider_manager.select_for_update.assert_not_called()
        fee_band_manager.select_for_update.assert_not_called()

    @patch("payroll.services.WithdrawalFeeBand.objects")
    @patch("payroll.services.PaymentProvider.objects")
    @patch("payroll.services.BenefitConsumption.objects")
    def test_uses_zero_fee_snapshot_when_no_band_matches(
        self, benefit_manager, provider_manager, fee_band_manager,
    ):
        benefit_query, provider_query, band_query = self.mock_fee_queries()
        band_query.__getitem__.return_value = []
        benefit_manager.select_for_update.return_value = benefit_query
        provider_manager.select_for_update.return_value = provider_query
        fee_band_manager.select_for_update.return_value = band_query

        with self.assertLogs("payroll.services", level="WARNING") as logs:
            self.service._prepare_withdrawal_fee_snapshots(self.payroll)

        self.assertIn("using zero fee", logs.output[0])
        self.assertEqual(self.benefit.withdrawal_fee, Decimal("0.00"))
        self.assertIsNotNone(self.benefit.withdrawal_fee_prepared_at)

    @patch("payroll.services.WithdrawalFeeBand.objects")
    @patch("payroll.services.PaymentProvider.objects")
    @patch("payroll.services.BenefitConsumption.objects")
    def test_existing_snapshot_is_not_repriced(
        self, benefit_manager, provider_manager, fee_band_manager,
    ):
        self.benefit.withdrawal_fee_prepared_at = object()
        self.benefit.withdrawal_provider_id = "provider-id"
        self.benefit.withdrawal_fee_band_id = "fee-band-id"
        self.benefit.withdrawal_fee = Decimal("3.00")
        benefit_query, _, _ = self.mock_fee_queries()
        benefit_manager.select_for_update.return_value = benefit_query

        self.service._prepare_withdrawal_fee_snapshots(self.payroll)

        self.assertEqual(self.benefit.withdrawal_fee, Decimal("3.00"))
        self.benefit.save.assert_not_called()
        provider_manager.select_for_update.assert_not_called()
        fee_band_manager.select_for_update.assert_not_called()

    @patch("payroll.services.WithdrawalFeeBand.objects")
    @patch("payroll.services.PaymentProvider.objects")
    @patch("payroll.services.BenefitConsumption.objects")
    def test_existing_zero_fee_snapshot_is_not_repriced(
        self, benefit_manager, provider_manager, fee_band_manager,
    ):
        self.benefit.withdrawal_fee_prepared_at = object()
        self.benefit.withdrawal_fee = Decimal("0.00")
        self.benefit.withdrawal_provider_id = None
        self.benefit.withdrawal_fee_band_id = None
        benefit_query, _, _ = self.mock_fee_queries()
        benefit_manager.select_for_update.return_value = benefit_query

        self.service._prepare_withdrawal_fee_snapshots(self.payroll)

        self.assertEqual(self.benefit.withdrawal_fee, Decimal("0.00"))
        self.benefit.save.assert_not_called()
        provider_manager.select_for_update.assert_not_called()
        fee_band_manager.select_for_update.assert_not_called()


class PaymentReferenceAccessTests(SimpleTestCase):
    @patch("payroll.services.ReconciliationUploadRow.objects.filter", return_value=[])
    def test_non_admin_review_download_redacts_reference_column(self, _rows):
        user = SimpleNamespace(
            is_authenticated=True,
            has_perms=lambda _perms: False,
        )
        service = CsvReconciliationService(user)
        contents = b"code,receipt,Paid\nBENEFIT-1,provider-ref-1,Yes\n"

        review = service.download_upload_review(SimpleNamespace(), contents)

        self.assertNotIn(b"receipt", review.getvalue().lower())
        self.assertNotIn(b"provider-ref-1", review.getvalue())

    @patch("payroll.services.ReconciliationUploadRow.objects.filter", return_value=[])
    def test_admin_review_download_also_hides_reference_column(self, _rows):
        user = SimpleNamespace(
            is_authenticated=True,
            has_perms=lambda _perms: True,
        )
        service = CsvReconciliationService(user)
        contents = b"code,receipt,Paid\nBENEFIT-1,provider-ref-1,Yes\n"

        with patch("payroll.apps.PayrollConfig.gql_withdrawal_fee_manage_perms", ["202005"]):
            review = service.download_upload_review(SimpleNamespace(), contents)

        self.assertNotIn(b"receipt", review.getvalue().lower())
        self.assertNotIn(b"provider-ref-1", review.getvalue())
