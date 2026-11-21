import importlib
from types import SimpleNamespace
from unittest.mock import MagicMock

from django.test import SimpleTestCase


class WithdrawalFeeMigrationTests(SimpleTestCase):
    def test_fee_right_migration_skips_when_admin_role_is_missing(self):
        migration = importlib.import_module(
            "payroll.migrations.0027_add_withdrawal_fee_right_to_admin",
        )
        role_model = SimpleNamespace(objects=MagicMock())
        role_right_model = SimpleNamespace(objects=MagicMock())
        role_model.objects.filter.return_value.first.return_value = None
        apps = SimpleNamespace(
            get_model=lambda app_label, model_name: {
                ("core", "role"): role_model,
                ("core", "roleright"): role_right_model,
            }[(app_label, model_name)],
        )

        migration.add_withdrawal_fee_right(apps, None)

        role_right_model.objects.create.assert_not_called()
        role_right_model.objects.filter.assert_not_called()

    def test_fee_right_migration_depends_on_core_migrations(self):
        migration = importlib.import_module(
            "payroll.migrations.0027_add_withdrawal_fee_right_to_admin",
        )

        self.assertIn(("core", "0037_asyncjob"), migration.Migration.dependencies)
