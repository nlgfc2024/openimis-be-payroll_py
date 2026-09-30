import logging

from django.db import transaction
from rest_framework import views
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from core.utils import DefaultStorageFileHandler
from core.views import check_user_rights
from payroll.apps import PayrollConfig
from payroll.models import Payroll, CsvReconciliationUpload
from payroll.payments_registry import PaymentMethodStorage
from payroll.services import CsvReconciliationService

logger = logging.getLogger(__name__)


@api_view(["POST"])
@permission_classes([check_user_rights(PayrollConfig.gql_payroll_create_perms, )])
def send_callback_to_openimis(request):
    try:
        user = request.user
        payroll_id, response_from_gateway, rejected_bills = \
            _resolve_send_callback_to_imis_args(request)
        payroll = Payroll.objects.get(id=payroll_id)
        strategy = PaymentMethodStorage.get_chosen_payment_method(payroll.payment_method)
        if strategy:
            # save the reponse from gateway in openIMIS
            strategy.acknowledge_of_reponse_view(
                payroll,
                response_from_gateway,
                user,
                rejected_bills
            )
        return Response({'success': True, 'error': None}, status=201)
    except ValueError as exc:
        logger.error("Error while sending callback to openIMIS", exc_info=exc)
        return Response({'success': False, 'error': str(exc)}, status=400)
    except Exception as exc:
        logger.error("Unexpected error while sending callback to openIMIS", exc_info=exc)
        return Response({'success': False, 'error': str(exc)}, status=500)


def _resolve_send_callback_to_imis_args(request):
    payroll_id = request.data.get('payroll_id')
    response_from_gateway = request.data.get('response_from_gateway')
    rejected_bills = request.data.get('rejected_bills')
    if not payroll_id:
        raise ValueError('Payroll Id not provided')
    if not response_from_gateway:
        raise ValueError('Response from gateway not provided')
    if rejected_bills is None:
        raise ValueError('Rejected Bills not provided')

    return payroll_id, response_from_gateway, rejected_bills


class CSVReconciliationAPIView(views.APIView):
    permission_classes = [check_user_rights(PayrollConfig.gql_csv_reconciliation_create_perms, )]

    def get(self, request):
        try:
            payroll_id = request.GET.get('payroll_id')
            get_blank = request.GET.get('blank')
            get_blank_bool = get_blank.lower() == 'true'

            if get_blank_bool:
                service = CsvReconciliationService(request.user)
                in_memory_file = service.download_reconciliation(payroll_id)
                response = Response(headers={'Content-Disposition': 'attachment; filename="reconciliation.csv"'},
                                    content_type='text/csv')
                response.content = in_memory_file.getvalue()
                return response
            else:
                file_name = request.GET.get('payroll_file_name')
                path = PayrollConfig.get_payroll_payment_file_path(payroll_id, file_name)
                file_handler = DefaultStorageFileHandler(path)
                return file_handler.get_file_response_csv(file_name)
        except ValueError as exc:
            logger.error("Error while generating CSV reconciliation", exc_info=exc)
            return Response({'success': False, 'error': str(exc)}, status=400)
        except FileNotFoundError as exc:
            logger.error("File not found", exc_info=exc)
            return Response({'success': False, 'error': str(exc)}, status=404)
        except Exception as exc:
            logger.error("Error while generating CSV reconciliation", exc_info=exc)
            return Response({'success': False, 'error': str(exc)}, status=500)

    @transaction.atomic
    def post(self, request):
        upload = CsvReconciliationUpload()
        payroll_id = request.GET.get('payroll_id')
        processing_started = False
        try:
            upload.save(username=request.user.login_name)
            file = request.FILES.get('file')
            if file is None:
                raise ValueError('csv_reconciliation.validation.file_required')
            target_file_path = PayrollConfig.get_payroll_payment_file_path(payroll_id, file.name)
            upload.file_name = file.name
            file_handler = DefaultStorageFileHandler(target_file_path)
            file_handler.check_file_path()
            processing_started = True
            service = CsvReconciliationService(request.user)
            file_to_upload, errors, summary = service.upload_reconciliation(payroll_id, file, upload)
            if errors:
                upload.status = CsvReconciliationUpload.Status.PARTIAL_SUCCESS
                upload.error = errors
                upload.json_ext = {'extra_info': summary}
            else:
                upload.status = CsvReconciliationUpload.Status.SUCCESS
                upload.json_ext = {'extra_info': summary}
            upload.save(username=request.user.login_name)
            file_handler.save_file(file_to_upload)
            return Response({
                'success': True,
                'error': None,
                'status': upload.status,
                'upload_id': str(upload.pk),
                'summary': summary,
                'errors': errors or {},
            }, status=201)
        except Exception as exc:
            duplicate_file = isinstance(exc, FileExistsError) and not processing_started
            logger.error("Error while uploading CSV reconciliation", exc_info=exc)
            if upload:
                upload.error = {'error': str(exc)}
                upload.payroll = Payroll.objects.filter(id=payroll_id).first()
                upload.status = CsvReconciliationUpload.Status.FAIL
                summary = {
                    'affected_rows': 0,
                }
                upload.json_ext = {'extra_info': summary}
                upload.save(username=request.user.login_name)
            return Response({
                'success': False,
                'error': ('csv_reconciliation.duplicate_file' if duplicate_file
                          else 'csv_reconciliation.processing_failed'),
                'status': CsvReconciliationUpload.Status.FAIL,
                'upload_id': str(upload.pk) if upload.pk else None,
            }, status=409 if duplicate_file else 500)
