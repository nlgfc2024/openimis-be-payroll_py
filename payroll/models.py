from django.db import models
from django.utils.translation import gettext as _

from core.models import HistoryModel, HistoryBusinessModel, User, UUIDModel, ObjectMutation, MutationLog
from core.fields import DateField
from invoice.models import Bill
from location.models import Location
from payment_cycle.models import PaymentCycle
from contribution_plan.models import PaymentPlan
from individual.models import Individual


class PayrollStatus(models.TextChoices):
    PENDING_APPROVAL = "PENDING_APPROVAL", _("PENDING_APPROVAL")
    APPROVE_FOR_PAYMENT = "APPROVE_FOR_PAYMENT", _("APPROVE_FOR_PAYMENT")
    REJECTED = "REJECTED", _("REJECTED")
    RECONCILED = "RECONCILED", _("RECONCILED")


class BenefitConsumptionStatus(models.TextChoices):
    ACCEPTED = "ACCEPTED", _("ACCEPTED")
    CREATED = "CREATED", _("CREATED")
    APPROVE_FOR_PAYMENT = "APPROVE_FOR_PAYMENT", _("APPROVE_FOR_PAYMENT")
    REJECTED = "REJECTED", _("REJECTED")
    DUPLICATE = "DUPLICATE", _("DUPLICATE")
    RECONCILED = "RECONCILED", _("RECONCILED")
    PENDING_DELETION = "PENDING_DELETION", _("PENDING_DELETION")


class PaymentPoint(HistoryModel):
    name = models.CharField(max_length=255)
    location = models.ForeignKey(Location, models.DO_NOTHING)
    ppm = models.ForeignKey(User, models.DO_NOTHING, blank=True, null=True)


class Payroll(HistoryBusinessModel):
    name = models.CharField(max_length=255, blank=False, null=False)
    payment_plan = models.ForeignKey(PaymentPlan, on_delete=models.DO_NOTHING, blank=True, null=True)
    payment_cycle = models.ForeignKey(PaymentCycle, on_delete=models.DO_NOTHING, blank=True, null=True)
    payment_point = models.ForeignKey(PaymentPoint, on_delete=models.DO_NOTHING, blank=True, null=True)
    status = models.CharField(
        max_length=100, choices=PayrollStatus.choices, default=PayrollStatus.PENDING_APPROVAL, null=False
    )
    payment_method = models.CharField(max_length=255, blank=True, null=True)

    def __str__(self):
        return f"Payroll {self.name} - {self.uuid}"


class PayrollBill(HistoryModel):
    # 1:n it is ensured by the service
    payroll = models.ForeignKey(Payroll, on_delete=models.DO_NOTHING)
    bill = models.ForeignKey(Bill, on_delete=models.DO_NOTHING)


class PaymentAdaptorHistory(HistoryModel):
    payroll = models.ForeignKey(Payroll, on_delete=models.DO_NOTHING)
    total_amount = models.CharField(max_length=255, blank=True, null=True)
    bills_ids = models.JSONField()


class BenefitConsumption(HistoryBusinessModel):
    individual = models.ForeignKey(Individual, on_delete=models.DO_NOTHING)
    photo = models.TextField(blank=True, null=True)
    code = models.CharField(max_length=255, blank=False, null=False)
    date_due = DateField(db_column='DateDue', null=True)
    receipt = models.CharField(db_column='Receipt', max_length=255, null=True, blank=True)
    amount = models.DecimalField(db_column='Amount', max_digits=18, decimal_places=2, null=True)
    type = models.CharField(db_column='Type', max_length=255, null=True)
    status = models.CharField(
        max_length=100, choices=BenefitConsumptionStatus.choices, default=BenefitConsumptionStatus.ACCEPTED, null=False
    )

    def __str__(self):
        return f"Benefit Consumption {self.code} - {self.receipt} - {self.amount}"


class BenefitAttachment(HistoryBusinessModel):
    benefit = models.ForeignKey(BenefitConsumption, on_delete=models.DO_NOTHING)
    bill = models.ForeignKey(Bill, on_delete=models.DO_NOTHING)


class PayrollBenefitConsumption(HistoryModel):
    # 1:n it is ensured by the service
    payroll = models.ForeignKey(Payroll, on_delete=models.DO_NOTHING)
    benefit = models.ForeignKey(BenefitConsumption, on_delete=models.DO_NOTHING)


class CsvReconciliationUpload(HistoryModel):
    class Status(models.TextChoices):
        TRIGGERED = 'TRIGGERED', _('Triggered')
        VALIDATING = 'VALIDATING', _('Validating')
        IN_PROGRESS = 'IN_PROGRESS', _('In progress')
        PENDING_REVIEW = 'PENDING_REVIEW', _('Pending review')
        SUCCESS = 'SUCCESS', _('Success')
        PARTIAL_SUCCESS = 'PARTIAL_SUCCESS', _('Partial Success')
        WAITING_FOR_VERIFICATION = 'WAITING_FOR_VERIFICATION', _('WAITING_FOR_VERIFICATION')
        FAIL = 'FAIL', _('Fail')
        DUPLICATE = 'DUPLICATE', _('Duplicate')

    payroll = models.ForeignKey(Payroll, models.DO_NOTHING, null=True, blank=True)
    status = models.CharField(max_length=255, choices=Status.choices, default=Status.TRIGGERED)
    error = models.JSONField(blank=True, default=dict)
    file_name = models.CharField(max_length=255, null=True, blank=True)
    checksum = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    storage_key = models.CharField(max_length=512, null=True, blank=True)
    duplicate_of = models.ForeignKey('self', models.DO_NOTHING, null=True, blank=True, related_name='duplicates')
    total_records = models.PositiveIntegerField(default=0)
    matched_records = models.PositiveIntegerField(default=0)
    paid_records = models.PositiveIntegerField(default=0)
    unpaid_records = models.PositiveIntegerField(default=0)
    unmatched_records = models.PositiveIntegerField(default=0)


class ReconciliationUploadRow(HistoryModel):
    class Status(models.TextChoices):
        VALID = 'VALID', _('Valid')
        INVALID = 'INVALID', _('Invalid')

    upload = models.ForeignKey(CsvReconciliationUpload, models.DO_NOTHING, related_name='rows')
    row_number = models.PositiveIntegerField()
    benefit = models.ForeignKey(BenefitConsumption, models.DO_NOTHING, null=True, blank=True)
    code = models.CharField(max_length=255, null=True, blank=True)
    national_id = models.CharField(max_length=255, null=True, blank=True)
    submitted_data = models.JSONField(default=dict)
    mismatch_reasons = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.VALID)


class PayrollMutation(UUIDModel, ObjectMutation):
    payroll = models.ForeignKey(Payroll, models.DO_NOTHING, related_name='mutations')
    mutation = models.ForeignKey(
        MutationLog, models.DO_NOTHING, related_name='payroll')
