from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from payroll.apps import PayrollConfig
from payroll.models import CsvReconciliationUpload
from payroll.services import CsvReconciliationService
from payroll.views import CSVReconciliationAPIView


class ReconciliationFeedbackTests(SimpleTestCase):
    def test_completed_response_distinguishes_row_errors(self):
        for errors, status in [
            (None, CsvReconciliationUpload.Status.SUCCESS),
            ({'A': ['receipt_required']}, CsvReconciliationUpload.Status.PARTIAL_SUCCESS),
        ]:
            with self.subTest(status=status), \
                    patch('payroll.views.CsvReconciliationUpload') as upload_model, \
                    patch('payroll.views.CsvReconciliationService') as service, \
                    patch('payroll.views.DefaultStorageFileHandler'):
                upload_model.Status = CsvReconciliationUpload.Status
                upload = upload_model.return_value
                upload.pk = 'upload-id'
                file = BytesIO(b'Code\nA\n')
                file.name = 'payment.csv'
                summary = {'affected_rows': 0 if errors else 1, 'skipped_items': 1 if errors else 0,
                           'total_number_of_benefits_in_file': 1}
                service.return_value.upload_reconciliation.return_value = (file, errors, summary)
                request = SimpleNamespace(GET={'payroll_id': 'payroll-id'}, FILES={'file': file},
                                          user=SimpleNamespace(login_name='tester'))
                # Exercise the handler without opening a database transaction; persistence is mocked.
                response = CSVReconciliationAPIView.post.__wrapped__(CSVReconciliationAPIView(), request)
                self.assertEqual(response.status_code, 201)
                self.assertEqual(response.data['status'], status)
                self.assertEqual(response.data['summary'], summary)
                self.assertEqual(response.data['errors'], errors or {})
                self.assertEqual(response.data['upload_id'], 'upload-id')

    @patch('payroll.views.logger')
    @patch('payroll.views.Payroll')
    @patch('payroll.views.CsvReconciliationUpload')
    def test_missing_file_returns_failure_without_exposing_exception(self, upload_model, payroll, logger):
        upload_model.Status = CsvReconciliationUpload.Status
        upload_model.return_value.pk = 'upload-id'
        request = SimpleNamespace(GET={'payroll_id': 'payroll-id'}, FILES={},
                                  user=SimpleNamespace(login_name='tester'))
        response = CSVReconciliationAPIView.post.__wrapped__(CSVReconciliationAPIView(), request)
        self.assertEqual(response.status_code, 500)
        self.assertFalse(response.data['success'])
        self.assertEqual(response.data['status'], CsvReconciliationUpload.Status.FAIL)
        self.assertEqual(response.data['error'], 'csv_reconciliation.processing_failed')
        self.assertIn('file_required', upload_model.return_value.error['error'])

    def test_summary_handles_multiple_errors_per_row(self):
        service = CsvReconciliationService(SimpleNamespace(login_name='tester'))
        service._resolve_payroll = Mock()
        service._validate_dataframe = Mock()
        service._reconcile_row = Mock(side_effect=[None, ['invalid_paid', 'receipt_required'], ['missing']])
        code_column = PayrollConfig.csv_reconciliation_code_column
        source_column = PayrollConfig.csv_reconciliation_field_mapping.get(code_column, code_column)
        file = BytesIO(f'{source_column}\nA\nB\nC\n'.encode())
        result_file, errors, summary = service.upload_reconciliation('payroll-id', file, Mock())
        self.assertEqual(summary, {'affected_rows': 1, 'skipped_items': 2,
                                   'total_number_of_benefits_in_file': 3})
        self.assertEqual(errors, {'B': ['invalid_paid', 'receipt_required'], 'C': ['missing']})
        self.assertIn(b'receipt_required', result_file.getvalue())

    @patch('payroll.services.BenefitConsumption')
    def test_unknown_benefit_is_a_row_error(self, benefits):
        benefits.objects.filter.return_value.first.return_value = None
        service = CsvReconciliationService(SimpleNamespace(login_name='tester'))
        errors = service._reconcile_row(Mock(), {'code': 'unknown'})
        self.assertEqual(len(errors), 1)

    def test_duplicate_name_is_reported_only_before_processing(self):
        for late_collision in (False, True):
            with self.subTest(late_collision=late_collision), \
                    patch('payroll.views.logger'), \
                    patch('payroll.views.Payroll'), \
                    patch('payroll.views.CsvReconciliationUpload') as upload_model, \
                    patch('payroll.views.CsvReconciliationService') as service, \
                    patch('payroll.views.DefaultStorageFileHandler') as storage:
                upload_model.Status = CsvReconciliationUpload.Status
                upload_model.return_value.pk = 'upload-id'
                file = BytesIO(b'Code\nA\n')
                file.name = 'payment.csv'
                handler = storage.return_value
                if late_collision:
                    handler.save_file.side_effect = FileExistsError('already exists')
                    service.return_value.upload_reconciliation.return_value = (file, None, {})
                else:
                    handler.check_file_path.side_effect = FileExistsError('already exists')
                request = SimpleNamespace(GET={'payroll_id': 'payroll-id'}, FILES={'file': file},
                                          user=SimpleNamespace(login_name='tester'))
                response = CSVReconciliationAPIView.post.__wrapped__(CSVReconciliationAPIView(), request)
                self.assertFalse(response.data['success'])
                self.assertEqual(response.status_code, 500 if late_collision else 409)
                self.assertEqual(response.data['error'], 'csv_reconciliation.processing_failed' if late_collision
                                 else 'csv_reconciliation.duplicate_file')
                if not late_collision:
                    service.assert_not_called()
