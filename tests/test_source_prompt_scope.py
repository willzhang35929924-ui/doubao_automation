"""Row prompts cannot inherit another product's scanned prompt. No browser/network."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from doubao_automation import orchestrator


class SourcePromptScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='source-prompt-scope-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name, value in [('STATE_DIR', self.root), ('SCRIPTS_DIR', self.root)]:
            p = patch.object(orchestrator, name, value)
            p.start()
            self.addCleanup(p.stop)
        for name in ('ensure_dirs', 'BrowserSession', 'GeminiWeb'):
            p = patch.object(orchestrator, name)
            value = p.start()
            self.addCleanup(p.stop)
            setattr(self, name, value)
        self.GeminiWeb.return_value.generate.return_value = 'Script 01: fixture'
        for name, result in [('parse_scripts', ['Script 01: fixture']), ('detect_total_seconds', 15)]:
            p = patch.object(orchestrator, name, return_value=result)
            p.start()
            self.addCleanup(p.stop)
        self.cfg = {'feishu': {
            'fixed_prompt': {'source': 'bitable', 'table_id': 'source-table',
                             'field': '提示词', 'record_index': -1},
            'scripts_table': {'table_id': 'source-table', 'prompt_field': '提示词'}}}

    def pipeline(self, cfg=None, fixed='圣诞日历：生成1个50秒和一个70秒的脚本', sources=None):
        p = orchestrator.Pipeline(cfg or self.cfg)
        p.store.reset({'fixed_prompt': fixed, 'scripts': [],
                       'sources': sources if sources is not None else [
                           {'record_id': 'owala', 'keyword': 'OWALA',
                            'prompt_text': 'OWALA保温杯：生成4个15秒的脚本',
                            'videos': ['fixture.mp4']}]})
        return p

    def sent_messages(self):
        return [call.args[1] for call in self.GeminiWeb.return_value.generate.call_args_list]

    def test_scanned_other_product_prompt_is_not_sent_with_current_row(self):
        p = self.pipeline()
        p._stage_scripts_locked()
        self.assertEqual(self.sent_messages(), ['OWALA保温杯：生成4个15秒的脚本'])
        self.assertEqual(len(p.store.snapshot()['scripts']), 1)

    def test_each_row_uses_its_own_prompt(self):
        p = self.pipeline(sources=[
            dict(record_id='one', prompt_text='Product one: write 2 scripts'),
            dict(record_id='two', prompt_text='Product two: write 4 scripts')])
        p._stage_scripts_locked()
        self.assertEqual(self.sent_messages(), ['Product one: write 2 scripts',
                                               'Product two: write 4 scripts'])

    def test_empty_row_fails_before_opening_browser_without_borrowing_cached_prompt(self):
        p = self.pipeline(sources=[dict(record_id='empty', prompt_text='  ', videos=['fixture.mp4'])])
        before = p.store.snapshot()
        with self.assertRaisesRegex(RuntimeError, '源记录 empty 的提示词为空'):
            p._stage_scripts_locked()
        self.BrowserSession.assert_not_called()
        self.GeminiWeb.assert_not_called()
        self.assertEqual(p.store.snapshot(), before)

    def test_row_prompt_does_not_require_global_prompt_or_local_file(self):
        p = self.pipeline(fixed='')
        with patch.object(orchestrator, 'resolve') as resolve:
            p._stage_scripts_locked()
        resolve.assert_not_called()
        self.assertEqual(self.sent_messages(), ['OWALA保温杯：生成4个15秒的脚本'])

    def test_limit_only_validates_rows_that_will_be_processed(self):
        p = self.pipeline(sources=[dict(record_id='one', prompt_text='first prompt'),
                                   dict(record_id='two', prompt_text='')])
        p._stage_scripts_locked(limit=1)
        self.assertEqual(self.sent_messages(), ['first prompt'])

    def test_distinct_global_sources_keep_fixed_and_supplemental_prompt(self):
        for change in [dict(table_id='other-table'), dict(field='全局规范'),
                       dict(record_index=0), dict(source='file'), dict(source='docx')]:
            with self.subTest(change=change):
                self.GeminiWeb.reset_mock()
                self.GeminiWeb.return_value.generate.return_value = 'Script 01: fixture'
                cfg = copy.deepcopy(self.cfg)
                cfg['feishu']['fixed_prompt'].update(change)
                p = self.pipeline(cfg=cfg, fixed='Shared formatting rules')
                p._stage_scripts_locked()
                self.assertEqual(self.sent_messages(), [
                    'Shared formatting rules\n\n【本次附加提示词】\nOWALA保温杯：生成4个15秒的脚本'])


if __name__ == '__main__':
    unittest.main()
