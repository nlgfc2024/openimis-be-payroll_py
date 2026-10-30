import os

from django.apps import AppConfig

from core.custom_filters import CustomFilterRegistryPoint
from payroll.payments_registry import PaymentsMethodRegistryPoint

MODULE_NAME = 'payroll'

DEFAULT_CONFIG = {
    "gql_payment_point_search_perms": ["201001"],
    "gql_payment_point_create_perms": ["201002"],
    "gql_payment_point_update_perms": ["201003"],
    "gql_payment_point_delete_perms": ["201004"],
    "gql_payroll_search_perms": ["202001"],
    "gql_payroll_create_perms": ["202002"],
    "gql_payroll_delete_perms": ["202004"],
    "gql_csv_reconciliation_search_perms": ["206001"],
    "gql_csv_reconciliation_create_perms": ["206002"],
    "payroll_accept_event": "payroll.accept_payroll",
    "payroll_reconciliation_event": "payroll.payroll_reconciliation",
    "payroll_reject_event": "payroll.payroll_reject",
    "csv_reconciliation_field_mapping": {
        "payrollbenefitconsumption__payroll__name": "Payroll Name",
        "payrollbenefitconsumption__payroll__status": "Payroll Status",
        "individual__location__parent__parent__parent__name": "District Name",
        "individual__json_ext__form_number": "Form Number",
        "individual__json_ext__household_mobile_number": "Phone Number",
        "individual__first_name": "First Name",
        "individual__last_name": "Last Name",
        "individual__json_ext__national_id": "National ID",
        "individual__dob": "Date of Birth",
        "code": "Code",
        "status": "Status",
        "amount": "Amount",
        "type": "Type",
        "receipt": "Receipt",
    },
    "csv_reconciliation_status_column": "Status",
    "csv_reconciliation_paid_extra_field": "Paid",
    "csv_reconciliation_receipt_column": "receipt",
    "csv_reconciliation_errors_column": "errors",
    "csv_reconciliation_code_column": "code",
    "csv_reconciliation_paid_yes": "Yes",
    "csv_reconciliation_paid_no": "No",
    # Reconciliation validation is policy driven.  The defaults deliberately
    # preserve the current behaviour: every known mismatch blocks application
    # and amount values must match exactly.
    "csv_reconciliation_blocking_mismatch_reasons": [
        "missing_code", "duplicate_code_in_file", "duplicate_national_id_in_file",
        "code_not_found", "code_not_in_payroll", "payroll_name_mismatch",
        "payroll_status_mismatch", "first_name_mismatch", "last_name_mismatch",
        "national_id_mismatch",
        "date_of_birth_mismatch", "status_mismatch", "amount_mismatch",
        "type_mismatch", "micro_catchment_mismatch", "invalid_paid_value",
        "missing_payment_reference",
    ],
    "csv_reconciliation_warning_mismatch_reasons": [],
    "csv_reconciliation_amount_tolerance": "0",
    "csv_reconciliation_micro_catchment_column": "Micro Catchment",
    "payroll_delete_event": "payroll.payroll_delete",
    "benefit_delete_event": "payroll.benefit_delete",

    "gateway_base_url": "http://41.175.18.170:8070/api/mobile/v1/",
    "endpoint_payment": "mock/payment",
    "endpoint_reconciliation": "mock/reconciliation",
    "payment_gateway_api_key": os.getenv('PAYMENT_GATEWAY_API_KEY'),
    "payment_gateway_basic_auth_username": os.getenv('PAYMENT_GATEWAY_BASIC_AUTH_USERNAME'),
    "payment_gateway_basic_auth_password": os.getenv('PAYMENT_GATEWAY_BASIC_AUTH_PASSWORD'),
    "payment_gateway_timeout": 5,
    "payment_gateway_auth_type": "basic",  # can be 'token' or 'basic'
    "payment_gateway_class": "payroll.payment_gateway.MockedPaymentGatewayConnector",
    "receipt_length": 8
}


class PayrollConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = MODULE_NAME

    gql_payment_point_search_perms = None
    gql_payment_point_create_perms = None
    gql_payment_point_update_perms = None
    gql_payment_point_delete_perms = None
    gql_payroll_search_perms = None
    gql_payroll_create_perms = None
    gql_payroll_delete_perms = None
    gql_csv_reconciliation_search_perms = None
    gql_csv_reconciliation_create_perms = None
    payroll_accept_event = None
    payroll_reconciliation_event = None
    payroll_reject_event = None
    csv_reconciliation_field_mapping = None
    csv_reconciliation_status_column = None
    csv_reconciliation_paid_extra_field = None
    csv_reconciliation_receipt_column = None
    csv_reconciliation_errors_column = None
    csv_reconciliation_code_column = None
    csv_reconciliation_paid_yes = None
    csv_reconciliation_paid_no = None
    csv_reconciliation_blocking_mismatch_reasons = None
    csv_reconciliation_warning_mismatch_reasons = None
    csv_reconciliation_amount_tolerance = None
    csv_reconciliation_micro_catchment_column = None
    payroll_delete_event = None
    benefit_delete_event = None

    gateway_base_url = None
    endpoint_payment = None
    endpoint_reconciliation = None
    payment_gateway_api_key = None
    payment_gateway_basic_auth_username = None
    payment_gateway_basic_auth_password = None
    payment_gateway_timeout = None
    payment_gateway_auth_type = None
    payment_gateway_class = None
    receipt_length = None

    # Only differences in submitted data may be treated as warnings. Identity,
    # payroll-membership, payment-state and payment-reference failures must
    # always block reconciliation.
    csv_reconciliation_warning_eligible_reasons = {
        "first_name_mismatch",
        "last_name_mismatch",
        "date_of_birth_mismatch",
        "amount_mismatch",
        "micro_catchment_mismatch",
    }

    def ready(self):
        from core.models import ModuleConfiguration

        cfg = ModuleConfiguration.get_or_default(self.name, DEFAULT_CONFIG)
        self.__load_config(cfg)
        self.__register_filters_and_payment_methods()

    @classmethod
    def __load_config(cls, cfg):
        """
        Load all config fields that match current AppConfig class fields, all custom fields have to be loaded separately
        """
        for field in cfg:
            if hasattr(PayrollConfig, field):
                setattr(PayrollConfig, field, cfg[field])
        cls.__validate_reconciliation_config()

    @classmethod
    def __validate_reconciliation_config(cls):
        warning_reasons = set(cls.csv_reconciliation_warning_mismatch_reasons or [])
        unsupported_warnings = warning_reasons - cls.csv_reconciliation_warning_eligible_reasons
        if unsupported_warnings:
            raise ValueError(
                "csv_reconciliation_warning_mismatch_reasons may only contain "
                f"data mismatches: {', '.join(sorted(unsupported_warnings))}"
            )

        blocking_reasons = set(cls.csv_reconciliation_blocking_mismatch_reasons or [])
        overlapping_reasons = warning_reasons & blocking_reasons
        if overlapping_reasons:
            raise ValueError(
                "Reconciliation mismatch reasons cannot be both blocking and warnings: "
                f"{', '.join(sorted(overlapping_reasons))}"
            )

    def __register_filters_and_payment_methods(cls):
        from social_protection.custom_filters import BenefitPlanCustomFilterWizard
        CustomFilterRegistryPoint.register_custom_filters(
            module_name=cls.name,
            custom_filter_class_list=[BenefitPlanCustomFilterWizard]
        )

        from payroll.strategies import (
            StrategyOfflinePayment,
            StrategyOnlinePayment
        )
        PaymentsMethodRegistryPoint.register_payment_method(
            payment_method_class_list=[
                StrategyOfflinePayment(),
                StrategyOnlinePayment(),
            ]
        )

    @staticmethod
    def get_payroll_payment_file_path(payroll_id, file_name=None):
        if file_name:
            return f"csv_reconciliation/payroll_{payroll_id}/{file_name}"
        return f"csv_reconciliation/payroll_{payroll_id}"
