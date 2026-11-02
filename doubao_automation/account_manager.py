"""账号状态与逐条视频额度账本；登录和新任务都不能重置额度。"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from .state import JsonStore

AVAILABLE='available'
BUSY='busy'
EXHAUSTED='exhausted'
NEEDS_LOGIN='needs_login'
DISABLED='disabled'
CHINA=timezone(timedelta(hours=8),'Asia/Shanghai')


class AccountManager:
    def __init__(self, store: JsonStore, names: list, daily_limit=4, clock=None):
        self.store,self.names=store,list(names)
        self.daily_limit=int(daily_limit)
        self.clock=clock or (lambda: datetime.now(timezone.utc))
        with self.store.txn() as data:
            for name in self.names:
                acc=data.setdefault('accounts',{}).setdefault(name,dict(status=AVAILABLE,note='',date=''))
                self._normalize(acc)

    def _today(self):
        value=self.clock()
        if value.tzinfo is None:value=value.replace(tzinfo=timezone.utc)
        return value.astimezone(CHINA).date().isoformat()

    def _account(self,data,name):
        if name not in self.names:raise ValueError('账号未启用或不在当前账号列表中')
        acc=data.setdefault('accounts',{}).setdefault(name,dict(status=AVAILABLE,note='',date=''))
        self._normalize(acc)
        return acc

    def _normalize(self,acc):
        if acc.get('status')==EXHAUSTED and acc.get('date')!=self._today():
            acc.update(status=AVAILABLE,note='',date='')
        if (acc.get('status')==EXHAUSTED and acc.get('date')==self._today()
                and self._today() not in acc.get('quota_days',{})):
            self._day(acc).update(platform_exhausted=True,observed_remaining=0,estimated=False)

    def _day(self,acc,day=None):
        return acc.setdefault('quota_days',{}).setdefault(day or self._today(),dict(
            limit=self.daily_limit,baseline_used=0,estimated=True,entries={},platform_exhausted=False))

    @staticmethod
    def _entries(acc):
        for day,ledger in acc.get('quota_days',{}).items():
            for key,entry in ledger.get('entries',{}).items():yield day,key,entry

    def _public(self,acc):
        today=self._today();day=acc.get('quota_days',{}).get(today,{})
        limit=int(day.get('limit',self.daily_limit))
        used=int(day.get('baseline_used',0))+sum(e.get('state')=='charged' for d,_,e in self._entries(acc) if d==today)
        reserved=sum(e.get('state')=='reserved' for _,_,e in self._entries(acc))
        unknown=sum(e.get('state') in ('submitted','unknown') for _,_,e in self._entries(acc))
        remaining=max(0,limit-used-reserved-unknown);reason='';status=acc.get('status',AVAILABLE)
        if status==DISABLED:reason='账号已禁用'
        elif status==NEEDS_LOGIN:reason='需要重新登录'
        elif day.get('platform_exhausted') or (status==EXHAUSTED and acc.get('date')==today):
            remaining,reason=0,'平台提示今日额度已耗尽'
        elif unknown:reason='已提交产物待核对；仅可恢复原请求'
        elif remaining==0:reason='今日额度已用完或已预留'
        return dict(date=today,limit=limit,used=used,reserved=reserved,unknown=unknown,
                    remaining=remaining,estimated=bool(day.get('estimated',True)),blocked_reason=reason)

    def quota(self,name):return self._public(self.store.snapshot().get('accounts',{}).get(name,{}))

    def status(self,name):
        acc=self.store.snapshot().get('accounts',{}).get(name,{})
        # A long-lived manager may be read across Beijing midnight without a
        # preceding reset_daily() call. Normalize this snapshot before reporting.
        self._normalize(acc)
        result={k:deepcopy(v) for k,v in acc.items() if k not in ('quota_days','quota_requests')}
        result['quota']=self._public(acc)
        return result

    def _set(self,name,status,note='',mark_date=False):
        with self.store.txn() as data:
            acc=self._account(data,name);acc.update(status=status,note=note)
            if mark_date:acc['date']=self._today()
            elif status==AVAILABLE:acc['date']=''

    def mark_available_after_login(self,name):
        with self.store.txn() as data:
            acc=self._account(data,name)
            if acc.get('status')==DISABLED:return
            q=self._public(acc)
            exhausted=self._day(acc).get('platform_exhausted') or q['used']>=q['limit']
            acc.update(status=EXHAUSTED if exhausted else AVAILABLE,date=self._today() if exhausted else '',
                       note='今日额度已耗尽' if exhausted else '登录已验证')

    def reserve(self,name,task_ids,request_id):
        task_ids=[str(t) for t in task_ids]
        if len(set(task_ids))!=len(task_ids):raise ValueError('批次内的视频任务不能重复')
        if not request_id or not 1<=len(task_ids)<=2:raise ValueError('一次请求必须包含1至2个独立视频任务')
        with self.store.txn() as data:
            acc=self._account(data,name);requests=acc.setdefault('quota_requests',{})
            existing=requests.get(request_id)
            if existing:
                if existing['task_ids']!=task_ids:raise ValueError('同一请求不能更换视频任务')
                if any(e.get('state') in ('cancelled','rejected') for _,_,e in self._entries(acc) if e.get('request_id')==request_id):
                    raise ValueError('已取消的请求不能复用，请使用新的请求编号')
                return deepcopy(existing)
            if acc.get('status') not in (AVAILABLE,BUSY):raise ValueError('账号当前不可生成视频')
            for other in data.get('accounts',{}).values():
                if any(e.get('task_id') in task_ids and e.get('state') not in ('cancelled','rejected') for _,_,e in self._entries(other)):
                    raise ValueError('视频任务已有额度记录，应恢复原请求而不是再次提交')
            quota=self._public(acc)
            if quota['unknown']:raise ValueError('账号有未决提交，仅可恢复原请求')
            if quota['remaining']<len(task_ids):raise ValueError('账号剩余额度不足，不能部分预留此批次')
            today=self._today();entries=self._day(acc)['entries']
            for task_id in task_ids:
                entries[request_id+':'+task_id]=dict(task_id=task_id,request_id=request_id,state='reserved',remote_ref={})
            record=dict(request_id=request_id,date=today,task_ids=task_ids,chat_url='',baseline={},submitted=False)
            requests[request_id]=record
            return deepcopy(record)

    def request(self,name,request_id):
        acc=self.store.snapshot().get('accounts',{}).get(name,{})
        record=deepcopy(acc.get('quota_requests',{}).get(request_id,{}))
        if record:record['entries']={e['task_id']:deepcopy(e) for _,_,e in self._entries(acc) if e.get('request_id')==request_id}
        return record

    def find_task(self,task_id,include_inactive=False):
        inactive=None
        for name,acc in self.store.snapshot().get('accounts',{}).items():
            for day,_,entry in self._entries(acc):
                if entry.get('task_id')==task_id:
                    record=deepcopy(acc.get('quota_requests',{}).get(entry['request_id'],{}))
                    result=dict(account=name,date=day,**deepcopy(entry),request=record)
                    if entry.get('state') not in ('cancelled','rejected'):return result
                    inactive=result
        return inactive if include_inactive else None

    def mark_submitted(self,name,request_id,chat_url='',baseline=None):
        with self.store.txn() as data:
            acc=self._account(data,name);request=acc.get('quota_requests',{}).get(request_id)
            if not request:raise ValueError('没有此请求的额度预留')
            if not request.get('submitted'):
                quota=self._public(acc)
                if (self._day(acc).get('platform_exhausted')
                        or quota['used']+quota['reserved']+quota['unknown']>quota['limit']):
                    raise ValueError('平台余额已变化，本批次不能发送，请释放后缩小批次')
                # A reserved-but-unsent request is charged on its actual submission day.
                if request['date']!=self._today():
                    moved=[]
                    for day,key,entry in list(self._entries(acc)):
                        if entry.get('request_id')==request_id and entry.get('state')=='reserved':
                            moved.append((key,entry));del acc['quota_days'][day]['entries'][key]
                    self._day(acc)['entries'].update(moved)
                    request['date']=self._today()
            request['submitted']=True
            if chat_url:request['chat_url']=chat_url
            if baseline is not None:request['baseline']=deepcopy(baseline)
            for _,_,entry in self._entries(acc):
                if entry.get('request_id')==request_id and entry.get('state')=='reserved':entry['state']='submitted'

    def record_generated(self,name,task_id,request_id='',remote_ref=None):
        with self.store.txn() as data:
            acc=self._account(data,name)
            matching=[e for _,_,e in self._entries(acc) if e.get('task_id')==task_id
                      and (not request_id or e.get('request_id')==request_id) and e.get('state') not in ('cancelled','rejected')]
            if len(matching)!=1:raise ValueError('无法唯一确定已生成视频的额度记录')
            matching[0]['state']='charged'
            if remote_ref is not None:matching[0]['remote_ref']=deepcopy(remote_ref)
            request=acc.get('quota_requests',{}).get(matching[0]['request_id'])
            if request is not None:request['submitted']=True

    def mark_unknown(self,name,request_id,task_ids=None,note=''):
        selected=set(task_ids) if task_ids is not None else None
        with self.store.txn() as data:
            acc=self._account(data,name)
            changed=False
            for _,_,entry in self._entries(acc):
                if (entry.get('request_id')==request_id and entry.get('state') in ('reserved','submitted','unknown')
                        and (selected is None or entry['task_id'] in selected)):
                    entry.update(state='unknown',note=note);changed=True
            if changed:
                request=acc.get('quota_requests',{}).get(request_id)
                if request is not None:request['submitted']=True

    def release_unsubmitted(self,name,request_id,task_ids=None):
        selected=set(task_ids) if task_ids is not None else None
        with self.store.txn() as data:
            acc=self._account(data,name)
            for _,_,entry in self._entries(acc):
                if (entry.get('request_id')==request_id and entry.get('state')=='reserved'
                        and (selected is None or entry['task_id'] in selected)):entry['state']='cancelled'

    def release_rejected(self,name,request_id,task_ids,evidence):
        """Release only slots definitively refused by the latest platform response."""
        if (not isinstance(evidence,dict) or evidence.get('kind')!='quota'
                or evidence.get('definitive') is not True or evidence.get('active') is not False
                or evidence.get('source')!='latest_assistant'):
            raise ValueError('只有最新回复明确拒绝且无生成中任务时才能释放已提交预留')
        selected=set(task_ids)
        released=[]
        with self.store.txn() as data:
            acc=self._account(data,name)
            request=acc.get('quota_requests',{}).get(request_id)
            if not request:raise ValueError('没有此请求的额度记录')
            for _,_,entry in self._entries(acc):
                if (entry.get('request_id')==request_id and entry.get('task_id') in selected
                        and entry.get('state') in ('reserved','submitted','unknown','rejected')):
                    entry.update(state='rejected',rejection=deepcopy(evidence))
                    released.append(entry['task_id'])
            request['rejected_task_ids']=sorted(set(request.get('rejected_task_ids',[]))|set(released))
        return released

    def release_failed_refunded(self,name,request_id,task_ids,evidence):
        """Release exact failed-card slots only after an explicit no-charge platform result."""
        failed=evidence.get('failed_task_ids',evidence.get('failed_card_task_ids')) if isinstance(evidence,dict) else None
        if (not isinstance(evidence,dict) or evidence.get('kind')!='generation_failed_refunded'
                or evidence.get('definitive') is not True or evidence.get('active') is not False
                or evidence.get('source')!='latest_assistant' or evidence.get('quota_not_charged') is not True
                or not isinstance(failed,(list,tuple,set)) or not failed
                or any(not isinstance(tid,str) or not tid for tid in failed)):
            raise ValueError('只有失败卡槽位、最新回复明确未扣额度且无生成中任务时才能释放')
        if not isinstance(task_ids,(list,tuple,set)) or any(not isinstance(tid,str) or not tid for tid in task_ids):
            raise ValueError('必须指定确切失败视频任务编号')
        selected=set(task_ids);failed=set(failed)
        if not selected.issubset(failed):raise ValueError('释放槽位必须有对应失败卡证据')
        released=[]
        with self.store.txn() as data:
            acc=self._account(data,name)
            request=acc.get('quota_requests',{}).get(request_id)
            if not request:raise ValueError('没有此请求的额度记录')
            if not (selected|failed).issubset(set(request['task_ids'])):
                raise ValueError('失败卡任务不属于此请求')
            for _,_,entry in self._entries(acc):
                if (entry.get('request_id')==request_id and entry.get('task_id') in selected
                        and entry.get('state') in ('reserved','submitted','unknown','rejected')):
                    entry.update(state='rejected',rejection=deepcopy(evidence))
                    released.append(entry['task_id'])
            request['rejected_task_ids']=sorted(set(request.get('rejected_task_ids',[]))|set(released))
            request['refunded_task_ids']=sorted(set(request.get('refunded_task_ids',[]))|set(released))
        return released

    def observe_remaining(self,name,count):
        if isinstance(count,bool) or not isinstance(count,int) or not 0<=count<=self.daily_limit:
            raise ValueError('平台剩余额度必须是0至每日上限之间的整数')
        with self.store.txn() as data:
            acc=self._account(data,name);day=self._day(acc)
            charged=sum(e.get('state')=='charged' for e in day['entries'].values())
            submitted=sum(e.get('state') in ('submitted','unknown') for e in day['entries'].values())
            inferred=max(0,self.daily_limit-count-charged-submitted)
            day.update(baseline_used=max(int(day.get('baseline_used',0)),inferred),estimated=False,
                       observed_remaining=count,platform_exhausted=bool(day.get('platform_exhausted')) or count==0)

    def pick(self):
        self.reset_daily()
        with self.store.txn() as data:
            for name in self.names:
                acc=data['accounts'].get(name,{})
                if acc.get('status')==AVAILABLE and self._public(acc)['remaining']>0:
                    acc['status']=BUSY;return name
        return ''

    def mark_busy(self,name):
        with self.store.txn() as data:
            acc=self._account(data,name)
            if acc.get('status')==AVAILABLE:acc['status']=BUSY;return True
            return acc.get('status')==BUSY

    def release(self,name):
        with self.store.txn() as data:
            acc=self._account(data,name)
            if acc.get('status')==BUSY:acc['status']=AVAILABLE

    def mark_exhausted(self,name,note=''):
        self.observe_remaining(name,0)
        self._set(name,EXHAUSTED,note or '平台提示今日额度已耗尽',mark_date=True)

    def mark_needs_login(self,name,note=''):self._set(name,NEEDS_LOGIN,note or '登录失效')
    def mark_disabled(self,name,note=''):self._set(name,DISABLED,note)

    def reset_daily(self):
        today=self._today()
        with self.store.txn() as data:
            for acc in data.get('accounts',{}).values():
                if acc.get('status')==EXHAUSTED and acc.get('date')!=today:acc.update(status=AVAILABLE,note='',date='')

    def any_available(self):
        accounts=self.store.snapshot().get('accounts',{})
        for account in accounts.values():self._normalize(account)
        return any(accounts.get(n,{}).get('status') in (AVAILABLE,BUSY,'checking')
                   and self._public(accounts[n])['remaining']>0 for n in self.names if n in accounts)

    def blocked_report(self):
        return '\n'.join(f"  - {n}: {self.status(n).get('status','?')} 剩余{self.quota(n)['remaining']}/{self.daily_limit} "
                         f"{self.quota(n)['blocked_reason']}".rstrip() for n in self.names)
