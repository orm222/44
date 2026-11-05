"""FastAPI contract tests with a mocked DB; these do not verify PostgreSQL."""
from datetime import datetime, timezone
import unittest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
import psycopg
import main


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        self.connection = MagicMock()
        self.connection.__enter__.return_value = self.connection
        self.patch = patch.object(main, 'connect', return_value=self.connection)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.row = {'id': 7, 'text': 'hello', 'created_at': datetime(2026, 1, 1, tzinfo=timezone.utc)}

    def test_create_uses_bound_parameters_and_returns_committed_result(self):
        self.connection.execute.return_value.fetchone.return_value = self.row
        result = self.client.post('/api/notes', json={'text': '  hello  '})
        self.assertEqual(result.status_code, 201)
        self.assertEqual(result.json()['id'], 7)
        query, parameters = self.connection.execute.call_args.args
        self.assertIn('VALUES (%s)', query)
        self.assertEqual(parameters, ('hello',))
        self.connection.__exit__.assert_called_once_with(None, None, None)

    def test_invalid_text_is_rejected_before_database_access(self):
        for value in ('', '   ', 'x' * 1001, 123, None):
            with self.subTest(value_type=type(value).__name__):
                self.assertEqual(self.client.post('/api/notes', json={'text': value}).status_code, 422)
        self.connection.execute.assert_not_called()

    def test_list_returns_database_rows(self):
        self.connection.execute.return_value.fetchall.return_value = [self.row]
        response = self.client.get('/api/notes')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]['text'], 'hello')

    def test_readiness_requires_database_and_schema(self):
        self.assertEqual(self.client.get('/health/ready').status_code, 200)
        self.assertIn('FROM notes', self.connection.execute.call_args.args[0])

    def test_database_failure_is_503_without_connection_details(self):
        with patch.object(main, 'connect', side_effect=psycopg.OperationalError('password=private-value')):
            for method, path, kwargs in (
                ('get', '/health/ready', {}),
                ('get', '/api/notes', {}),
                ('post', '/api/notes', {'json': {'text': 'valid'}})
            ):
                response = getattr(self.client, method)(path, **kwargs)
                self.assertEqual(response.status_code, 503)
                self.assertNotIn('private-value', response.text)

    def test_unicode_and_sql_like_text_remain_a_parameter(self):
        value = "메모'); DROP TABLE notes; --"
        self.connection.execute.return_value.fetchone.return_value = dict(self.row, text=value)
        self.assertEqual(self.client.post('/api/notes', json={'text': value}).status_code, 201)
        self.assertEqual(self.connection.execute.call_args.args[1], (value,))


if __name__ == '__main__':
    unittest.main()
