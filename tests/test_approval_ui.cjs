// Approval controls use the actual page script with a small DOM/fetch harness.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const elements = new Map();
function element(selector) {
  if (selector === 'meta[name="csrf-token"]') return {content:'test-only-csrf-token'};
  if (!elements.has(selector)) elements.set(selector,{value:'',textContent:'',innerHTML:'',style:{display:'none'},listeners:{},
    addEventListener(name,fn){this.listeners[name]=fn;}});
  return elements.get(selector);
}
const source = "D:\\豆包自动化\\workspace\\videos\\中文's.mp4";
const video={name:"中文's.mp4",path:source,size:'3.4MB',postprocess:null};
const requests=[];
let failApproval=false, approvalGate=null, failDelete=false, deleteGate=null, videoPresent=true, confirmAnswer=true, onInterval;
const confirmations=[];
const state={rows:[],requested_script_count:1,running:false,returncode:null};
const context=vm.createContext({document:{querySelector:element,activeElement:null},FormData:class {},console,
  confirm(text){confirmations.push(text);return confirmAnswer;},
  setInterval(fn){onInterval=fn;},
  fetch:async(path,options={})=>{
    requests.push({path,options});
    if (path==='/api/videos/approve' && approvalGate) await approvalGate;
    if (path==='/api/videos/delete' && deleteGate) await deleteGate;
    return {ok:!(path==='/api/videos/approve'&&failApproval)&&!(path==='/api/videos/delete'&&failDelete),status:path==='/api/non-json-test'?404:failDelete&&path==='/api/videos/delete'?409:200,statusText:'failed',json:async()=>{
      if (path==='/api/non-json-test') throw new SyntaxError('Unexpected token <');
      if (path==='/api/state') return structuredClone(state);
      if (path==='/api/videos') return {videos:videoPresent?[structuredClone(video)]:[]};
      if (path==='/api/open-video') return {message:'已打开视频所在文件夹',folder:'D:\\目录'};
      if (path==='/api/videos/approve') return failApproval?{error:'GPU 暂不可用'}:{ok:true,job:{status:'queued',stage:'等待去水印',progress:0}};
      if (path==='/api/videos/delete') {
        if (failDelete) return {error:'处理中，暂不能删除'};
        videoPresent=false;return {ok:true,deleted:source};
      }
      return {ok:true};
    }};
  }});
vm.runInContext(fs.readFileSync('dashboard_ui.js','utf8'),context);
function button() {
  const status={textContent:''},folder={textContent:''},deletion={textContent:''};
  return {disabled:false,dataset:{path:source},status,folder,deletion,
    closest(){return {querySelector(selector){return selector==='.approval-result'?status:selector==='.delete-result'?deletion:folder;}};}};
}
(async()=>{
  await assert.rejects(context.api('/api/non-json-test'),/服务响应异常或正在更新（HTTP 404）/);
  await context.loadVideos();
  assert.match(element('#videoRows').innerHTML,/>审核通过<\/button>/);
  assert.match(element('#videoRows').innerHTML,/data-path="D:.*&#39;s\.mp4"/,'attribute safely escapes an apostrophe');
  assert.ok(!element('#videoRows').innerHTML.includes("approveVideo('"));
  const first=button();let release;approvalGate=new Promise(resolve=>{release=resolve;});
  const pending=context.approveVideo(first);
  assert.equal(first.disabled,true);await context.approveVideo(first);
  assert.equal(requests.filter(r=>r.path==='/api/videos/approve').length,1,'rapid repeated click submits once');
  release();await pending;approvalGate=null;
  const post=requests.find(r=>r.path==='/api/videos/approve');
  assert.equal(JSON.parse(post.options.body).path,source);
  assert.match(element('#videoRows').innerHTML,/等待去水印/);
  assert.match(element('#videoRows').innerHTML,/disabled>处理中…<\/button>/);
  video.postprocess={status:'running',stage:'SeedVR 1080P 超分',progress:62.4};await context.loadVideos();
  assert.match(element('#videoRows').innerHTML,/SeedVR 1080P 超分 · 62%/);
  video.postprocess={status:'done',stage:'完成',progress:100,output_path:'D:\\approved\\result.mp4'};await context.loadVideos();
  assert.match(element('#videoRows').innerHTML,/查看1080P成片/);
  assert.match(element('#videoRows').innerHTML,/media-video\?path=D%3A%5Capproved%5Cresult.mp4/);
  video.postprocess={status:'failed',stage:'去水印',error:'模型缺失 <unsafe>',log_tail:'trace <safe>'};await context.loadVideos();
  assert.match(element('#videoRows').innerHTML,/>重试处理<\/button>/);
  assert.match(element('#videoRows').innerHTML,/模型缺失 &lt;unsafe&gt;/);
  assert.ok(!element('#videoRows').innerHTML.includes('trace <safe>'));
  failApproval=true;const retry=button();await context.approveVideo(retry);
  assert.match(retry.status.textContent,/GPU 暂不可用/);assert.equal(retry.disabled,false);
  const folderButton=button();await context.openFolder(folderButton);await context.loadVideos();
  assert.match(element('#videoRows').innerHTML,/已打开视频所在文件夹：D:\\目录/,'polling preserves folder feedback');
  video.postprocess={status:'running',stage:'SeedVR 超分',progress:55};await context.loadVideos();
  assert.match(element('#videoRows').innerHTML,/onclick="deleteVideo\(this\)" title="处理中，暂不能删除" disabled>删除/);
  await context.deleteVideo(button());
  assert.equal(confirmations.length,0,'active video cannot enter deletion confirmation');
  assert.equal(requests.filter(r=>r.path==='/api/videos/delete').length,0);
  video.postprocess=null;await context.loadVideos();confirmAnswer=false;
  await context.deleteVideo(button());
  assert.equal(requests.filter(r=>r.path==='/api/videos/delete').length,0,'cancel sends no delete');
  assert.match(confirmations.at(-1),/原始 MP4/);assert.match(confirmations.at(-1),/磁盘删除无法撤销/);
  assert.ok(confirmations.at(-1).includes(source));
  assert.doesNotMatch(confirmations.at(-1),/approved|成片|处理记录/,'confirmation is limited to the original file');
  confirmAnswer=true;failDelete=true;const rejectedDelete=button();await context.deleteVideo(rejectedDelete);
  assert.match(rejectedDelete.deletion.textContent,/删除失败：处理中/);assert.equal(rejectedDelete.disabled,false);
  assert.equal(videoPresent,true,'server rejection preserves row');
  assert.match(element('#videoRows').innerHTML,/已打开视频所在文件夹：D:\\目录/,'deletion error preserves folder feedback');
  failDelete=false;let finishDelete;deleteGate=new Promise(resolve=>{finishDelete=resolve;});
  const deleting=button();const deletion=context.deleteVideo(deleting);assert.equal(deleting.disabled,true);
  await context.deleteVideo(deleting);
  const approvalCount=requests.filter(r=>r.path==='/api/videos/approve').length;
  await context.approveVideo(button());assert.equal(requests.filter(r=>r.path==='/api/videos/approve').length,approvalCount,'deleting video cannot start processing');
  assert.equal(requests.filter(r=>r.path==='/api/videos/delete').length,2,'failed request plus one confirmed deletion');
  finishDelete();await deletion;deleteGate=null;
  assert.equal(JSON.parse(requests.filter(r=>r.path==='/api/videos/delete').at(-1).options.body).path,source);
  assert.equal(element('#videoRows').innerHTML,'暂无视频');assert.equal(requests.at(-1).path,'/api/videos','successful deletion refreshes the list');
  element('#videos').style.display='';onInterval();await new Promise(resolve=>setImmediate(resolve));
  assert.ok(requests.filter(r=>r.path==='/api/videos').length>1,'visible video tab polls status');
  console.log('Video UI: approval progress/output/retry, safe delete/cancel/busy guard, feedback retention, polling passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
