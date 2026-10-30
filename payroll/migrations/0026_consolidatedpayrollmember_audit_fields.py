# Generated manually to complete the inherited HistoryModel fields omitted by
# the initial consolidated-member migration.
import datetime

import dirtyfields.dirtyfields
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import simple_history.models


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('payroll', '0025_consolidated_payroll'),
    ]

    operations = [
        migrations.AddField(
            model_name='consolidatedpayrollmember',
            name='user_created',
            field=models.ForeignKey(db_column='UserCreatedUUID', on_delete=django.db.models.deletion.DO_NOTHING,
                                    related_name='%(class)s_user_created', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='consolidatedpayrollmember',
            name='user_updated',
            field=models.ForeignKey(db_column='UserUpdatedUUID', on_delete=django.db.models.deletion.DO_NOTHING,
                                    related_name='%(class)s_user_updated', to=settings.AUTH_USER_MODEL),
        ),
        migrations.CreateModel(
            name='HistoricalConsolidatedPayrollMember',
            fields=[
                ('id', models.UUIDField(db_column='UUID', db_index=True, default=None, editable=False)),
                ('is_deleted', models.BooleanField(db_column='isDeleted', default=False)),
                ('json_ext', models.JSONField(blank=True, db_column='Json_ext', null=True)),
                ('date_created', models.DateTimeField(db_column='DateCreated', default=datetime.datetime.now, null=True)),
                ('date_updated', models.DateTimeField(db_column='DateUpdated', default=datetime.datetime.now, null=True)),
                ('version', models.IntegerField(default=1)),
                ('beneficiaries_count', models.PositiveIntegerField(default=0)),
                ('total_amount', models.DecimalField(decimal_places=2, default=0, max_digits=18)),
                ('history_id', models.AutoField(primary_key=True, serialize=False)),
                ('history_date', models.DateTimeField(db_index=True)),
                ('history_change_reason', models.CharField(max_length=100, null=True)),
                ('history_type', models.CharField(choices=[('+', 'Created'), ('~', 'Changed'), ('-', 'Deleted')], max_length=1)),
                ('consolidated_payroll', models.ForeignKey(blank=True, db_constraint=False, null=True,
                 on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to='payroll.payroll')),
                ('source_payroll', models.ForeignKey(blank=True, db_constraint=False, null=True,
                 on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to='payroll.payroll')),
                ('history_user', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL,
                 related_name='+', to=settings.AUTH_USER_MODEL)),
                ('user_created', models.ForeignKey(blank=True, db_column='UserCreatedUUID', db_constraint=False,
                 null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('user_updated', models.ForeignKey(blank=True, db_column='UserUpdatedUUID', db_constraint=False,
                 null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'historical consolidated payroll member',
                'verbose_name_plural': 'historical consolidated payroll members',
                'ordering': ('-history_date', '-history_id'),
                'get_latest_by': ('history_date', 'history_id'),
            },
            bases=(simple_history.models.HistoricalChanges, models.Model),
        ),
    ]
