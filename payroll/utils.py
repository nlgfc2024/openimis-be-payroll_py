import random
from django.apps import apps


class CodeGenerator:
    @classmethod
    def generate_unique_code(cls, app_label, model_name, code_field_name, length):
        allowed_chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ123456789'
        code = ''.join(random.choice(allowed_chars) for _ in range(length))

        while cls._code_exists(app_label, model_name, code_field_name, code):
            code = ''.join(random.choice(allowed_chars) for _ in range(length))

        return code

    @classmethod
    def _code_exists(cls, app_label, model_name, code_field_name, code):
        model = apps.get_model(app_label=app_label, model_name=model_name)
        try:
            return model.objects.filter(**{code_field_name: code}).exists()
        except model.DoesNotExist:
            return False


class PayrollNameGenerator:
    """Build readable payroll names that remain globally unique.

    A sequence is appended by the payroll service, which locks the payment
    cycle during creation to avoid duplicate readable names.
    """

    PREFIX = "PAYROLL"
    MAX_LENGTH = 255

    @classmethod
    def generate(cls, payment_plan, payment_cycle, project_names, sequence):
        plan_code = str(payment_plan.code or "PLAN")
        cycle_code = str(payment_cycle.code or "CYCLE")
        projects = "+".join(project_names)
        sequence_suffix = f"-{sequence:03d}"
        # Reserve room for every meaningful part of the name. This prevents an
        # unusually long plan or cycle code from hiding the selected project.
        available_code_length = cls.MAX_LENGTH - len(cls.PREFIX) - len(sequence_suffix) - 3
        plan_length = min(len(plan_code), 64, available_code_length)
        cycle_length = min(len(cycle_code), 64, available_code_length - plan_length)
        project_length = available_code_length - plan_length - cycle_length

        return (
            f"{cls.PREFIX}-{plan_code[:plan_length]}-{cycle_code[:cycle_length]}-"
            f"{projects[:project_length]}{sequence_suffix}"
        )
