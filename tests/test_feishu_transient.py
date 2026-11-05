"""Offline regressions for Feishu DataNotReady reads; no real network or credentials."""
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from doubao_automation import feishu_client as module
from doubao_automation.feishu_client import FeishuClient, FeishuError
from tests.test_feishu_http import Response, config


def ready(items=(), *, more=False, cursor=None):
    data = {'items': list(items), 'has_more': more}
    if cursor is not None:
        data['page_token'] = cursor
    return Response(payload={'code': 0, 'data': data})


def not_ready(request_id='fixture-transient', *, status=400, headers=None):
    return Response(status, payload={
        'code': 1254607,
        'msg': 'fixture-private-body fixture-secret https://private.example/?signature=fixture',
    }, headers={'X-Tt-Logid': request_id, **(headers or {})})


class FeishuTransientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='feishu-transient-test-')
        self.root = Path(self.temp.name)
        self.logs = patch.object(module, 'LOGS_DIR', self.root / 'logs')
        self.logs.start()
        self.sleep = patch.object(module.time, 'sleep')
        self.sleep_mock = self.sleep.start()
        self.client = FeishuClient(config())
        self.client._token = 'fixture-token'
        self.client._token_expire = time.time() + 7000
        self.calls = []

    def tearDown(self):
        self.client.close()
        self.sleep.stop()
        self.logs.stop()
        self.temp.cleanup()

    def serve(self, responses):
        pending = iter(responses)

        def request(method, url, **kwargs):
            # Capture values at send time: later page-size adaptation must not
            # alter the evidence for an earlier request's cursor or size.
            self.calls.append((method, url, copy.deepcopy(kwargs)))
            response = next(pending)
            if isinstance(response, BaseException):
                raise response
            return response

        self.request = Mock(side_effect=request)
        self.client.session.request = self.request

    def sizes(self):
        return [kwargs['params']['page_size'] for _, _, kwargs in self.calls]

    def test_400_data_not_ready_retries_then_closes_responses_and_succeeds(self):
        first = not_ready(headers={'Retry-After': '3600'})
        final = ready([{'record_id': 'record-a'}])
        self.serve([first, final])
        self.assertEqual(self.client.list_records('fixture-table'), [{'record_id': 'record-a'}])
        self.assertEqual(self.request.call_count, 2)
        self.assertEqual(self.sizes(), [100, 50])
        self.assertTrue(first.closed)
        self.assertTrue(final.closed)
        self.assertEqual(self.sleep_mock.call_count, 1)
        self.assertEqual(self.sleep_mock.call_args.args[0], 5,
                         'Retry-After must be respected while capped at five seconds')
        self.assertTrue(self.client.session.verify)
        for method, _, kwargs in self.calls:
            self.assertEqual(method, 'GET')
            self.assertNotIn('verify', kwargs)

    def test_retrying_second_page_preserves_cursor_and_all_records(self):
        self.serve([
            ready([{'record_id': 'record-a'}], more=True, cursor='cursor-2'),
            not_ready('fixture-page-two-first'),
            not_ready('fixture-page-two-second'),
            ready([{'record_id': 'record-b'}], more=True, cursor='cursor-3'),
            ready([{'record_id': 'record-c'}]),
        ])
        rows = self.client.list_records('fixture-table')
        self.assertEqual([row['record_id'] for row in rows], ['record-a', 'record-b', 'record-c'])
        self.assertEqual(self.sizes(), [100, 100, 50, 25, 25])
        self.assertEqual([kwargs['params'].get('page_token') for _, _, kwargs in self.calls],
                         [None, 'cursor-2', 'cursor-2', 'cursor-2', 'cursor-3'])
        self.assertEqual(self.sleep_mock.call_count, 2)

    def test_exhausted_read_has_one_five_attempt_budget_and_safe_diagnostic(self):
        responses = [not_ready(f'fixture-request-{index}') for index in range(1, 6)]
        self.serve(responses)
        with self.assertRaises(FeishuError) as caught:
            self.client.list_records('fixture-table')
        error = caught.exception
        self.assertEqual(self.request.call_count, 5)
        self.assertEqual(self.sizes(), [100, 50, 25, 12, 6])
        self.assertEqual(self.sleep_mock.call_count, 4)
        self.assertTrue(all(response.closed for response in responses))
        self.assertTrue(error.retryable)
        self.assertEqual(error.api_code, 1254607)
        self.assertEqual(error.request_id, 'fixture-request-5')
        self.assertIn('1254607', str(error))
        self.assertIn('fixture-request-5', str(error))
        self.assertIn('未就绪', str(error))
        log = (self.root / 'logs/feishu_sync.log').read_text()
        events = [json.loads(line) for line in log.splitlines()]
        self.assertTrue(any(event.get('api_code') == 1254607 for event in events))
        for secret in ('fixture-private-body', 'fixture-secret', 'private.example', 'fixture-token'):
            self.assertNotIn(secret, str(error))
            self.assertNotIn(secret, log)

    def test_200_business_data_not_ready_also_retries(self):
        first = not_ready(status=200)
        self.serve([first, ready([{'record_id': 'record-a'}])])
        self.assertEqual(self.client.list_records('fixture-table'), [{'record_id': 'record-a'}])
        self.assertEqual(self.request.call_count, 2)
        self.assertTrue(first.closed)

    def test_non_record_get_keeps_the_default_three_attempt_limit(self):
        self.serve([not_ready(f'fixture-fields-{index}') for index in range(3)])
        with self.assertRaises(FeishuError) as caught:
            self.client.list_fields('fixture-table')
        self.assertEqual(self.request.call_count, 3)
        self.assertEqual(self.sleep_mock.call_count, 2)
        self.assertTrue(caught.exception.retryable)

    def test_permission_or_unknown_400_does_not_retry(self):
        for code in (1254302, 1254999):
            with self.subTest(code=code):
                self.calls.clear()
                self.sleep_mock.reset_mock()
                self.serve([Response(400, payload={'code': code, 'msg': 'fixture-secret'})])
                with self.assertRaises(FeishuError) as caught:
                    self.client.list_records('fixture-table')
                self.assertEqual(self.request.call_count, 1)
                self.assertEqual(self.sleep_mock.call_count, 0)
                self.assertFalse(caught.exception.retryable)
                self.assertNotIn('fixture-secret', str(caught.exception))

    def test_non_json_400_does_not_retry(self):
        self.serve([Response(400)])
        with self.assertRaises(FeishuError):
            self.client.list_records('fixture-table')
        self.assertEqual(self.request.call_count, 1)
        self.assertEqual(self.sleep_mock.call_count, 0)

    def test_auth_post_business_error_is_never_replayed(self):
        self.client._token = ''
        self.serve([not_ready()])
        with self.assertRaises(FeishuError):
            self.client.token()
        self.assertEqual(self.request.call_count, 1)
        self.assertEqual(self.calls[0][0], 'POST')
        self.assertEqual(self.sleep_mock.call_count, 0)

    def test_mixed_transport_http_and_business_errors_share_the_record_budget(self):
        responses = [requests.exceptions.ConnectTimeout('fixture-private-body'),
                     Response(503), not_ready(), Response(503), not_ready('fixture-final')]
        self.serve(responses)
        with self.assertRaises(FeishuError) as caught:
            self.client.list_records('fixture-table')
        self.assertEqual(self.request.call_count, 5)
        self.assertEqual(self.sleep_mock.call_count, 4)
        self.assertEqual(caught.exception.request_id, 'fixture-final')
        self.assertTrue(all(response.closed for response in responses if isinstance(response, Response)))

    def test_page_size_never_reaches_zero(self):
        self.serve([not_ready(), not_ready(), ready([{'record_id': 'record-a'}])])
        self.assertEqual(self.client.list_records('fixture-table', page_size=1), [{'record_id': 'record-a'}])
        self.assertEqual(self.sizes(), [1, 1, 1])


if __name__ == '__main__':
    unittest.main()
