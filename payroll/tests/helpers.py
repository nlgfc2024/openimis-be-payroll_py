from location.models import Location
from payroll.models import PaymentPoint
from core.test_helpers import LogInHelper


class PaymentPointHelper:
    _TEST_DATA_PAYMENT_POINT = None

    def __init__(self):
        self._TEST_DATA_PAYMENT_POINT = {
            "name": "TestPaymentPoint",
            "location": Location.objects.filter(validity_to__isnull=True, type='V').first()
        }

    def get_or_create_payment_point_api(self, **kwargs):
        payment_point = PaymentPoint.objects.filter(name={**self._TEST_DATA_PAYMENT_POINT, **kwargs}["name"]).first()
        if payment_point is None:
            payment_point = self.__create_payment_point(**kwargs)
        return payment_point

    def __create_payment_point(self, **kwargs):
        user = LogInHelper().get_or_create_user_api(
            password='PayrollTestPassword1!',
        )
        name = {**self._TEST_DATA_PAYMENT_POINT, **kwargs}["name"],
        location = {**self._TEST_DATA_PAYMENT_POINT, **kwargs}["location"]
        payment_point = PaymentPoint(
            name=name,
            location=location,
            ppm=user
        )
        payment_point.save(username=user.username)
        return payment_point
