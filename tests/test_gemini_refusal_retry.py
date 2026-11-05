"""Only proven complete rejected replies may retry; no browser or network."""
from contextlib import ExitStack, redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from doubao_automation import orchestrator as module
from doubao_automation.gemini_web import (GeminiError, GeminiRefusalResponse, GeminiResponseRejected,
                                         GeminiSubmissionUncertain)


VALID_SCRIPT='''Script 01：测试（总时长15秒）
总时长：15秒
00:00-00:05
画面：展示产品。口播：第一段。
00:05-00:10
画面：演示产品。口播：第二段。
00:10-00:15
画面：展示结果。口播：第三段。
'''
INSTRUCTION='原始指令：写一个该产品功能场景应用的15秒短视频脚本，不改写我的要求。'
FILES=['fixture-first.webp','fixture-second.mp4']


def retryable_response(message='completed response rejected',kind=GeminiResponseRejected):
    error=kind(message)
    error.can_retry_same_request=True  # Simulate generate's successfully cached proof.
    return error


class GeminiRefusalRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='gemini-refusal-retry-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        for name in ('STATE_DIR','SCRIPTS_DIR'):
            self.stack.enter_context(patch.object(module,name,self.root))
        self.stack.enter_context(patch.object(module,'ensure_dirs'))
        self.session=self.stack.enter_context(patch.object(module,'BrowserSession'))
        self.gemini=self.stack.enter_context(patch.object(module,'GeminiWeb'))
        self.doubao=self.stack.enter_context(patch.object(module,'DoubaoAgent'))
        self.sleep=self.stack.enter_context(patch.object(module.time,'sleep'))
        self.cfg={'feishu':{
            'fixed_prompt':{'source':'bitable','table_id':'fixture-table','field':'提示词','record_index':-1},
            'scripts_table':{'table_id':'fixture-table','prompt_field':'提示词'}},
            'gemini':{'refusal_retry_delay_seconds':5}}
        self.pipeline=module.Pipeline(self.cfg)
        self.pipeline.store.reset({'sources':[dict(record_id='fixture-record',keyword='fixture',
            prompt_text=INSTRUCTION,videos=list(FILES))], 'scripts':[], 'tasks':[], 'requests':{}})

    def generated_inputs(self):
        return [(call.args[0],call.args[1]) for call in self.gemini.return_value.method_calls
                if call[0] in ('generate','retry_last_rejected_response')]

    def test_completed_refusal_then_success_retains_one_valid_script(self):
        self.gemini.return_value.generate.side_effect=retryable_response(kind=GeminiRefusalResponse)
        self.gemini.return_value.retry_last_rejected_response.return_value=VALID_SCRIPT
        output=io.StringIO()
        with redirect_stdout(output):self.pipeline._stage_scripts_locked()
        self.assertEqual(self.generated_inputs(),[(FILES,INSTRUCTION),(FILES,INSTRUCTION)])
        self.assertEqual(self.gemini.return_value.generate.call_count,1)
        self.assertEqual(self.gemini.return_value.retry_last_rejected_response.call_count,1)
        saved=self.pipeline.store.snapshot()
        self.assertEqual(len(saved['scripts']),1)
        self.assertEqual(saved['scripts'][0]['total_seconds'],15)
        self.assertEqual(saved['tasks'],[])
        self.doubao.assert_not_called()
        self.sleep.assert_called_once_with(5)
        self.session.return_value.stop.assert_called_once()
        self.assertIn('保持原指令/素材同会话重试2/3',output.getvalue())
        self.assertIn('尚未提交豆包',output.getvalue())

    def test_all_refusals_stop_at_three_calls_and_do_not_save_fake_scripts(self):
        self.gemini.return_value.generate.side_effect=retryable_response()
        self.gemini.return_value.retry_last_rejected_response.side_effect=retryable_response()
        before=self.pipeline.store.snapshot()
        with self.assertRaises(GeminiResponseRejected):self.pipeline._stage_scripts_locked()
        self.assertEqual(self.generated_inputs(),[(FILES,INSTRUCTION)]*3)
        self.assertEqual(self.gemini.return_value.generate.call_count,1)
        self.assertEqual(self.gemini.return_value.retry_last_rejected_response.call_count,2)
        self.assertEqual(self.sleep.call_count,2)
        self.assertEqual(self.pipeline.store.snapshot(),before)
        self.assertEqual(list(self.root.glob('*.txt')),[])
        self.session.return_value.stop.assert_called_once()
        self.doubao.assert_not_called()

    def test_unknown_send_and_invalid_format_are_never_replayed(self):
        for error in (GeminiSubmissionUncertain('unknown send'),GeminiResponseRejected('invalid format'),
                      GeminiRefusalResponse('refusal without completed context')):
            with self.subTest(error=type(error).__name__):
                self.gemini.return_value.generate.reset_mock()
                self.gemini.return_value.retry_last_rejected_response.reset_mock()
                self.sleep.reset_mock()
                self.gemini.return_value.generate.side_effect=error
                with self.assertRaises(type(error)):self.pipeline._stage_scripts_locked()
                self.assertEqual(self.gemini.return_value.generate.call_count,1)
                self.gemini.return_value.retry_last_rejected_response.assert_not_called()
                self.sleep.assert_not_called()
                self.assertEqual(self.pipeline.store.snapshot()['scripts'],[])
                self.doubao.assert_not_called()

    def test_each_retry_receives_original_paths_even_if_previous_callee_mutates_copy(self):
        received=[]
        def generate(paths,message):
            received.append((list(paths),message))
            if len(received)<3:
                paths.clear()
                raise retryable_response()
            return VALID_SCRIPT
        self.gemini.return_value.generate.side_effect=generate
        self.gemini.return_value.retry_last_rejected_response.side_effect=generate
        self.pipeline._stage_scripts_locked()
        self.assertEqual(received,[(FILES,INSTRUCTION)]*3)
        self.assertEqual(self.pipeline.store.snapshot()['sources'][0]['videos'],FILES)

    def test_configured_zero_retries_performs_one_call(self):
        self.cfg['gemini']['retry_on_error']=0
        self.gemini.return_value.generate.side_effect=retryable_response()
        with self.assertRaises(GeminiResponseRejected):self.pipeline._stage_scripts_locked()
        self.assertEqual(self.gemini.return_value.generate.call_count,1)
        self.gemini.return_value.retry_last_rejected_response.assert_not_called()
        self.sleep.assert_not_called()

    def test_configured_large_retry_count_is_still_bounded_to_three_calls(self):
        self.cfg['gemini']['retry_on_error']=20
        self.gemini.return_value.generate.side_effect=retryable_response()
        self.gemini.return_value.retry_last_rejected_response.side_effect=retryable_response()
        with self.assertRaises(GeminiResponseRejected):self.pipeline._stage_scripts_locked()
        self.assertEqual(self.gemini.return_value.generate.call_count,1)
        self.assertEqual(self.gemini.return_value.retry_last_rejected_response.call_count,2)
        self.assertEqual(self.sleep.call_count,2)

    def test_proven_format_quantity_and_duration_rejections_retry_without_word_matching(self):
        original=self.pipeline.store.snapshot()
        for reason in ('missing script format','wrong script quantity','wrong total duration'):
            with self.subTest(reason=reason):
                self.pipeline.store.reset(original)
                self.gemini.return_value.reset_mock(side_effect=True)
                self.sleep.reset_mock()
                self.gemini.return_value.generate.side_effect=retryable_response(reason)
                self.gemini.return_value.retry_last_rejected_response.return_value=VALID_SCRIPT
                self.pipeline._stage_scripts_locked()
                self.assertEqual(self.gemini.return_value.generate.call_count,1)
                self.assertEqual(self.gemini.return_value.retry_last_rejected_response.call_count,1)
                self.assertEqual(self.generated_inputs(),[(FILES,INSTRUCTION)]*2)
                self.assertEqual(len(self.pipeline.store.snapshot()['scripts']),1)
                self.doubao.assert_not_called()

    def test_unproven_helper_failure_cannot_replay_consumed_context(self):
        self.gemini.return_value.generate.side_effect=retryable_response()
        self.gemini.return_value.retry_last_rejected_response.side_effect=GeminiError('context failed before proof')
        with self.assertRaises(GeminiError):self.pipeline._stage_scripts_locked()
        self.assertEqual(self.gemini.return_value.generate.call_count,1)
        self.assertEqual(self.gemini.return_value.retry_last_rejected_response.call_count,1)
        self.sleep.assert_called_once_with(5)
        self.assertEqual(self.pipeline.store.snapshot()['scripts'],[])


if __name__=='__main__':unittest.main()
