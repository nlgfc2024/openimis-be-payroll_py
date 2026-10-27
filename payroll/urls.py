from django.urls import path

from payroll.views import send_callback_to_openimis, CSVReconciliationAPIView, DistrictReconciliationBatchAPIView

urlpatterns = [
    path('send_callback_to_openimis/', send_callback_to_openimis),
    path('csv_reconciliation/', CSVReconciliationAPIView.as_view()),
    path('district_reconciliation/', DistrictReconciliationBatchAPIView.as_view()),
]
