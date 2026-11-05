import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from doubao_automation.doubao_batch import BatchGenerationMixin,batch_prompt,choose_reply,quota_remaining


def asset(url,index=0):
    return {'url':url,'stable':url.split('?')[0],'video_index':index,
            'message_id':'fixture-'+hashlib.sha256(url.split('?')[0].encode()).hexdigest()[:12],
            'message_ordinal':0,'duration':15,'ownership_verified':True,
            'owner_user_message_id':'fixture-owner'}


class Page:
    url='https://www.doubao.com/chat/123456789'
    def __init__(self):self.clock=0
    def locator(self,_):return self
    def inner_text(self,**kwargs):return '视频生成'
    def wait_for_timeout(self,_):self.clock+=10
    def goto(self,*args,**kwargs):pass


class Agent(BatchGenerationMixin):
    def __init__(self,steps,replies=None):
        self.page=Page();self.name='test';self.dcfg={'generation_timeout_minutes':1,'poll_interval_seconds':1}
        self.quota_patterns=[];self.failure_patterns=[];self.steps=iter(steps);self.last=[]
        self.replies=iter(replies or ['生成中']*20);self.sent=[]
    def open_chat(self):pass
    def new_chat(self):pass
    def configure_video_mode(self):pass
    def upload_images(self,_):pass
    def _assert_logged_in(self):pass
    def _handle_dialogs(self):pass
    def type_and_send(self,text):self.sent.append(text)
    def _assistant_text(self):
        self._last_reply_source='latest_assistant'
        return next(self.replies,'生成中') if self.sent else '欢迎'
    def _batch_assets(self):
        self.last=next(self.steps,self.last)
        if isinstance(self.last,tuple):
            self._batch_cards_snapshot=self.last[1];return self.last[0]
        return self.last
    def _download_batch_asset(self,ref,dest):Path(dest).write_bytes(b'x'*2000);return True


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.tasks=[{'id':f's#seg{i}','seg_index':i,'seg_total':4,'target_seconds':15,'est_seconds':15,
                     'start_seconds':15*i,'end_seconds':15*(i+1),'prompt':f'本段原文{i}'} for i in range(2)]
        self.dests={t['id']:Path(self.tmp.name)/f'{i}.mp4' for i,t in enumerate(self.tasks)}
    def tearDown(self):self.tmp.cleanup()
    def run_agent(self,agent,context=None):
        generated=[];submitted=[]
        with patch('doubao_automation.doubao_batch.time.monotonic',side_effect=lambda:agent.page.clock):
            result=agent.run_batch(self.tasks,[],self.dests,{'total_seconds':50,**(context or {})},
                                   lambda url,baseline:submitted.append((url,baseline)),
                                   lambda tid,ref:generated.append((tid,ref)))
        return result,generated,submitted
    def test_fresh_batch_filters_empty_refs_and_bills_each_actual_asset_once(self):
        a,b=asset('https://cdn/one.mp4'),asset('https://cdn/two.mp4',1)
        agent=Agent([[],[a],[a,b]])
        result,generated,_=self.run_agent(agent,{'remote_refs':{t['id']:{} for t in self.tasks}})
        self.assertEqual(set(result['videos']),set(self.dests))
        self.assertEqual([x[0] for x in generated],list(self.dests))
        self.assertEqual(len(agent.sent),1)
    def test_historical_video_is_excluded_from_new_batch(self):
        old=asset('https://cdn/old.mp4');a=asset('https://cdn/new1.mp4',1);b=asset('https://cdn/new2.mp4',2)
        result,generated,_=self.run_agent(Agent([[old],[old,a,b]]))
        self.assertEqual(len(generated),2)
        self.assertTrue(all(x[1]['url']!='https://cdn/old.mp4' for x in generated))
        self.assertFalse(result['unresolved'])
    def test_ambiguous_resumed_history_never_generates_or_bills_old_video(self):
        old=asset('blob:old');changed=asset('blob:changed')
        agent=Agent([[changed]])
        result,generated,_=self.run_agent(agent,{'resume_only':True,'chat_url':agent.page.url,
          'baseline':{'keys':[hashlib.sha256(b'blob:old').hexdigest()],'assets':[old]}})
        self.assertEqual(result['error_kind'],'needs_user')
        self.assertFalse(generated);self.assertFalse(agent.sent)
    def test_resume_only_downloads_and_reconciles_partial_result(self):
        a,b=asset('https://cdn/one.mp4'),asset('https://cdn/two.mp4',1)
        ref=dict(a,key=hashlib.sha256(b'https://cdn/one.mp4').hexdigest())
        agent=Agent([[a,b]])
        result,generated,_=self.run_agent(agent,{'resume_only':True,'chat_url':agent.page.url,
          'baseline':{'keys':[]},'remote_refs':{self.tasks[0]['id']:ref}})
        self.assertEqual(len(generated),2);self.assertFalse(result['unresolved']);self.assertFalse(agent.sent)
    def test_partial_unknown_request_never_automatically_resubmits(self):
        a=asset('https://cdn/one.mp4')
        agent=Agent([[],[a]])
        result,generated,_=self.run_agent(agent)
        self.assertEqual(result['unresolved'],[self.tasks[1]['id']])
        self.assertEqual(len(generated),1);self.assertEqual(len(agent.sent),1)
        self.assertFalse(result['continuation_safe'])

    def test_resumed_history_waits_for_all_baseline_cards_before_identity_check(self):
        old_a,old_b=asset('https://cdn/old-a.mp4'),asset('https://cdn/old-b.mp4',1)
        a,b=asset('https://cdn/new-a.mp4',2),asset('https://cdn/new-b.mp4',3)
        agent=Agent([[old_a],[old_a,old_b,a,b]])
        result,generated,_=self.run_agent(agent,{'resume_only':True,'chat_url':agent.page.url,
          'baseline':{'keys':[agent._key_asset(v) for v in (old_a,old_b)],'assets':[old_a,old_b]}})
        self.assertFalse(result['unresolved']);self.assertFalse(agent.sent)
        self.assertEqual([ref['url'] for _,ref in generated],[a['url'],b['url']])
    def test_choice_variants_are_bounded_to_one_control_reply(self):
        agent=Agent([[]],['方案一压缩为15秒，方案二分成两段15秒，请选择',
                          '你希望压缩，还是分成两段？']*8)
        self.run_agent(agent)
        self.assertEqual(len(agent.sent),2)
        self.assertIn('选择分段',agent.sent[1]);self.assertNotIn('【完整剧本',agent.sent[1])
    def test_explicit_single_limit_sends_only_missing_second_segment(self):
        a,b=asset('https://cdn/one.mp4'),asset('https://cdn/two.mp4',1)
        agent=Agent([[],[],[a],[a,b]],['一次只能生成一条视频','第一条已经生成完成'])
        result,generated,_=self.run_agent(agent)
        self.assertEqual(len(generated),2);self.assertEqual(len(agent.sent),3)
        self.assertIn('视频2/4',agent.sent[-1]);self.assertFalse(result['unresolved'])
    def test_prompt_preserves_full_length_and_only_two_windows(self):
        prompt=batch_prompt(self.tasks,{'total_seconds':50,'script_text':'完整50秒原文'})
        self.assertIn('50秒',prompt);self.assertIn('0–15',prompt);self.assertIn('15–30',prompt)
        with self.assertRaises(ValueError):batch_prompt(self.tasks*2,{'total_seconds':50})
    def test_parser_does_not_infer_quota_from_unrelated_daily_chat(self):
        self.assertEqual(quota_remaining('今日视频生成剩余2次'),2)
        self.assertIsNone(quota_remaining('每天4次，每次最多2条'))
        self.assertEqual(choose_reply('方案1压缩为15秒，方案2分成两段，请选择'),'split')

    def test_failed_first_card_does_not_steal_successful_second_slot(self):
        failed={'kind':'card','status':'failed','card_position':0}
        ready={'kind':'card','status':'ready','card_position':1,'stable':'card:msg:1','url':'',
               'card_message_id':'msg','card_ordinal':1,'receive_index':0,
               'ownership_verified':True,'owner_user_message_id':'fixture-owner'}
        agent=Agent([([],[]),([ready],[failed,ready])],['你的内容已生成完毕，其中1个视频生成失败，失败视频的生成额度未扣除。'])
        result,generated,_=self.run_agent(agent)
        self.assertEqual([tid for tid,_ in generated],[self.tasks[1]['id']])
        self.assertEqual(set(result['videos']),{self.tasks[1]['id']})
        self.assertEqual(result['rejected_task_ids'],[self.tasks[0]['id']])
        self.assertTrue(result['rejection_evidence']['quota_not_charged'])

    def test_resume_filtered_request_keeps_original_card_slot_order(self):
        ready={'kind':'card','status':'ready','card_position':1,'stable':'card:msg:1','url':'',
               'card_message_id':'msg','card_ordinal':1,'receive_index':0,
               'ownership_verified':True,'owner_user_message_id':'fixture-owner'}
        failed={'kind':'card','status':'failed','card_position':0}
        agent=Agent([([ready],[failed,ready])])
        second=self.tasks[1];self.tasks=[second]
        result,generated,_=self.run_agent(agent,{'resume_only':True,'chat_url':agent.page.url,
                         'baseline':{'keys':[]},'original_task_ids':['s#seg0','s#seg1']})
        self.assertEqual([tid for tid,_ in generated],['s#seg1'])
        self.assertEqual(set(result['videos']),{'s#seg1'});self.assertFalse(agent.sent)


if __name__=='__main__':unittest.main()
