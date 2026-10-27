from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('payroll', '0025_district_payroll_batches'),
    ]

    operations = [
        migrations.RunSQL(
            sql='ALTER TABLE payroll_districtpayrollbatch RENAME COLUMN "UUID" TO id;',
            reverse_sql='ALTER TABLE payroll_districtpayrollbatch RENAME COLUMN id TO "UUID";',
        ),
        migrations.RunSQL(
            sql='ALTER TABLE payroll_districtpayrollbatchpayroll RENAME COLUMN "UUID" TO id;',
            reverse_sql='ALTER TABLE payroll_districtpayrollbatchpayroll RENAME COLUMN id TO "UUID";',
        ),
    ]
