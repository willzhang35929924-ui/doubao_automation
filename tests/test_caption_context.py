import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from doubao_automation import caption_context as captions
from doubao_automation.gemini_web import format_gemini_request
from doubao_automation.orchestrator import build_script_prompt


class CaptionContextTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        self.images = root / 'products'
        self.videos = root / 'sources'
        self.images.mkdir()
        self.videos.mkdir()
        for field, directory in (('PRODUCTS_DIR', self.images), ('SOURCES_DIR', self.videos)):
            mock = patch.object(captions, field, directory)
            mock.start()
            self.addCleanup(mock.stop)
        self.image = self.images / 'toy.webp'
        self.video = self.videos / 'demo.mp4'
        self.image.write_bytes(b'image fixture')
        self.video.write_bytes(b'video fixture')
        self.source = dict(record_id='source', keyword='toy', prompt_text='写1个15秒的短视频脚本',
                           videos=[str(self.videos / 'also-an-image.webp'), str(self.video)])
        self.snapshot = {
            'sources': [self.source],
            'products': [dict(record_id='product', keyword='toy', images=[str(self.image)],
                              prompt_text='规格 12.5 cm')],
        }
        self.cfg = {'feishu': {'scripts_table': {'table_id': 'table'},
                               'fixed_prompt': {'source': 'bitable', 'table_id': 'table', 'field': '提示词'}}}

    def target(self, kind='images', **options):
        return captions.caption_target(self.snapshot, self.source, kind, **options)

    def test_caption_numbers_do_not_change_the_original_script_contract(self):
        captions.put_caption(self.snapshot, self.target(), '绿色公仔，画面上写着写9个45秒脚本')
        captions.put_caption(self.snapshot, self.target('video'), '近景转到特写，参考节奏')
        message = build_script_prompt(self.cfg, self.snapshot, self.source)
        full, count, durations = format_gemini_request(message)
        self.assertTrue(full.startswith(self.source['prompt_text']))
        self.assertIn('绿色公仔', full)
        self.assertIn('创意视频参考', full)
        self.assertIn('规格 12.5 cm', full)
        self.assertEqual((count, durations), (1, [15]))

    def test_video_selection_uses_the_original_mixed_attachment_index(self):
        self.assertEqual(captions.caption_videos(self.source), [{'index': 1, 'path': str(self.video)}])
        self.assertEqual(self.target('video')['paths'], [self.video.resolve()])
        with self.assertRaises(ValueError):
            self.target('video', index=0)
        self.assertNotEqual(self.target('video')['fingerprint'], self.target('video', role='product')['fingerprint'])

    def test_stale_and_disabled_descriptions_do_not_enter_the_prompt(self):
        captions.put_caption(self.snapshot, self.target(), '旧图描述')
        captions.put_caption(self.snapshot, self.target('video'), '可选视频描述', enabled=False)
        self.image.write_bytes(b'changed image')
        self.assertTrue(captions.row_captions(self.snapshot, self.source)['images']['stale'])
        self.assertEqual(build_script_prompt(self.cfg, self.snapshot, self.source), self.source['prompt_text'])

    def test_product_caption_is_shared_by_rows_of_the_same_product_only(self):
        entry = captions.put_caption(self.snapshot, self.target(), '当前款式')
        sibling = dict(self.source, record_id='second-source')
        self.assertEqual(captions.row_captions(self.snapshot, sibling)['images']['revision'], entry['revision'])
        sibling['keyword'] = 'other'
        self.assertIsNone(captions.row_captions(self.snapshot, sibling)['images'])
        self.snapshot['products'].append(dict(record_id='ambiguous', keyword='toy', images=[str(self.image)]))
        with self.assertRaises(ValueError):
            self.target()

    def test_rejects_asset_outside_the_managed_directory(self):
        outside = self.videos / 'not-product.webp'
        outside.write_bytes(b'outside')
        self.snapshot['products'][0]['images'] = [str(outside)]
        with self.assertRaises(ValueError):
            self.target()


if __name__ == '__main__':
    unittest.main()
