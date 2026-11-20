from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("payroll", "0027_add_withdrawal_fee_right_to_admin"),
    ]

    operations = [
        migrations.AddField(
            model_name="benefitconsumption",
            name="withdrawal_fee_error",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="historicalbenefitconsumption",
            name="withdrawal_fee_error",
            field=models.TextField(blank=True, null=True),
        ),
    ]
