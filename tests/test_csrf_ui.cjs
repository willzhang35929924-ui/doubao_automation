// Local request security and markup regression; all tokens/data are fixtures.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const elements = new Map(), tokenMeta = {content:'fixture-old-token'};
function element(selector) {
  if (selector === 'meta[name="csrf-token"]') return tokenMeta;
  if (!elements.has(selector)) elements.set(selector,{value:'',textContent:'',innerHTML:'',style:{display:'none'},listeners:{},
    addEventListener(name,fn){this.listeners[name]=fn;}});
  return elements.get(selector);
}
class FakeFormData { append() {} }
const state={rows:[{record_id:"x');alert(1);//",keyword:'test',prompt:'',videos:[],images:[]}],running:false,returncode:null};
const calls=[]; let mode='ok', writes=0;
function response(status,payload){return {status,ok:status>=200&&status<300,statusText:'failure',json:async()=>payload};}
const context=vm.createContext({document:{querySelector:element,activeElement:null},FormData:FakeFormData,setInterval(){},confirm(){return true;},console,
  fetch:async(path,options={})=>{
    calls.push({path,options});
    if(path==='/api/state')return response(200,structuredClone(state));
    if(path==='/api/csrf')return mode==='bad-token-fetch'?response(200,{}):response(200,{token:'fixture-new-token'});
    if(options.method==='POST'){
      writes++;
      if(mode==='network')throw Error('network');
      if(mode==='origin')return response(403,{code:'origin_invalid',error:'来源不可信'});
      if(mode==='forbidden')return response(403,{code:'forbidden',error:'拒绝'});
      if(mode==='bad-token-fetch'||mode==='always-stale'||(mode==='stale'&&writes===1))return response(403,{code:'csrf_invalid',error:'令牌失效'});
      return response(200,{ok:true,updated:1});
    }
    return response(200,{});
  }});
vm.runInContext(fs.readFileSync('dashboard_ui.js','utf8'),context);
(async()=>{
  await context.refresh(); calls.length=0;
  const markup=element('#rows').innerHTML;
  assert.match(markup,/data-record-id="x&#39;\);alert\(1\);\/\/" onclick="deleteRow\(this\)"/);
  assert.doesNotMatch(markup,/onclick="deleteRow\('/,'record ID must never enter JS source');

  await context.api('/api/state');
  assert.equal(calls.at(-1).options.headers['X-CSRF-Token'],undefined,'GET must not send token');
  await context.api('/api/edit',{rows:[]});
  let post=calls.at(-1);assert.equal(post.options.headers['X-CSRF-Token'],'fixture-old-token');
  assert.equal(post.options.headers['Content-Type'],'application/json');
  assert.equal(post.options.mode,'same-origin');

  const form=new FakeFormData();await context.api('/api/upload',form);
  post=calls.at(-1);assert.equal(post.options.headers['X-CSRF-Token'],'fixture-old-token');
  assert.equal(post.options.headers['Content-Type'],undefined,'browser must generate multipart boundary');
  assert.equal(post.options.body,form);

  mode='stale';writes=0;calls.length=0;await context.api('/api/upload',form);
  assert.deepEqual(calls.map(c=>c.path),['/api/upload','/api/csrf','/api/upload']);
  assert.equal(calls[1].options.cache,'no-store');
  assert.equal(calls[2].options.headers['X-CSRF-Token'],'fixture-new-token');
  assert.equal(calls[2].options.body,form,'multipart body can be resent after auth rejection');
  assert.equal(tokenMeta.content,'fixture-new-token');

  for(const failure of ['origin','forbidden','network']){
    mode=failure;writes=0;calls.length=0;
    await assert.rejects(context.api('/api/videos/delete',{path:'fixture'}));
    assert.deepEqual(calls.map(c=>c.path),['/api/videos/delete'],failure+' cannot retry');
  }
  mode='always-stale';writes=0;calls.length=0;
  await assert.rejects(context.api('/api/run',{}),/刷新页面/);
  assert.deepEqual(calls.map(c=>c.path),['/api/run','/api/csrf','/api/run'],'refresh retry is bounded');
  mode='bad-token-fetch';writes=0;calls.length=0;
  await assert.rejects(context.api('/api/run',{}),/安全令牌更新失败/);
  assert.deepEqual(calls.map(c=>c.path),['/api/run','/api/csrf']);
  calls.length=0;await assert.rejects(context.api('https://elsewhere.example/api/run',{}),/本地 API/);
  assert.equal(calls.length,0,'never send token cross-origin');

  mode='ok';writes=0;calls.length=0;
  await context.deleteRow({dataset:{recordId:state.rows[0].record_id}});
  const deleted=calls.find(c=>c.path==='/api/rows/delete');
  assert.equal(JSON.parse(deleted.options.body).record_id,state.rows[0].record_id);
  console.log('CSRF UI: JSON, multipart, bounded token rotation, no unsafe retries and escaped record IDs passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
