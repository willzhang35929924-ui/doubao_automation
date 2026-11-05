// Presentation-only harness: no network, files, approvals, or generation.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const elements = new Map();
function element(selector) {
  if (selector === 'meta[name="csrf-token"]') return {content:'fixture-csrf'};
  if (!elements.has(selector)) elements.set(selector, {
    value:'', textContent:'', innerHTML:'', style:{}, addEventListener(){},
  });
  return elements.get(selector);
}
const video = {name:'actual"filename.mp4',label:'OWALA <cup> · 脚本2 · 片段1/1',
               path:'/fixture/videos/actual"filename.mp4',size:'6.0MB'};
const context = vm.createContext({
  document:{querySelector:element,activeElement:null}, FormData:class {}, console,
  confirm(){throw Error('No approval or deletion should occur');}, setInterval(){},
  fetch:async(path)=>({ok:true,status:200,json:async()=>path==='/api/videos'?{videos:[video]}:
    path==='/api/state'?{rows:[],running:false}:path==='/api/accounts'?{accounts:[]}:{}}),
});
vm.runInContext(fs.readFileSync('dashboard_ui.js','utf8'),context);
(async()=>{
  await context.loadVideos();
  const html = element('#videoRows').innerHTML;
  assert.match(html,/OWALA &lt;cup&gt; · 脚本2 · 片段1\/1/);
  assert.match(html,/title="actual&quot;filename\.mp4"/);
  assert.ok(html.includes('/media-video?path='+encodeURIComponent(video.path)));
  assert.ok(!html.includes('OWALA <cup>'));
  console.log('video library labels: passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
