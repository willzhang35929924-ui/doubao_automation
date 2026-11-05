"""Offline authorization accounting and refusal-plan guards; all state is synthetic."""
from copy import deepcopy
from contextlib import ExitStack, redirect_stdout
import hashlib
import io
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import Mock, patch

from doubao_automation.account_manager import AccountManager
from doubao_automation import orchestrator as module
from doubao_automation.orchestrator import Pipeline
from doubao_automation.state import JsonStore


REFUSAL='由于程序代码的局限，我没法办到。'
PROMPT='Fixture complete original prompt\nwith a second line.'
MARKER='【自动化任务标记：fixture-request】'


def baseline():
    return {'ownership': {'prompt': PROMPT, 'marker': MARKER}, 'card_count': 0}


def evidence(rid):
    digest=lambda text:hashlib.sha256(re.sub(r'\s+','',text).encode()).hexdigest()
    return dict(kind='material_authorization',request_id=rid,
        prompt_sha256=digest(PROMPT),marker_sha256=digest(MARKER),
        **{key:True for key in ('dialog_visible','draft_matches','marker_matches',
           'no_user_messages','no_cards','fresh_route','before_accept_proven')})


class ApprovalAccountingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='approval-accounting-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        for key,folder in (('STATE_DIR','state'),('VIDEOS_DIR','videos'),
                           ('SCRIPTS_DIR','scripts'),('FRAMES_DIR','frames')):
            path=self.root/folder;path.mkdir()
            self.stack.enter_context(patch.object(module,key,path))
        self.stack.enter_context(patch.object(module,'ensure_dirs'))
        self.cfg={'accounts':{'doubao':[{'name':'a','profile':'fixture','enabled':True}]},
                  'video':{'auto_concat':False}}
        self.pipeline=Pipeline(self.cfg)
        self.manager=self.pipeline.accounts
        self.pipeline._task_images=Mock(return_value=[])
        self.pipeline._assemble_all=Mock()

    def seed(self,text='Complete original script for a local fixture.'):
        with self.pipeline.store.txn() as d:
            d.update(scripts=[{'id':'script','text':text,'keyword':'fixture'}],
                products=[{'keyword':'fixture','name':'Fixture product','images':[]}],
                tasks=[dict(id='script#seg0',script_id='script',seg_index=0,seg_total=1,
                    target_seconds=15,est_seconds=15,start_seconds=0,end_seconds=15,
                    prompt='fixture scene',narration='fixture narration',status='pending',
                    account='',request_id='',video='',raw_video='',remote_ref={},attempts=0)],
                requests={},assemblies={})
        return self.pipeline._claim_batch('a')

    def submitted(self,rid='fixture-request'):
        self.manager.reserve('a',['fixture-task'],rid)
        self.manager.mark_submitted('a',rid,baseline=baseline())
        self.manager.mark_unknown('a',rid)
        return rid

    def test_proven_block_keeps_exact_slot_and_survives_manager_restart(self):
        rid=self.submitted()
        self.assertEqual(self.manager.quota('a')['unknown'],1)
        self.manager.mark_awaiting_approval('a',rid,evidence(rid))
        q=self.manager.quota('a')
        self.assertEqual((q['used'],q['reserved'],q['unknown'],q['remaining']),(0,1,0,3))
        self.assertEqual(q['awaiting_approval'],1)
        self.assertFalse(self.manager.request('a',rid)['submitted'])
        self.assertIn('授权',q['blocked_reason'])
        with self.assertRaisesRegex(ValueError,'等待授权'):
            self.manager.reserve('a',['another-task'],'another-request')
        restarted=AccountManager(self.manager.store,['a'])
        self.assertEqual(restarted.quota('a')['awaiting_approval'],1)

    def test_incomplete_or_mismatched_evidence_never_releases_uncertainty(self):
        rid=self.submitted();original=self.manager.store.snapshot()
        for key,value in (('draft_matches',False),('fresh_route',False),
                          ('request_id','wrong-request'),('marker_sha256','0'*64),
                          ('prompt_sha256','0'*64),('kind','platform_terms')):
            with self.subTest(key=key):
                proof=evidence(rid);proof[key]=value
                with self.assertRaises(ValueError):
                    self.manager.mark_awaiting_approval('a',rid,proof)
                self.assertEqual(self.manager.store.snapshot(),original)

    def test_accepted_turn_or_generated_asset_cannot_be_downgraded(self):
        for accepted in ('ownership_acknowledged','user_message_id','original_anchor_proof','remote_ref'):
            with self.subTest(accepted=accepted):
                rid='request-'+accepted;tid='task-'+accepted
                self.manager.reserve('a',[tid],rid)
                b=baseline()
                if accepted=='ownership_acknowledged':b[accepted]=True
                elif accepted!='remote_ref':b['ownership'][accepted]='fixture-proof'
                self.manager.mark_submitted('a',rid,baseline=b)
                if accepted=='remote_ref':
                    self.manager.record_generated('a',tid,request_id=rid,remote_ref={'key':'fixture-asset'})
                old=self.manager.request('a',rid)
                with self.assertRaises(ValueError):
                    self.manager.mark_awaiting_approval('a',rid,evidence(rid))
                self.assertEqual(self.manager.request('a',rid),old)
                # Keep subtests independent without modifying any real ledger.
                self.manager=AccountManager(JsonStore(self.root/(accepted+'.json')),['a'])

    def approval_agent(self,proven=True):
        def run(tasks,images,dests,context,on_submitted,on_generated):
            on_submitted('',baseline())
            proof=evidence(context['request_id'])
            if not proven:proof['draft_matches']=False;proof['before_accept_proven']=False
            return dict(videos={},remote_refs={},submitted=not proven,chat_url='',
                        error_kind='user_approval',error='等待素材授权',
                        approval_block=proof,continuation_safe=proven)
        return Mock(run_batch=Mock(side_effect=run))

    def test_proven_result_does_not_get_overridden_by_preclick_submitted_ledger(self):
        batch=self.seed();rid=batch[0]['request_id']
        self.assertFalse(self.pipeline._execute_batch(self.approval_agent(),'a',batch))
        s=self.pipeline.store.snapshot()
        self.assertEqual(s['requests'][rid]['phase'],'awaiting_user_approval')
        self.assertFalse(s['requests'][rid]['submitted'])
        self.assertEqual(s['tasks'][0]['status'],'needs_user')
        self.assertEqual(self.manager.quota('a')['unknown'],0)
        self.assertEqual(self.manager.quota('a')['reserved'],1)
        self.pipeline._attempted_requests=set()
        self.pipeline._recover_state()
        self.assertEqual(self.pipeline.store.snapshot()['tasks'][0]['status'],'needs_user')
        self.assertIsNone(self.pipeline._claim_batch('a'))

    def test_ambiguous_approval_still_has_unknown_slot_and_reason(self):
        batch=self.seed();rid=batch[0]['request_id']
        self.assertFalse(self.pipeline._execute_batch(self.approval_agent(False),'a',batch))
        s=self.pipeline.store.snapshot()
        self.assertEqual(s['requests'][rid]['phase'],'unknown')
        self.assertEqual(s['requests'][rid]['error_kind'],'user_approval')
        self.assertEqual(self.manager.quota('a')['unknown'],1)
        self.assertEqual(s['tasks'][0]['status'],'needs_user')
        self.assertIn('授权',s['tasks'][0]['error'])

    def test_approval_report_does_not_claim_next_day_will_auto_resume(self):
        batch=self.seed()
        self.pipeline._execute_batch(self.approval_agent(),'a',batch)
        output=io.StringIO()
        with redirect_stdout(output):self.pipeline.report()
        self.assertIn('等待平台授权或核对',output.getvalue())
        self.assertNotIn('次日',output.getvalue())
        self.assertNotIn('自动续接',output.getvalue())

    def test_saved_refusal_cannot_be_planned_or_claimed_for_new_send(self):
        self.seed()
        with self.pipeline.store.txn() as d:
            d['scripts'][0]['text']=REFUSAL;d['tasks']=[];d['requests']={}
        with self.assertRaisesRegex(ValueError,'拒答'):
            self.pipeline._stage_plan_locked()
        # A previously reserved old plan is checked immediately before images/send too.
        self.pipeline=Pipeline(self.cfg);self.manager=self.pipeline.accounts
        self.pipeline._task_images=Mock(return_value=[])
        # Release the synthetic orphan slot from the separate first scenario.
        with self.manager.store.txn() as d:d.clear()
        batch=self.seed();rid=batch[0]['request_id']
        with self.pipeline.store.txn() as d:d['scripts'][0]['text']=REFUSAL
        agent=Mock()
        self.assertFalse(self.pipeline._execute_batch(agent,'a',batch))
        agent.run_batch.assert_not_called();self.pipeline._task_images.assert_not_called()
        self.assertEqual(self.pipeline.store.snapshot()['tasks'][0]['status'],'failed')
        self.assertEqual(self.pipeline.store.snapshot()['requests'][rid]['phase'],'cancelled')
        self.assertEqual(self.manager.quota('a')['reserved'],0)

    def test_already_accepted_refusal_plan_can_recover_download_without_new_send(self):
        batch=self.seed();rid=batch[0]['request_id']
        self.manager.mark_submitted('a',rid,chat_url='https://www.doubao.com/chat/123',baseline=baseline())
        with self.pipeline.store.txn() as d:
            d['scripts'][0]['text']=REFUSAL
            d['requests'][rid].update(submitted=True,chat_url='https://www.doubao.com/chat/123')
        raw=self.root/'recovered.raw.mp4';raw.write_bytes(b'local fixture')
        ref={'key':'fixture-card','ownership_verified':True,'card_message_id':'fixture-card'}
        def recover(tasks,images,dests,context,on_submitted,on_generated):
            self.assertTrue(context['resume_only']);self.assertEqual(images,[])
            return dict(videos={batch[0]['id']:str(raw)},remote_refs={batch[0]['id']:ref},
                        submitted=True,chat_url=context['chat_url'],error_kind='',error='')
        self.pipeline._finalize_local=lambda task,path:self.pipeline._finish(task['id'],status='done',video=str(path))
        self.assertTrue(self.pipeline._execute_batch(Mock(run_batch=Mock(side_effect=recover)),'a',batch))
        self.pipeline._task_images.assert_not_called()
        self.assertEqual(self.pipeline.store.snapshot()['tasks'][0]['status'],'done')
        self.assertEqual(self.manager.quota('a')['used'],1)


if __name__=='__main__':unittest.main()
