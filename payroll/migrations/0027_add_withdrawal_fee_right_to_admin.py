from django.db import migrations


WITHDRAWAL_FEE_RIGHT = 202005
IMIS_ADMINISTRATOR_SYSTEM_ROLE = 64


def add_withdrawal_fee_right(apps, schema_editor):
    role_model = apps.get_model("core", "role")
    role_right_model = apps.get_model("core", "roleright")
    role = role_model.objects.get(is_system=IMIS_ADMINISTRATOR_SYSTEM_ROLE)

    if not role_right_model.objects.filter(
        validity_to__isnull=True,
        role=role,
        right_id=WITHDRAWAL_FEE_RIGHT,
    ).exists():
        role_right_model.objects.create(
            role=role,
            right_id=WITHDRAWAL_FEE_RIGHT,
            audit_user_id=1,
        )


def remove_withdrawal_fee_right(apps, schema_editor):
    apps.get_model("core", "roleright").objects.filter(
        role__is_system=IMIS_ADMINISTRATOR_SYSTEM_ROLE,
        right_id=WITHDRAWAL_FEE_RIGHT,
        validity_to__isnull=True,
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("payroll", "0026_paymentprovider_benefitconsumption_withdrawal_fee_and_more"),
    ]

    operations = [
        migrations.RunPython(add_withdrawal_fee_right, remove_withdrawal_fee_right),
    ]
