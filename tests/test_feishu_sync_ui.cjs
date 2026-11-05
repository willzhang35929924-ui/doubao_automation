// No network or account data: exercise visible feedback and duplicate-click protection.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const elements = new Map();
function element(selector) {
  if (selector === 'meta[name="csrf-token"]') return {content:'test-csrf'};
  if (!elements.has(selector)) elements.set(selector, {
    value:'', textContent:'', innerHTML:'', disabled:false, style:{display:'none'},
    addEventListener() {}
  });
  return elements.get(selector);
}
let release, mode = 'pending', syncCalls = 0;
const ctx = vm.createContext({document:{querySelector:element,activeElement:null},FormData:class {},setInterval(){},console,
  fetch:async(path,options={}) => {
    if (path === '/api/state') return {ok:true,json:async()=>({rows:[],running:false,returncode:null,log:''})};
    if (path !== '/api/sync') throw Error('unexpected request');
    syncCalls++;
    assert.equal(options.headers['X-CSRF-Token'],'test-csrf');
    assert.equal(options.referrerPolicy,'same-origin','Safari POST must preserve its local Origin');
    assert.equal(options.mode,'same-origin');
    assert.equal(options.credentials,'same-origin');
    if (mode === 'pending') return await new Promise(resolve => { release = resolve; });
    if (mode === 'network') throw Error('Failed to fetch');
    return {ok:false,status:400,json:async()=>({error:'请配置飞书应用凭证'})};
  }});
vm.runInContext(fs.readFileSync('dashboard_ui.js','utf8'),ctx);
(async () => {
  const first=ctx.syncFeishu();
  assert.match(element('#sync-result').textContent,/正在连接飞书/);
  assert.equal(element('[onclick="syncFeishu()"]').disabled,true);
  await ctx.syncFeishu();
  assert.equal(syncCalls,1,'pending clicks must not issue another write');
  release({ok:true,json:async()=>({products:2,sources:3})});
  await first;
  assert.match(element('#sync-result').textContent,/产品 2 条，素材 3 条/);
  assert.equal(element('[onclick="syncFeishu()"]').disabled,false);
  mode='config'; await ctx.syncFeishu();
  assert.match(element('#sync-result').textContent,/同步失败.*配置飞书/);
  mode='network'; await ctx.syncFeishu();
  assert.match(element('#sync-result').textContent,/无法连接工作台/);
  console.log('Feishu UI: pending, duplicate click, success, configuration and network errors passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
