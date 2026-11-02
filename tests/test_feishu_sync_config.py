"""Feishu preflight uses fake settings and never contacts Feishu or user files."""
import unittest
from pathlib import Path
from unittest.mock import patch

import dashboard
from doubao_automation.feishu_client import FeishuClient, FeishuError


def config():
    return {'feishu': {'app_id': 'fixture-app', 'app_secret': 'fixture-secret',
                       'app_token': 'fixture-base',
                       'products_table': {'table_id': 'fixture-products'},
                       'scripts_table': {'table_id': 'fixture-scripts'}}}


class FeishuPreflightTests(unittest.TestCase):
    def test_missing_table_id_is_reported_before_network(self):
        cfg = config()
        cfg['feishu']['products_table']['table_id'] = ''
        client = FeishuClient(cfg)
        with patch.object(client, 'list_fields') as network:
            with self.assertRaisesRegex(FeishuError, 'products_table.table_id'):
                client.validate_sync_config()
            network.assert_not_called()

    def test_missing_prompt_is_reported_before_network(self):
        client = FeishuClient(config())
        with patch.object(Path, 'is_file', return_value=False), patch.object(client, 'list_fields') as network:
            with self.assertRaisesRegex(FeishuError, '固定提示词'):
                client.validate_sync_config()
            network.assert_not_called()

    def test_missing_private_config_is_visible_via_api(self):
        with patch.object(dashboard, 'load_config', side_effect=FileNotFoundError), \
             patch.object(dashboard, 'running', return_value=False):
            response = dashboard.app.test_client().post('/api/sync', json={}, headers={
                'Host': 'localhost', 'Origin': 'http://localhost',
                'X-CSRF-Token': dashboard.CSRF_TOKEN})
        self.assertEqual(response.status_code, 400)
        self.assertIn('config.example.yaml', response.json['error'])

    def test_successful_sync_preflights_before_fetch_and_saves_results(self):
        saved = {}
        with patch.object(dashboard, 'load_config', return_value=config()), \
             patch.object(dashboard, 'running', return_value=False), \
             patch.object(dashboard, 'FeishuClient') as client_type, \
             patch.object(dashboard, 'store') as store:
            client = client_type.return_value
            client.load_fixed_prompt.return_value = 'fixture prompt'
            client.fetch_products.return_value = []
            client.fetch_sources.return_value = []
            store.return_value.txn.return_value.__enter__.return_value = saved
            response = dashboard.app.test_client().post('/api/sync', json={}, headers={
                'Host': 'localhost', 'Origin': 'http://localhost',
                'X-CSRF-Token': dashboard.CSRF_TOKEN})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([call[0] for call in client.method_calls],
                         ['validate_sync_config', 'load_fixed_prompt', 'fetch_products', 'fetch_sources'])
        self.assertEqual(saved['fixed_prompt'], 'fixture prompt')


if __name__ == '__main__':
    unittest.main()
