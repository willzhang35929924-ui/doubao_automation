"""Offline record-completeness accounting; fixtures never contact Feishu."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from doubao_automation import feishu_client as module
from doubao_automation.feishu_client import FeishuClient, FeishuError


def config():
    return {'feishu': {'app_id': 'fixture-app', 'app_secret': 'fixture-secret',
                       'app_token': 'fixture-base', 'max_video_size_mb': 1,
                       'products_table': {'table_id': 'fixture-products'},
                       'scripts_table': {'table_id': 'fixture-scripts'}}}


class FeishuCompletenessTests(unittest.TestCase):
    def setUp(self):
        self.client = FeishuClient(config())
        self.headers = patch.object(self.client, '_headers', return_value={'Authorization': 'fixture'})
        self.headers.start()

    def tearDown(self):
        self.headers.stop()
        self.client.close()

    def test_all_record_pages_are_retained(self):
        pages = [
            {'code': 0, 'data': {'items': [{'record_id': 'one'}], 'has_more': True, 'page_token': 'page-two'}},
            {'code': 0, 'data': {'items': [{'record_id': 'two'}], 'has_more': True, 'page_token': 'page-three'}},
            {'code': 0, 'data': {'items': [{'record_id': 'three'}], 'has_more': False}},
        ]
        with patch.object(self.client, '_json', side_effect=pages) as request:
            records = self.client.list_records('fixture-table', page_size=1)
        self.assertEqual([r['record_id'] for r in records], ['one', 'two', 'three'])
        self.assertEqual([c.kwargs['params'] for c in request.call_args_list],
                         [{'page_size': 1}, {'page_size': 1, 'page_token': 'page-two'},
                          {'page_size': 1, 'page_token': 'page-three'}])

    def test_missing_or_invalid_next_page_token_never_reports_partial_success(self):
        for token in (None, '', ' ', 123):
            with self.subTest(token=token), patch.object(self.client, '_json', return_value={
                    'code': 0, 'data': {'items': [{'record_id': 'one'}], 'has_more': True, 'page_token': token}}) as request:
                with self.assertRaisesRegex(FeishuError, '分页标记'):
                    self.client.list_records('fixture-table')
                self.assertEqual(request.call_count, 1)

    def test_repeated_page_token_stops_before_looping_or_returning_duplicates(self):
        response = {'code': 0, 'data': {'items': [{'record_id': 'fixture'}],
                                        'has_more': True, 'page_token': 'repeat'}}
        with patch.object(self.client, '_json', return_value=response) as request:
            with self.assertRaisesRegex(FeishuError, '分页标记'):
                self.client.list_records('fixture-table')
            self.assertEqual(request.call_count, 2)

    def test_product_counts_explain_records_without_image_attachments(self):
        attachment = {'file_token': 'fixture-file', 'name': 'fixture.png',
                      'url': 'https://open.feishu.cn/fixture?extra=fixture'}
        records = [
            {'record_id': 'with-image', 'fields': {'keyword': 'fixture', '产品图': [attachment]}},
            {'record_id': 'empty', 'fields': {}},
            {'record_id': 'invalid-image', 'fields': {'产品图': [{'name': 'missing-token'}]}},
        ]
        with tempfile.TemporaryDirectory(prefix='feishu-counts-') as folder, \
             patch.object(module, 'PRODUCTS_DIR', Path(folder)), \
             patch.object(self.client, '_require_field', side_effect=lambda table, field, what: field), \
             patch.object(self.client, 'list_records', return_value=records), \
             patch.object(self.client, 'download_media', return_value=Path(folder)/'fixture.png') as download, \
             patch('builtins.print'):
            products = self.client.fetch_products()
        self.assertEqual([p['record_id'] for p in products], ['with-image'])
        self.assertEqual(self.client.sync_stats['products'], {
            'remote_records': 3, 'imported_records': 1, 'skipped_no_images': 2,
            'attachments': 1, 'downloaded_attachments': 1})
        self.assertIs(download.call_args.kwargs['attachment'], attachment)

    def test_source_counts_distinguish_empty_rows_and_oversized_only_rows(self):
        big = {'file_token': 'fixture-big', 'size': 2*1048576, 'name': 'big.mp4'}
        small = {'file_token': 'fixture-small', 'size': 10, 'name': 'small.mp4'}
        records = [
            {'record_id': 'prompt-only', 'fields': {'提示词': 'fixture'}},
            {'record_id': 'blank', 'fields': {}},
            {'record_id': 'large-only', 'fields': {'视频': [big]}},
            {'record_id': 'mixed', 'fields': {'提示词': 'fixture', '视频': [big, small]}},
            {'record_id': 'video-only', 'fields': {'视频': [small]}},
        ]
        with tempfile.TemporaryDirectory(prefix='feishu-counts-') as folder, \
             patch.object(module, 'SOURCES_DIR', Path(folder)), \
             patch.object(self.client, '_require_field', side_effect=lambda table, field, what: field), \
             patch.object(self.client, 'list_records', return_value=records), \
             patch.object(self.client, 'download_media', return_value=Path(folder)/'fixture.mp4') as download, \
             patch('builtins.print'):
            sources = self.client.fetch_sources()
        self.assertEqual([s['record_id'] for s in sources], ['prompt-only', 'mixed', 'video-only'])
        self.assertEqual(self.client.sync_stats['sources'], {
            'remote_records': 5, 'imported_records': 3, 'skipped_empty': 1,
            'skipped_oversized_only': 1, 'oversized_attachments': 2,
            'attachments': 4, 'downloaded_attachments': 2})
        self.assertEqual(download.call_count, 2)
        self.assertTrue(all(c.kwargs['attachment'] is small for c in download.call_args_list))

    def test_product_prompt_is_optional_and_preserves_default_or_configured_column(self):
        variants = [
            (None, {'提示词': [{'text': 'fixture '}, {'text': 'instructions'}]}, 'fixture instructions'),
            ('optional-custom', {'optional-custom': 'custom fixture'}, 'custom fixture'),
            (None, {}, ''),
        ]
        attachment = {'file_token': 'fixture-image', 'name': 'fixture.png'}
        for prompt_field, fields, expected in variants:
            with self.subTest(prompt_field=prompt_field, expected=expected):
                table = self.client.cfg['products_table']
                table.pop('prompt_field', None)
                if prompt_field:
                    table['prompt_field'] = prompt_field
                record = {'record_id': 'fixture-product', 'fields': dict(fields, 产品图=[attachment])}
                with tempfile.TemporaryDirectory(prefix='feishu-product-prompt-') as folder, \
                     patch.object(module, 'PRODUCTS_DIR', Path(folder)), \
                     patch.object(self.client, '_require_field', side_effect=lambda table, field, what: field) as require, \
                     patch.object(self.client, 'list_records', return_value=[record]), \
                     patch.object(self.client, 'download_media', return_value=Path(folder)/'fixture.png'), \
                     patch('builtins.print'):
                    products = self.client.fetch_products()
                self.assertEqual(products[0]['prompt_text'], expected)
                self.assertEqual([c.args[1] for c in require.call_args_list], ['产品图', 'keyword'],
                                 'optional product prompt must never become a required column')

    def test_same_product_names_and_attachment_names_do_not_overwrite_other_media(self):
        one = {'file_token': 'fixture-one', 'name': 'same.png'}
        two = {'file_token': 'fixture-two', 'name': 'same.png'}
        records = [
            {'record_id': 'record-a', 'fields': {'product-name': 'same product', '产品图': [one, two]}},
            {'record_id': 'record-b', 'fields': {'product-name': 'same product', '产品图': [one]}},
        ]
        self.client.cfg['products_table']['name_field'] = 'product-name'
        def download(token, dest, attachment=None):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(token.encode())
            return dest
        with tempfile.TemporaryDirectory(prefix='feishu-distinct-media-') as folder, \
             patch.object(module, 'PRODUCTS_DIR', Path(folder)), \
             patch.object(self.client, '_require_field', side_effect=lambda table, field, what: field), \
             patch.object(self.client, 'list_records', return_value=records), \
             patch.object(self.client, 'download_media', side_effect=download), \
             patch('builtins.print'):
            products = self.client.fetch_products()
            paths = [Path(p) for product in products for p in product['images']]
            self.assertEqual(len(set(paths)), 3)
            self.assertEqual([p.read_bytes() for p in paths], [b'fixture-one', b'fixture-two', b'fixture-one'])
            self.assertEqual([p.parent.name for p in paths], ['record-a', 'record-a', 'record-b'])
            self.assertEqual([p['name'] for p in products], ['same product', 'same product'])
            self.assertTrue(all('fixture-one' not in p.name and 'fixture-two' not in p.name for p in paths))

    def test_same_named_source_attachments_get_stable_distinct_bounded_paths(self):
        attachments = [{'file_token': 'fixture-one', 'name': '视频'*150+'.mp4'},
                       {'file_token': 'fixture-two', 'name': '视频'*150+'.mp4'}]
        records = [{'record_id': 'record-a', 'fields': {'视频': attachments}}]
        def download(token, dest, attachment=None):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(token.encode())
            return dest
        with tempfile.TemporaryDirectory(prefix='feishu-distinct-media-') as folder, \
             patch.object(module, 'SOURCES_DIR', Path(folder)), \
             patch.object(self.client, '_require_field', side_effect=lambda table, field, what: field), \
             patch.object(self.client, 'list_records', return_value=records), \
             patch.object(self.client, 'download_media', side_effect=download), \
             patch('builtins.print'):
            sources = self.client.fetch_sources()
            paths = [Path(p) for p in sources[0]['videos']]
            self.assertEqual(len(set(paths)), 2)
            self.assertEqual([p.read_bytes() for p in paths], [b'fixture-one', b'fixture-two'])
            self.assertTrue(all(len(p.name.encode('utf-8')) < 220 for p in paths))
            self.assertTrue(all(p.suffix == '.mp4' for p in paths))
            self.assertEqual([self.client._attachment_filename(a) for a in attachments], [p.name for p in paths])


if __name__ == '__main__':
    unittest.main()
