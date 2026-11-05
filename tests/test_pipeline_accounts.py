"""Account requirements apply to generation, not preparation or status commands."""
import copy
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

import run_pipeline
from doubao_automation import orchestrator
from doubao_automation.config import require_doubao_accounts


class PipelineAccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='doubao-pipeline-accounts-')
        self.root = Path(self.tmp.name)
        self.patches = [patch.object(orchestrator, 'STATE_DIR', self.root),
                        patch.object(orchestrator, 'ensure_dirs')]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_validation_distinguishes_missing_accounts_from_disabled_accounts(self):
        cases = [({}, 'accounts.doubao 为空'),
                 ({'accounts': {'doubao': []}}, 'accounts.doubao 为空'),
                 ({'accounts': {'doubao': None}}, 'accounts.doubao 为空'),
                 ({'accounts': {'doubao': [dict(name='fixture', enabled=False)]}}, '均未启用')]
        for cfg, message in cases:
            with self.subTest(cfg=cfg):
                before = copy.deepcopy(cfg)
                with self.assertRaisesRegex(ValueError, message) as error:
                    require_doubao_accounts(cfg)
                self.assertIn('账号库', str(error.exception))
                self.assertEqual(cfg, before)

    def test_validation_preserves_legacy_enabled_default_and_filters_disabled(self):
        accounts = [dict(name='disabled', enabled=False), dict(name='legacy'),
                    dict(name='enabled', enabled=True)]
        self.assertEqual(require_doubao_accounts({'accounts': {'doubao': accounts}}), accounts[1:])

    def test_preparation_and_status_work_without_enabled_doubao_accounts(self):
        for accounts in ([], [dict(name='fixture', enabled=False)]):
            with self.subTest(accounts=accounts):
                pipeline = orchestrator.Pipeline({'accounts': {'doubao': accounts}})
                with pipeline.store.txn() as data:
                    data.update(products=[dict(keyword='fixture', images=['fixture.png'])],
                                sources=[dict(record_id='fixture')],
                                scripts=[dict(id='s1', keyword='fixture', text='fixture script')],
                                tasks=[])
                segment = dict(index=0, total=1, prompt='fixture prompt', narration='', est_seconds=5)
                with patch.object(orchestrator, 'FeishuClient') as feishu, \
                     patch.object(orchestrator, 'BrowserSession') as browser, \
                     patch.object(orchestrator, 'build_segments', return_value=[segment]), \
                     redirect_stdout(io.StringIO()) as output:
                    pipeline.stage_fetch()
                    pipeline.stage_scripts()
                    pipeline.stage_plan()
                    pipeline.report()
                feishu.assert_not_called()
                browser.assert_not_called()
                self.assertEqual(len(pipeline.store.snapshot()['tasks']), 1)
                self.assertIn('暂无启用的豆包账号', output.getvalue())

    def test_generation_rejects_before_mutating_journal_or_running_any_stage(self):
        for accounts in ([], [dict(name='fixture', enabled=False)]):
            for method in ('stage_run', 'run_all'):
                with self.subTest(accounts=accounts, method=method):
                    pipeline = orchestrator.Pipeline({'accounts': {'doubao': accounts}})
                    pipeline.store.reset({'scripts': [{'id': 'saved'}], 'tasks': [{'id': 'saved#0'}]})
                    before = pipeline.store.snapshot()
                    with patch.object(pipeline, '_run_lock') as runner_lock, \
                         patch.object(pipeline, 'stage_fetch') as fetch, \
                         patch.object(pipeline, 'stage_scripts') as scripts:
                        with self.assertRaises(ValueError):
                            getattr(pipeline, method)()
                    runner_lock.assert_not_called()
                    fetch.assert_not_called()
                    scripts.assert_not_called()
                    self.assertEqual(pipeline.store.snapshot(), before)

    def test_enabled_account_reaches_generation_stage(self):
        pipeline = orchestrator.Pipeline({'accounts': {'doubao': [dict(name='fixture')]}})
        with patch.object(pipeline, '_stage_run_locked') as stage:
            pipeline.stage_run()
        stage.assert_called_once_with()

    def test_cli_explains_disabled_accounts_without_traceback(self):
        cfg = {'accounts': {'doubao': [dict(name='fixture', enabled=False)]}}
        with patch.object(sys, 'argv', ['run_pipeline.py', 'run']), \
             patch.object(run_pipeline, 'load_config', return_value=cfg), \
             patch.object(run_pipeline, 'ensure_dirs'), \
             patch.object(run_pipeline, 'use_workspace_temp'), \
             redirect_stderr(io.StringIO()) as error:
            result = run_pipeline.main()
        self.assertEqual(result, 2)
        self.assertIn('均未启用', error.getvalue())
        self.assertNotIn('Traceback', error.getvalue())


if __name__ == '__main__':
    unittest.main()
