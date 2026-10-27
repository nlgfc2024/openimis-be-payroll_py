# Generated manually to keep district payment reconciliation separate from project payrolls.

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('location', '0015_set_managed_to_true'),
        ('payroll', '0024_reconciliation_upload_audit'),
    ]

    operations = [
        migrations.CreateModel(
            name='DistrictPayrollBatch',
            fields=[
                ('id', models.UUIDField(db_column='UUID', default=None, editable=False, primary_key=True, serialize=False)),
                ('status', models.CharField(choices=[('DRAFT', 'Draft'), ('DOWNLOADED', 'Downloaded'), ('PENDING_REVIEW', 'Pending review'), ('APPLIED', 'Applied'), ('FAILED', 'Failed')], default='DRAFT', max_length=32)),
                ('total_records', models.PositiveIntegerField(default=0)),
                ('matched_records', models.PositiveIntegerField(default=0)),
                ('paid_records', models.PositiveIntegerField(default=0)),
                ('unpaid_records', models.PositiveIntegerField(default=0)),
                ('unmatched_records', models.PositiveIntegerField(default=0)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('district', models.ForeignKey(on_delete=django.db.models.deletion.DO_NOTHING, to='location.location')),
                ('benefit_plan_id', models.CharField(max_length=255)),
            ],
        ),
        migrations.CreateModel(
            name='DistrictPayrollBatchPayroll',
            fields=[
                ('id', models.UUIDField(db_column='UUID', default=None, editable=False, primary_key=True, serialize=False)),
                ('batch', models.ForeignKey(on_delete=django.db.models.deletion.DO_NOTHING, related_name='payroll_links', to='payroll.districtpayrollbatch')),
                ('payroll', models.ForeignKey(on_delete=django.db.models.deletion.DO_NOTHING, to='payroll.payroll')),
            ],
        ),
        migrations.AddConstraint(
            model_name='districtpayrollbatch',
            constraint=models.UniqueConstraint(fields=('district', 'benefit_plan_id'), name='payroll_district_batch_scope_unique'),
        ),
        migrations.AddConstraint(
            model_name='districtpayrollbatchpayroll',
            constraint=models.UniqueConstraint(fields=('batch', 'payroll'), name='payroll_district_batch_payroll_unique'),
        ),
        migrations.AddField(
            model_name='csvreconciliationupload',
            name='district_batch',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.DO_NOTHING, to='payroll.districtpayrollbatch'),
        ),
        migrations.AddField(
            model_name='historicalcsvreconciliationupload',
            name='district_batch',
            field=models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to='payroll.districtpayrollbatch'),
        ),
    ]
