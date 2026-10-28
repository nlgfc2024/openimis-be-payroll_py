import uuid

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
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('status', models.CharField(choices=[('DRAFT', 'Draft'), ('PENDING_DISTRICT_APPROVAL', 'Pending district approval'), ('PENDING_NATIONAL_APPROVAL', 'Pending national approval'), ('READY_FOR_PSP', 'Ready for PSP'), ('SUBMITTED_TO_PSP', 'Submitted to PSP'), ('PENDING_REVIEW', 'Pending review'), ('APPLIED', 'Applied'), ('REJECTED', 'Rejected'), ('FAILED', 'Failed')], default='DRAFT', max_length=32)),
                ('total_records', models.PositiveIntegerField(default=0)),
                ('matched_records', models.PositiveIntegerField(default=0)),
                ('paid_records', models.PositiveIntegerField(default=0)),
                ('unpaid_records', models.PositiveIntegerField(default=0)),
                ('unmatched_records', models.PositiveIntegerField(default=0)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('district', models.ForeignKey(on_delete=django.db.models.deletion.DO_NOTHING, to='location.location')),
                ('benefit_plan_id', models.CharField(max_length=255)),
                ('financial_year', models.CharField(max_length=9)),
                ('bank_name', models.CharField(blank=True, max_length=255, null=True)),
                ('account_number', models.CharField(blank=True, max_length=255, null=True)),
                ('cash_out_fee_per_participant', models.DecimalField(decimal_places=2, default=0, max_digits=18)),
                ('total_wages', models.DecimalField(decimal_places=2, default=0, max_digits=18)),
                ('total_cash_out_fees', models.DecimalField(decimal_places=2, default=0, max_digits=18)),
                ('summary', models.JSONField(default=dict)),
            ],
        ),
        migrations.CreateModel(
            name='DistrictPayrollBatchPayroll',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('batch', models.ForeignKey(on_delete=django.db.models.deletion.DO_NOTHING, related_name='payroll_links', to='payroll.districtpayrollbatch')),
                ('payroll', models.ForeignKey(on_delete=django.db.models.deletion.DO_NOTHING, to='payroll.payroll')),
            ],
        ),
        migrations.AddConstraint(
            model_name='districtpayrollbatch',
            constraint=models.UniqueConstraint(fields=('district', 'benefit_plan_id', 'financial_year'), name='payroll_district_batch_scope_unique'),
        ),
        migrations.AddConstraint(
            model_name='districtpayrollbatchpayroll',
            constraint=models.UniqueConstraint(fields=('batch', 'payroll'), name='payroll_district_batch_payroll_unique'),
        ),
        migrations.AddField(
            model_name='csvreconciliationupload',
            name='district_wagesheet',
            field=models.ForeignKey(blank=True, db_column='district_batch_id', null=True, on_delete=django.db.models.deletion.DO_NOTHING, to='payroll.districtpayrollbatch'),
        ),
        migrations.AddField(
            model_name='historicalcsvreconciliationupload',
            name='district_wagesheet',
            field=models.ForeignKey(blank=True, db_column='district_batch_id', db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to='payroll.districtpayrollbatch'),
        ),
    ]
