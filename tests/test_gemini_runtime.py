"""Gemini runtime guards. No real browser, upload, credentials or model calls."""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from doubao_automation import gemini_web as module
from doubao_automation.gemini_web import GeminiWeb,GeminiError,GeminiNotLoggedIn


def snapshot(texts,users=1,user_texts=None,attachment_counts=None):
    return dict(model_count=len(texts),user_count=users,texts=texts,selector='model-response',
                user_texts=user_texts if user_texts is not None else ['prior message']*users,
                user_attachment_counts=attachment_counts if attachment_counts is not None else [0]*users,
                response_hashes=[hashlib.sha256(t.encode()).hexdigest() for t in texts])


def script_fixture(index,total):
    stamp=lambda second:f'{second//60:02d}:{second%60:02d}'
    return f'Script {index:02d}：测试（总时长{total}秒）\n总时长：{total}秒\n'+''.join(
        f'{stamp(start)} - {stamp(min(total,start+5))}\n画面：测试场景{start}。\n口播：独立测试片段{start}。\n'
        for start in range(0,total,5))


class Locator:
    def __init__(self,page,selector,index=0):self.page,self.selector,self.index=page,selector,index
    @property
    def first(self):return self
    def nth(self,index):return Locator(self.page,self.selector,index)
    def count(self):return len(self.page.values.get(self.selector,[]))
    def inner_text(self,**kwargs):return self.page.values[self.selector][self.index]
    def is_visible(self):return self.count()>0 and (self.selector,self.index) not in self.page.hidden
    def is_enabled(self):return self.page.send_enabled if self.selector=='send' else True
    def wait_for(self,**kwargs):
        if self.page.editor_unavailable:raise TimeoutError('Editor missing')
    def click(self,**kwargs):
        self.page.clicked.append(self.selector)
        if self.selector=='send' and self.page.send_click_failure:raise TimeoutError('unit click timeout')
    def fill(self,text,**kwargs):self.page.draft=text


class Page:
    def __init__(self):
        self.now=0.0;self.url='https://gemini.google.com/app/test?secret=never-store'
        self.values={'body':['Local diagnostic page'],'editor':[''], 'send':['Send']}
        self.clicked=[];self.draft='old draft';self.editor_unavailable=False;self.send_enabled=True
        self.hidden=set();self.send_click_failure=False
        self.keyboard=Mock();self.get_by_role=Mock()
    def locator(self,selector):return Locator(self,selector)
    def wait_for_timeout(self,milliseconds):self.now+=milliseconds/1000
    def goto(self,url,**kwargs):self.url=url
    def screenshot(self,path,**kwargs):Path(path).write_bytes(b'unit-test screenshot placeholder')


class GeminiRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='gemini_runtime_')
        self.addCleanup(self.temp.cleanup)
        self.page=Page()
        self.cfg={'gemini':{'audit_dir':self.temp.name,'response_stable_seconds':1,
                           'response_start_timeout_s':3,'response_timeout_minutes':.1,
                           'send_ack_timeout_seconds':2,'send_ready_timeout_seconds':2,'selectors':{
                               'prompt_editor':['editor'],'send_button':['send']}}}
        self.agent=GeminiWeb(SimpleNamespace(page=self.page),self.cfg)

    def refusal_chat(self,replies=None):
        media=Path(self.temp.name)/'fixture.mp4';media.write_bytes(b'fixture-video-a')
        paths=(str(media),);message='请写一个该产品功能场景应用的15秒短视频脚本'
        state={'users':[],'models':[],'attachments':[]}
        pending=iter(replies or ['由于程序代码的局限，我没法办到。',script_fixture(1,15)])
        original_goto=self.page.goto
        self.page.goto=Mock(side_effect=original_goto)
        self.page.draft=''
        self.agent.open=Mock(side_effect=lambda:self.page.goto('https://gemini.google.com/app/refusal-unit'))
        self.agent._stop_visible=Mock(return_value=False)
        self.agent._draft_text=Mock(side_effect=lambda **kwargs:self.page.draft)
        self.agent._response_snapshot=lambda:snapshot(list(state['models']),len(state['users']),
            user_texts=list(state['users']),attachment_counts=list(state['attachments']))
        self.agent.type_prompt=Mock(side_effect=lambda text:setattr(self.page,'draft',text))
        self.agent.upload_files=Mock()
        def send(text):
            self.agent._submission_attempted=True
            state['users'].append(text);state['attachments'].append(len(paths));self.page.draft=''
        def response():
            text=next(pending);state['models'].append(text);return text
        self.agent.send_prompt=Mock(side_effect=send)
        self.agent.wait_response=Mock(side_effect=response)
        return paths,message,state

    def test_old_stable_text_is_not_a_new_response(self):
        base=snapshot(['old stable answer'])
        self.assertEqual(self.agent._fresh_response(base,base),'')
        self.assertEqual(self.agent._fresh_response(snapshot(['old stable answer',''],2),base),'')

    def test_new_model_turn_or_changed_turn_after_user_send_is_accepted(self):
        base=snapshot(['old answer'])
        self.assertEqual(self.agent._fresh_response(snapshot(['old answer','new answer'],2),base),'new answer')
        self.assertEqual(self.agent._fresh_response(snapshot(['new answer'],2),base),'new answer')

    def test_whole_model_turn_is_preferred_over_last_markdown_fragment(self):
        self.page.values['model-response']=['Script 01 full text\nScript 02 full text']
        self.page.values['model-response .markdown']=['Only a footer']
        self.assertIn('Script 01',self.agent._last_response_text())
        self.assertIn('Script 02',self.agent._last_response_text())

    def test_gemini_said_prefix_is_removed_without_discarding_script(self):
        self.assertEqual(self.agent._clean_response('Gemini said\n\nScript 01: valid'),'Script 01: valid')

    def test_real_newlines_in_navigation_are_rejected(self):
        with self.assertRaises(GeminiError):self.agent._check_errors('New chat\nSearch chats\nImages')

    def test_observed_platform_refusal_is_not_an_incomplete_script(self):
        with self.assertRaisesRegex(GeminiError,'无法理解'):
            self.agent._check_errors('我只是一个语言模型，理解不了这个问题，因此没法帮上忙。')

    def test_string_selector_is_not_split_into_characters(self):
        self.agent.sel['response_text']='.assistant-output'
        self.page.values['.assistant-output']=['full current response']
        self.assertEqual(self.agent._last_response_text(),'full current response')

    def test_type_replaces_stale_draft(self):
        self.agent.type_prompt('new instruction')
        self.assertEqual(self.page.draft,'new instruction')
        self.page.keyboard.insert_text.assert_not_called()

    def test_click_without_new_turn_is_not_reported_as_sent(self):
        old=snapshot(['old answer'])
        self.agent._response_snapshot=Mock(return_value=old)
        self.agent._stop_visible=Mock(return_value=False)
        self.agent._capture_diagnostic=Mock()
        with patch.object(module.time,'monotonic',side_effect=lambda:self.page.now):
            with self.assertRaisesRegex(GeminiError,'未确认新消息'):self.agent.send_prompt('new instruction')
        self.agent._capture_diagnostic.assert_called_once()

    def test_disabled_send_control_is_not_bypassed_with_enter(self):
        self.page.send_enabled=False
        self.agent._response_snapshot=Mock(return_value=snapshot([]))
        self.agent._capture_diagnostic=Mock()
        with patch.object(module.time,'monotonic',side_effect=lambda:self.page.now):
            with self.assertRaisesRegex(GeminiError,'未就绪'):self.agent.send_prompt('new instruction')
        self.assertNotIn('send',self.page.clicked)
        self.page.keyboard.press.assert_not_called()

    def test_hidden_first_match_does_not_hide_visible_send_control(self):
        self.page.values['send']=['hidden button','visible button'];self.page.hidden.add(('send',0))
        self.agent._response_snapshot=Mock(side_effect=[snapshot([]),snapshot(['new response'],2)])
        self.agent.send_prompt('new instruction')
        self.assertEqual(self.page.clicked.count('send'),1)
        self.page.keyboard.press.assert_not_called()

    def test_click_timeout_checks_conversation_instead_of_clicking_again(self):
        self.page.send_click_failure=True
        self.agent.sel['send_button']=['send','send']
        self.agent._response_snapshot=Mock(return_value=snapshot([]))
        self.agent._stop_visible=Mock(return_value=False);self.agent._capture_diagnostic=Mock()
        with patch.object(module.time,'monotonic',side_effect=lambda:self.page.now):
            with self.assertRaisesRegex(GeminiError,'未确认新消息'):self.agent.send_prompt('new instruction')
        self.assertEqual(self.page.clicked.count('send'),1)
        self.page.keyboard.press.assert_not_called()

    def test_prompt_draft_is_filled_before_waiting_for_video_upload(self):
        calls=Mock()
        for method in ('open','type_prompt','upload_files','send_prompt','wait_response'):
            mocked=Mock(return_value=script_fixture(1,15) if method=='wait_response' else None)
            setattr(self.agent,method,mocked)
            calls.attach_mock(mocked,method)
        self.agent.generate(['fixture.mp4'],'fixture instruction')
        self.assertEqual([call[0] for call in calls.mock_calls],
                         ['open','type_prompt','upload_files','send_prompt','wait_response'])
        self.assertIn('fixture instruction',self.agent.type_prompt.call_args.args[0])
        self.assertEqual(self.agent.type_prompt.call_args.args,self.agent.send_prompt.call_args.args)

    def test_matching_manual_submission_is_adopted_during_upload_wait(self):
        self.agent._expected_prompt='full prepared prompt'
        self.agent._sent_baseline=snapshot([],users=1,user_texts=['prior message'])
        self.agent._response_snapshot=Mock(return_value=snapshot(
            [],users=2,user_texts=['prior message','full prepared prompt']))
        self.assertTrue(self.agent._check_for_external_submission())
        self.assertTrue(self.agent._external_submission_accepted)
        self.assertTrue(self.agent._submission_attempted)

    def test_ambiguous_manual_submission_is_uncertain(self):
        self.agent._expected_prompt='full prepared prompt'
        self.agent._sent_baseline=snapshot([],users=1,user_texts=['prior message'])
        self.agent._response_snapshot=Mock(return_value=snapshot(
            [],users=2,user_texts=['prior message','different prompt']))
        with self.assertRaisesRegex(module.GeminiSubmissionUncertain,'无法唯一匹配'):
            self.agent._check_for_external_submission()
        self.assertTrue(self.agent._submission_attempted)

    def test_disappeared_draft_without_visible_matching_turn_is_uncertain(self):
        self.agent._expected_prompt='full prepared prompt'
        self.agent._sent_baseline=snapshot([],users=0,user_texts=[])
        self.agent._response_snapshot=Mock(return_value=snapshot([],users=0,user_texts=[]))
        with self.assertRaisesRegex(module.GeminiSubmissionUncertain,'草稿已消失'):
            self.agent._check_for_external_submission()
        self.assertTrue(self.agent._submission_attempted)

    def test_manual_submission_requires_all_expected_attachments(self):
        self.agent._expected_prompt='full prepared prompt'
        self.agent._expected_attachment_count=7
        self.agent._sent_baseline=snapshot([],users=0,user_texts=[])
        for counts in ([], [6], [8]):
            with self.subTest(counts=counts):
                self.agent._response_snapshot=Mock(return_value=snapshot(
                    [],users=1,user_texts=['full prepared prompt'],attachment_counts=counts))
                with self.assertRaisesRegex(module.GeminiSubmissionUncertain,'全部附件'):
                    self.agent._check_for_external_submission()
        self.agent._response_snapshot=Mock(return_value=snapshot(
            [],users=1,user_texts=['full prepared prompt'],attachment_counts=[7]))
        self.assertTrue(self.agent._check_for_external_submission())

    def test_generate_adopts_manual_send_without_calling_send_again(self):
        self.agent.open=Mock()
        self.agent.type_prompt=Mock()
        self.agent.send_prompt=Mock()
        self.agent.wait_response=Mock(return_value=script_fixture(1,15))
        def upload(_):
            self.agent._response_snapshot=Mock(return_value=snapshot(
                [],users=1,user_texts=[self.agent._expected_prompt],attachment_counts=[1]))
            self.assertTrue(self.agent._check_for_external_submission())
        self.agent.upload_files=Mock(side_effect=upload)
        with patch.object(self.agent,'_capture_diagnostic'):
            self.agent.generate(['fixture.mp4'],'fixture instruction')
        self.agent.send_prompt.assert_not_called()
        self.agent.wait_response.assert_called_once_with()

    def test_generate_does_not_allow_retry_after_an_attempted_send(self):
        self.agent.open=Mock()
        def attempted_send(_):
            self.agent._submission_attempted=True
            raise GeminiError('未确认新消息')
        self.agent.send_prompt=Mock(side_effect=attempted_send)
        with self.assertRaisesRegex(module.GeminiSubmissionUncertain,'停止自动重试'):
            self.agent.generate([], 'fixture')
        self.agent.send_prompt.assert_called_once()

    def test_failure_before_send_remains_a_retryable_gemini_error(self):
        self.agent.open=Mock(side_effect=GeminiError('fixture editor not ready'))
        self.agent.send_prompt=Mock()
        with self.assertRaises(GeminiError) as error:
            self.agent.generate([], 'fixture')
        self.assertNotIsInstance(error.exception,module.GeminiSubmissionUncertain)
        self.agent.send_prompt.assert_not_called()

    def test_wait_never_returns_old_stable_response(self):
        old=snapshot(['old answer'])
        self.agent._sent_baseline=old
        self.agent._response_snapshot=Mock(return_value=old)
        self.agent._stop_visible=Mock(return_value=False)
        self.agent._capture_diagnostic=Mock()
        with patch.object(module.time,'monotonic',side_effect=lambda:self.page.now):
            with self.assertRaisesRegex(GeminiError,'本次新的'):self.agent.wait_response()

    def test_fast_new_response_can_finish_without_stop_control(self):
        old=snapshot(['old answer']);new=snapshot(['old answer','fresh full response'],2)
        self.agent._sent_baseline=old
        self.agent._response_snapshot=Mock(side_effect=[old,new,new])
        self.agent._stop_visible=Mock(return_value=False)
        with patch.object(module.time,'monotonic',side_effect=lambda:self.page.now):
            self.assertEqual(self.agent.wait_response(),'fresh full response')

    def test_acknowledged_user_turn_allows_long_thinking_without_stop_control(self):
        self.agent._sent_baseline=snapshot(['old answer'])
        self.agent._response_snapshot=lambda:(snapshot(['old answer'],2) if self.page.now<4
                                             else snapshot(['old answer','late fresh response'],2))
        self.agent._stop_visible=Mock(return_value=False)
        with patch.object(module.time,'monotonic',side_effect=lambda:self.page.now):
            self.assertEqual(self.agent.wait_response(),'late fresh response')

    def test_streaming_timeout_preserves_partial_instead_of_returning_it(self):
        self.agent._sent_baseline=snapshot(['old'])
        self.agent._response_snapshot=Mock(return_value=snapshot(['old','partial new script'],2))
        self.agent._stop_visible=Mock(return_value=True)
        self.agent._capture_diagnostic=Mock()
        with patch.object(module.time,'monotonic',side_effect=lambda:self.page.now):
            with self.assertRaisesRegex(GeminiError,'未在时限内完成'):self.agent.wait_response()
        self.assertEqual(self.agent._last_observed_response,'partial new script')
        self.assertEqual(self.agent._capture_diagnostic.call_args.kwargs['response'],'partial new script')

    def test_failed_duration_validation_persists_raw_response(self):
        self.agent.open=Mock();self.agent.send_prompt=Mock()
        self.agent.wait_response=Mock(return_value='A non-script footer from the interface')
        with self.assertRaisesRegex(GeminiError,'缺少独立脚本标题'):
            self.agent.generate([], 'Script 01=50秒；Script 02=70秒')
        responses=list(Path(self.temp.name).glob('*failed.response.txt'))
        self.assertEqual(len(responses),1)
        self.assertIn('non-script footer',responses[0].read_text('utf-8'))

    def test_complete_fifty_and_seventy_second_timeline_is_accepted(self):
        raw=script_fixture(1,50)+'\n'+script_fixture(2,70)
        self.agent.open=Mock();self.agent.send_prompt=Mock()
        self.agent.wait_response=Mock(return_value=raw)
        self.assertEqual(self.agent.generate([], 'Script 01=50秒；Script 02=70秒'),raw)
        captures=list(Path(self.temp.name).glob('*completed.response.txt'))
        self.assertEqual(captures[0].read_text('utf-8'),raw)

    def test_observed_code_limitation_refusal_stops_without_completed_script(self):
        raw='由于程序代码的局限，我没法办到。'
        self.assertTrue(module.is_refusal_response(raw))
        self.agent.open=Mock();self.agent.send_prompt=Mock()
        self.agent.wait_response=Mock(return_value=raw)
        with self.assertRaisesRegex(module.GeminiRefusalResponse,'明确拒绝.*未发往豆包'):
            self.agent.generate([], '写一个该产品功能场景应用的15秒爆款tiktok短视频脚本')
        self.assertEqual(self.agent.wait_response.call_count,1)
        self.assertEqual(self.agent.send_prompt.call_count,1)
        self.assertEqual(list(Path(self.temp.name).glob('*completed.response.txt')),[])
        self.assertEqual(len(list(Path(self.temp.name).glob('*failed.response.txt'))),1)

    def test_unstructured_nonempty_reply_is_never_a_generated_script(self):
        self.agent.open=Mock();self.agent.send_prompt=Mock()
        for raw in ('好的，请稍等。','这是一个很有创意的产品，可以使用温暖灯光展示。'):
            with self.subTest(raw=raw):
                self.agent.wait_response=Mock(return_value=raw)
                with self.assertRaises(module.GeminiResponseRejected):
                    self.agent.generate([], '请依据素材生成脚本')

    def test_observed_language_model_refusal_is_recognized_for_saved_script_guards(self):
        raw='请恕我爱莫能助，我只是一个语言模型，不能理解或回复你的这个问题。'
        self.assertTrue(module.is_refusal_response(raw))
        self.agent.open=Mock();self.agent.send_prompt=Mock()
        self.agent.wait_response=Mock(return_value=raw)
        with self.assertRaisesRegex(module.GeminiRefusalResponse,'明确拒绝.*未发往豆包'):
            self.agent.generate([], '请写一个该产品功能场景应用的15秒短视频脚本')
        self.agent.send_prompt.assert_called_once()
        self.assertEqual(list(Path(self.temp.name).glob('*completed.response.txt')),[])
        self.assertEqual(len(list(Path(self.temp.name).glob('*failed.response.txt'))),1)
        dialogue=script_fixture(1,15).replace('独立测试片段0。',raw)
        self.assertFalse(module.is_refusal_response(dialogue))

    def test_short_valid_timeline_is_accepted_without_length_threshold(self):
        raw='Script 01：短镜头（总时长5秒）\n总时长：5秒\n00:00-00:05\n画面：产品。\n口播：新品。'
        self.agent.open=Mock();self.agent.send_prompt=Mock()
        self.agent.wait_response=Mock(return_value=raw)
        self.assertEqual(self.agent.generate([], '请写1个5秒的视频脚本'),raw)

    def test_completed_refusals_have_a_specific_retry_class(self):
        for raw in ('由于程序代码的局限，我没法办到。',
                    '请恕我爱莫能助，我只是一个语言模型，不能理解或回复你的这个问题。',
                    '我只是一个语言模型，理解不了这个问题，因此没法帮上忙。',
                    '身为一个语言模型，我没办法提供这方面的帮助。',
                    '很抱歉，作为一个人工智能助手，我无法为你提供这方面的帮助。',
                    '对不起，身为一个AI模型，我不能理解或回复这个请求。',
                    '我是一个语言模型，这超出了我的设计用途。'):
            with self.subTest(raw=raw):
                with self.assertRaises(module.GeminiRefusalResponse) as caught:
                    self.agent._check_errors(raw)
                self.assertFalse(caught.exception.can_retry_same_request)
                dialogue=script_fixture(1,15).replace('独立测试片段0。',raw)
                self.agent._check_errors(dialogue)

    def test_positive_model_self_description_is_not_a_refusal(self):
        for raw in ('作为语言模型我可以帮助你完成脚本。',
                    '身为一个AI助手，我能提供相关帮助。',
                    '作为语言模型，我可以帮助你解释“无法理解”这样的说法。',
                    '作为AI，我不能保证提供的信息完全正确，但我可以帮助你构思。'):
            with self.subTest(raw=raw):
                self.assertFalse(module.is_refusal_response(raw))
                self.agent._check_errors(raw)

    def test_model_self_description_refusal_can_establish_same_chat_retry(self):
        raw='身为一个语言模型，我没办法提供这方面的帮助。'
        paths,message,_=self.refusal_chat([raw,script_fixture(1,15)])
        with self.assertRaises(module.GeminiRefusalResponse):self.agent.generate(paths,message)
        self.assertIsNotNone(self.agent._last_refusal_context)
        self.assertEqual(self.agent.retry_last_refusal(paths,message),script_fixture(1,15))
        self.agent.open.assert_called_once();self.page.goto.assert_called_once()

    def test_complete_invalid_replies_allow_same_chat_retry_independent_of_wording(self):
        replies=('我是一个语言模型，这超出了我的设计用途。',
                 '这个产品不错，可以试着用温馨的灯光展示。',
                 'Script 01：测试（总时长15秒）\n总时长：15秒\n没有时间轴。',
                 script_fixture(1,5),
                 script_fixture(1,15)+'\n'+script_fixture(2,15))
        for raw in replies:
            with self.subTest(raw=raw):
                paths,message,_=self.refusal_chat([raw,script_fixture(1,15)])
                # Eligibility must not depend on recognizing a refusal phrase.
                with patch.object(module,'is_refusal_response',return_value=False):
                    with self.assertRaises(module.GeminiResponseRejected) as caught:
                        self.agent.generate(paths,message)
                    self.assertNotIsInstance(caught.exception,module.GeminiRefusalResponse)
                    self.assertTrue(caught.exception.can_retry_same_request)
                    self.assertIsNotNone(self.agent._last_refusal_context)
                    self.assertEqual(self.agent.retry_last_rejected_response(paths,message),script_fixture(1,15))
                self.agent.open.assert_called_once();self.page.goto.assert_called_once()
                prompts=[call.args[0] for call in self.agent.send_prompt.call_args_list]
                self.assertEqual(len(prompts),2);self.assertEqual(prompts[0],prompts[1])

    def test_a_valid_reply_never_creates_retry_eligibility(self):
        paths,message,_=self.refusal_chat([script_fixture(1,15)])
        self.assertEqual(self.agent.generate(paths,message),script_fixture(1,15))
        self.assertIsNone(self.agent._last_refusal_context)
        with self.assertRaises(module.GeminiSubmissionUncertain):
            self.agent.retry_last_rejected_response(paths,message)
        self.agent.send_prompt.assert_called_once()

    def test_an_empty_model_node_is_not_proof_of_a_complete_invalid_reply(self):
        paths,message,_=self.refusal_chat([''])
        with self.assertRaises(module.GeminiResponseRejected) as caught:
            self.agent.generate(paths,message)
        self.assertFalse(caught.exception.can_retry_same_request)
        self.assertIsNone(self.agent._last_refusal_context)
        with self.assertRaises(module.GeminiSubmissionUncertain):
            self.agent.retry_last_rejected_response(paths,message)
        self.agent.send_prompt.assert_called_once()

    def test_repainted_old_model_turn_cannot_authorize_refusal_retry(self):
        paths,message,state=self.refusal_chat()
        state['models'].append('An older model turn')
        def repaint():
            state['models'][-1]='身为一个语言模型，我没办法提供这方面的帮助。'
            return state['models'][-1]
        self.agent.wait_response=Mock(side_effect=repaint)
        with self.assertRaises(module.GeminiRefusalResponse) as caught:self.agent.generate(paths,message)
        self.assertIsNone(self.agent._last_refusal_context)
        self.assertFalse(caught.exception.can_retry_same_request)
        with self.assertRaises(module.GeminiSubmissionUncertain):self.agent.retry_last_refusal(paths,message)
        self.agent.send_prompt.assert_called_once()

    def test_format_and_duration_errors_are_not_retryable_refusals(self):
        self.agent.open=Mock();self.agent.send_prompt=Mock()
        for raw in ('好的，请稍等。',script_fixture(1,5)):
            with self.subTest(raw=raw):
                self.agent.wait_response=Mock(return_value=raw)
                with self.assertRaises(module.GeminiResponseRejected) as caught:
                    self.agent.generate([], '请写1个15秒的视频脚本')
                self.assertNotIsInstance(caught.exception,module.GeminiRefusalResponse)
                self.assertFalse(caught.exception.can_retry_same_request)
                self.assertIsNone(self.agent._last_refusal_context)
                with self.assertRaises(module.GeminiSubmissionUncertain):
                    self.agent.retry_last_refusal([], '请写1个15秒的视频脚本')

    def test_partial_refusal_during_uncertain_response_is_not_a_completed_refusal(self):
        self.agent.open=Mock()
        def sent(_):
            self.agent._submission_attempted=True
        def incomplete_response():
            self.agent._last_observed_response='由于程序代码的局限，我没法办到。'
            raise module.GeminiSubmissionUncertain('尚未确认回复已完整接收')
        self.agent.send_prompt=Mock(side_effect=sent)
        self.agent.wait_response=Mock(side_effect=incomplete_response)
        with self.assertRaises(module.GeminiSubmissionUncertain) as caught:
            self.agent.generate([], '请写1个15秒的视频脚本')
        self.assertNotIsInstance(caught.exception,module.GeminiRefusalResponse)
        self.agent.send_prompt.assert_called_once()
        self.assertEqual(list(Path(self.temp.name).glob('*completed.response.txt')),[])
        with self.assertRaises(module.GeminiSubmissionUncertain):
            self.agent.retry_last_refusal([], '请写1个15秒的视频脚本')
        self.agent.send_prompt.assert_called_once()

    def test_confirmed_refusal_retry_reuses_page_and_exact_original_inputs(self):
        paths,message,state=self.refusal_chat()
        with self.assertRaises(module.GeminiRefusalResponse):self.agent.generate(paths,message)
        self.assertIsNotNone(self.agent._last_refusal_context)
        self.assertEqual(self.agent.retry_last_refusal(paths,message),script_fixture(1,15))
        self.agent.open.assert_called_once();self.page.goto.assert_called_once()
        prompts=[call.args[0] for call in self.agent.type_prompt.call_args_list]
        self.assertEqual(prompts[0],prompts[1])
        self.assertEqual(prompts[1].count('【自动化脚本输出规范】'),1)
        self.assertEqual([call.args[0] for call in self.agent.upload_files.call_args_list],[paths,paths])
        self.assertEqual(self.agent._sent_baseline['user_count'],1)
        self.assertEqual(self.agent._sent_baseline['model_count'],1)
        old=snapshot([state['models'][0]],1,user_texts=[prompts[0]],attachment_counts=[1])
        self.assertEqual(self.agent._fresh_response(old,self.agent._sent_baseline),'')
        self.assertIsNone(self.agent._last_refusal_context)

    def test_each_completed_refusal_grants_only_one_same_chat_retry(self):
        paths,message,state=self.refusal_chat([
            '由于程序代码的局限，我没法办到。',
            '请恕我爱莫能助，我只是一个语言模型，不能理解或回复你的这个问题。',script_fixture(1,15)])
        with self.assertRaises(module.GeminiRefusalResponse):self.agent.generate(paths,message)
        with self.assertRaises(module.GeminiRefusalResponse):self.agent.retry_last_refusal(paths,message)
        self.assertEqual(self.agent.retry_last_refusal(paths,message),script_fixture(1,15))
        self.agent.open.assert_called_once();self.page.goto.assert_called_once()
        prompts=[call.args[0] for call in self.agent.send_prompt.call_args_list]
        self.assertEqual(len(prompts),3);self.assertEqual(len(set(prompts)),1)
        with self.assertRaises(module.GeminiSubmissionUncertain):self.agent.retry_last_refusal(paths,message)
        self.assertEqual(self.agent.send_prompt.call_count,3)

    def test_same_chat_refusal_retry_stops_after_human_has_resent(self):
        paths,message,state=self.refusal_chat()
        with self.assertRaises(module.GeminiRefusalResponse):self.agent.generate(paths,message)
        state['users'].append(state['users'][0]);state['attachments'].append(1)
        with self.assertRaisesRegex(module.GeminiSubmissionUncertain,'新消息'):
            self.agent.retry_last_refusal(paths,message)
        self.agent.send_prompt.assert_called_once();self.agent.open.assert_called_once()

    def test_same_chat_refusal_retry_preserves_a_human_draft(self):
        paths,message,_=self.refusal_chat()
        with self.assertRaises(module.GeminiRefusalResponse):self.agent.generate(paths,message)
        self.page.draft='A new draft written by the user'
        with self.assertRaisesRegex(module.GeminiSubmissionUncertain,'新草稿'):
            self.agent.retry_last_refusal(paths,message)
        self.assertEqual(self.page.draft,'A new draft written by the user')
        self.agent.send_prompt.assert_called_once()

    def test_same_chat_refusal_retry_rejects_file_content_changes(self):
        paths,message,_=self.refusal_chat()
        with self.assertRaises(module.GeminiRefusalResponse):self.agent.generate(paths,message)
        Path(paths[0]).write_bytes(b'fixture-video-b')  # Same byte length; hashes must still detect it.
        with self.assertRaisesRegex(module.GeminiSubmissionUncertain,'原附件内容'):
            self.agent.retry_last_refusal(paths,message)
        self.agent.send_prompt.assert_called_once()

    def test_same_chat_refusal_retry_rejects_changed_instruction_or_path(self):
        for changed in ('instruction','path'):
            with self.subTest(changed=changed):
                paths,message,_=self.refusal_chat()
                with self.assertRaises(module.GeminiRefusalResponse):self.agent.generate(paths,message)
                retry_paths=paths if changed=='instruction' else (str(Path(self.temp.name)/'other.mp4'),)
                retry_message=message+' changed' if changed=='instruction' else message
                with self.assertRaisesRegex(module.GeminiSubmissionUncertain,'原指令或附件路径'):
                    self.agent.retry_last_refusal(retry_paths,retry_message)
                self.agent.send_prompt.assert_called_once()

    def test_same_chat_refusal_retry_stops_on_changed_conversation(self):
        paths,message,_=self.refusal_chat()
        with self.assertRaises(module.GeminiRefusalResponse):self.agent.generate(paths,message)
        self.page.url='https://gemini.google.com/app/different-conversation'
        with self.assertRaisesRegex(module.GeminiSubmissionUncertain,'会话已变化'):
            self.agent.retry_last_refusal(paths,message)
        self.agent.send_prompt.assert_called_once();self.agent.open.assert_called_once()

    def test_same_chat_refusal_retry_rejects_a_different_page_at_the_same_url(self):
        paths,message,_=self.refusal_chat()
        with self.assertRaises(module.GeminiRefusalResponse):self.agent.generate(paths,message)
        replacement=Page();replacement.url=self.page.url;self.agent.session.page=replacement
        with self.assertRaisesRegex(module.GeminiSubmissionUncertain,'会话已变化'):
            self.agent.retry_last_refusal(paths,message)
        self.agent.send_prompt.assert_called_once();self.agent.open.assert_called_once()

    def test_same_chat_refusal_retry_stops_while_the_page_is_generating(self):
        paths,message,_=self.refusal_chat()
        with self.assertRaises(module.GeminiRefusalResponse):self.agent.generate(paths,message)
        self.agent._stop_visible.return_value=True
        with self.assertRaisesRegex(module.GeminiSubmissionUncertain,'正在回复'):
            self.agent.retry_last_refusal(paths,message)
        self.agent.send_prompt.assert_called_once()

    def test_refusal_retry_requires_accepted_turn_evidence(self):
        self.agent.open=Mock();self.agent.send_prompt=Mock()
        self.agent.wait_response=Mock(return_value='由于程序代码的局限，我没法办到。')
        with self.assertRaises(module.GeminiRefusalResponse):self.agent.generate([], 'fixture instruction')
        self.assertIsNone(self.agent._last_refusal_context)
        with self.assertRaisesRegex(module.GeminiSubmissionUncertain,'完整回复记录'):
            self.agent.retry_last_refusal([], 'fixture instruction')
        self.agent.send_prompt.assert_called_once()

    def test_declared_script_without_timeline_is_rejected_even_without_request_duration(self):
        self.agent.open=Mock();self.agent.send_prompt=Mock()
        self.agent.wait_response=Mock(return_value='Script 01：测试（总时长15秒）\n总时长：15秒\n只有一句话。')
        with self.assertRaisesRegex(module.GeminiResponseRejected,'缺少.*时间戳'):
            self.agent.generate([], '请依据素材生成脚本')

    def test_timestamp_labels_without_content_are_not_a_script(self):
        self.agent.open=Mock();self.agent.send_prompt=Mock()
        self.agent.wait_response=Mock(return_value='Script 01：测试（总时长5秒）\n总时长：5秒\n00:00-00:05\n画面：\n口播：')
        with self.assertRaisesRegex(module.GeminiResponseRejected,'缺少画面或音频正文'):
            self.agent.generate([], '请依据素材生成脚本')

    def test_refusal_as_spoken_dialogue_inside_valid_script_is_not_platform_refusal(self):
        raw=script_fixture(1,5).replace('独立测试片段0。','我没法办到。')
        self.assertFalse(module.is_refusal_response(raw))
        self.agent.open=Mock();self.agent.send_prompt=Mock()
        self.agent.wait_response=Mock(return_value=raw)
        self.assertEqual(self.agent.generate([], '请写一个5秒脚本'),raw)

    def test_natural_product_description_duration_is_not_lost(self):
        message='写一个该产品功能场景应用的15秒爆款tiktok短视频脚本'
        self.assertEqual(module._explicit_script_count(message),1)
        self.assertEqual(module._requested_durations(message,1),[15])
        self.agent.open=Mock();self.agent.send_prompt=Mock()
        self.agent.wait_response=Mock(return_value=script_fixture(1,5))
        with self.assertRaisesRegex(module.GeminiResponseRejected,'总时长不符.*15.*5'):
            self.agent.generate([],message)

    def test_source_duration_and_tool_limit_are_not_output_duration(self):
        self.assertEqual(module._requested_durations('按15秒源视频结构写一个该产品的短视频脚本',1),[])
        self.assertEqual(module._requested_durations('写一个参考15秒源视频的产品脚本',1),[])
        self.assertEqual(module._requested_durations('写3个脚本，豆包单次15秒分段',3),[])
        self.assertEqual(module._requested_durations('写一个50秒脚本\n【本次附加提示词】\n写一个该产品功能场景应用的15秒短视频脚本',1),[15])

    def test_unsupported_fractional_duration_is_not_truncated_to_its_last_digit(self):
        self.assertEqual(module._requested_durations('写一个该产品3.1秒的短视频脚本',1),[])

    def test_explicit_prompt_script_count_is_enforced_without_truncation(self):
        raw=script_fixture(1,50)+'\n'+script_fixture(2,70)
        self.agent.open=Mock();self.agent.send_prompt=Mock();self.agent.wait_response=Mock(return_value=raw)
        with self.assertRaisesRegex(GeminiError,'数量不符.*3个.*2个'):
            self.agent.generate([], '请生成3个短视频脚本，每条包含15秒分段')
        self.assertEqual(self.agent.generate([], '请生成两个短视频脚本'),raw)

    def test_no_explicit_count_preserves_all_generated_scripts(self):
        raw=script_fixture(1,50)+'\n'+script_fixture(2,70)+'\n'+script_fixture(3,50)
        self.agent.open=Mock();self.agent.send_prompt=Mock();self.agent.wait_response=Mock(return_value=raw)
        self.assertEqual(self.agent.generate([], '按素材生成视频脚本，每段15秒'),raw)

    def test_supplemental_count_overrides_fixed_prompt_and_ambiguous_is_not_guessed(self):
        self.assertEqual(module._explicit_script_count('生成5个脚本\n【本次附加提示词】\n请再写2个TIKTOK爆款短视频脚本'),2)
        self.assertIsNone(module._explicit_script_count('生成2个脚本，也可以写3个脚本'))
        self.assertIsNone(module._explicit_script_count('将脚本分成2条15秒视频，包含3个人物'))
        self.assertEqual(module._explicit_script_count('一共生成3个视频脚本'),3)
        self.assertEqual(module._explicit_script_count('生成5个脚本\n【本次附加提示词】\n生成1个50秒和一个70秒的视频'),2)
        self.assertEqual(module._requested_durations('生成1个50秒和一个70秒的视频'),[50,70])
        self.assertEqual(module._requested_durations('Script 01=20秒\n【本次附加提示词】\nScript 01=50秒；Script 02=70秒'),[50,70])
        self.assertEqual(module._requested_durations('生成1个50秒和一个70秒的视频',1),[])

    def test_supplemental_single_fifteen_seconds_overrides_fixed_long_versions(self):
        message=('帮我再写1个50秒和一个70秒的TIKTOK爆款短视频脚本'
                 '\n【本次附加提示词】\n帮我再写1个15秒的TIKTOK爆款短视频脚本')
        self.assertEqual(module._explicit_script_count(message),1)
        self.assertEqual(module._requested_durations(message,1),[15])
        self.agent.open=Mock();self.agent.send_prompt=Mock()
        self.agent.wait_response=Mock(return_value=script_fixture(1,15))
        self.agent.generate([],message)
        instructions=self.agent.send_prompt.call_args.args[0].split('【自动化脚本输出规范】',1)[1]
        self.assertIn('本次最终只输出1个脚本',instructions)
        self.assertIn('本次各脚本总时长按顺序为15秒',instructions)
        self.assertNotIn('50秒',instructions)
        self.assertNotIn('70秒',instructions)

    def test_single_requested_duration_is_checked_not_only_script_count(self):
        self.agent.open=Mock();self.agent.send_prompt=Mock()
        self.agent.wait_response=Mock(return_value=script_fixture(1,50))
        with self.assertRaisesRegex(GeminiError,'总时长不符.*15.*50'):
            self.agent.generate([], '帮我再写1个15秒的视频脚本')

    def test_one_of_each_duration_is_two_scripts_not_one_total(self):
        message='帮我再写1个50秒和一个70秒的TIKTOK爆款短视频脚本'
        self.assertEqual(module._explicit_script_count(message),2)
        self.assertEqual(module._requested_durations(message,2),[50,70])

    def test_no_duration_request_does_not_add_arbitrary_long_versions(self):
        self.agent.open=Mock();self.agent.send_prompt=Mock()
        self.agent.wait_response=Mock(return_value=script_fixture(1,15))
        self.agent.generate([], '请依据素材生成脚本')
        instructions=self.agent.send_prompt.call_args.args[0].split('【自动化脚本输出规范】',1)[1]
        self.assertNotIn('50秒',instructions)
        self.assertNotIn('70秒',instructions)

    def test_repeated_fifteen_second_request_overrides_old_durations(self):
        message=('生成1个50秒和一个70秒的视频\n【本次附加提示词】\n'
                 '帮我再写4个15秒的TIKTOK爆款短视频脚本')
        self.assertEqual(module._explicit_script_count(message),4)
        self.assertEqual(module._requested_durations(message,4),[15]*4)
        self.assertEqual(module._requested_durations('写四个15秒的视频脚本'),[15]*4)
        self.assertEqual(module._requested_durations(message,3),[])
        self.agent.open=Mock();self.agent.send_prompt=Mock()
        raw='\n'.join(script_fixture(index,15) for index in range(1,5))
        self.agent.wait_response=Mock(return_value=raw)
        self.assertEqual(self.agent.generate([],message),raw)
        self.agent.wait_response=Mock(return_value=raw.replace(script_fixture(4,15),script_fixture(4,50)))
        with self.assertRaisesRegex(GeminiError,'总时长不符'):
            self.agent.generate([],message)

    def test_segment_duration_is_not_mistaken_for_total_duration(self):
        self.assertEqual(module._requested_durations('生成3个脚本，每条包含15秒分段',3),[])

    def test_natural_language_fifty_and_seventy_seconds_are_validated(self):
        raw=script_fixture(1,50)+'\n'+script_fixture(2,70)
        self.agent.open=Mock();self.agent.send_prompt=Mock();self.agent.wait_response=Mock(return_value=raw)
        self.assertEqual(self.agent.generate([], '生成1个50秒和一个70秒的视频'),raw)
        with self.assertRaisesRegex(GeminiError,'总时长不符'):
            self.agent.generate([], '生成1个50秒和一个60秒的视频')

    def test_declared_duration_without_timeline_is_rejected(self):
        raw='Script 01：测试（总时长50秒）\n只有一句话。\nScript 02：测试（总时长70秒）\n只有一句话。'
        self.agent.open=Mock();self.agent.send_prompt=Mock()
        self.agent.wait_response=Mock(return_value=raw)
        with self.assertRaisesRegex(GeminiError,'缺少.*时间戳'):
            self.agent.generate([], 'Script 01=50秒；Script 02=70秒')

    def test_malformed_timestamp_is_a_diagnosable_gemini_error(self):
        raw=(script_fixture(1,50)+'\n'+script_fixture(2,70)).replace('00:00 - 00:05','00:00 - 00:75',1)
        self.agent.open=Mock();self.agent.send_prompt=Mock()
        self.agent.wait_response=Mock(return_value=raw)
        with self.assertRaisesRegex(GeminiError,'时间轴不完整'):
            self.agent.generate([], 'Script 01=50秒；Script 02=70秒')

    def test_timeline_gap_is_rejected_even_when_declared_duration_matches(self):
        raw=(script_fixture(1,50)+'\n'+script_fixture(2,70)).replace('00:15 - 00:20','00:16 - 00:20',1)
        self.agent.open=Mock();self.agent.send_prompt=Mock()
        self.agent.wait_response=Mock(return_value=raw)
        with self.assertRaisesRegex(GeminiError,'时间轴不完整'):
            self.agent.generate([], 'Script 01=50秒；Script 02=70秒')

    def test_diagnostic_saves_raw_and_screenshot_without_url_query(self):
        prefix=self.agent._capture_diagnostic('unit',response='verbatim response',error='test error')
        root=Path(self.temp.name)
        data=json.loads((root/(prefix+'.json')).read_text('utf-8'))
        self.assertNotIn('secret',data['url'])
        self.assertEqual((root/(prefix+'.response.txt')).read_text('utf-8'),'verbatim response')
        self.assertTrue((root/(prefix+'.png')).is_file())

    def test_diagnostic_body_failure_does_not_prevent_screenshot(self):
        self.page.values.pop('body')
        prefix=self.agent._capture_diagnostic('unit_body_failure',response='raw response')
        self.assertTrue((Path(self.temp.name)/(prefix+'.png')).is_file())

    def test_diagnostic_write_failure_does_not_hide_runtime_error(self):
        with patch.object(Path,'mkdir',side_effect=PermissionError('unit fake denial')):
            self.agent._capture_diagnostic('unit_unwritable',error='original runtime error')

    def test_missing_editor_never_clicks_agreement_or_continue(self):
        self.page.editor_unavailable=True
        self.agent._capture_diagnostic=Mock()
        with self.assertRaises(GeminiNotLoggedIn):self.agent.open()
        self.page.get_by_role.assert_not_called()


if __name__=='__main__':unittest.main(verbosity=2)
