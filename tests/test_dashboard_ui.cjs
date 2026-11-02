// 页面逻辑回归，无浏览器、无外部请求。
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const elements = new Map();
function element(selector) {
  if (selector === 'meta[name="csrf-token"]') return {content:'test-only-csrf-token'};
  if (!elements.has(selector)) elements.set(selector, {value:'',textContent:'',innerHTML:'',style:{display:'none'},listeners:{},
    addEventListener(name,fn){this.listeners[name]=fn;}});
  return elements.get(selector);
}
const calls=[];
const requests=[];
let failSave=false;
let folderFail=false;
let stateDisconnected=false;
const state={rows:[{record_id:'r1',keyword:'产品',prompt:'旧内容',videos:[],images:[]}],requested_script_count:1,returncode:null,running:false};
const context=vm.createContext({document:{querySelector:element,activeElement:null},FormData:class {},setInterval(){},
  fetch:async(path,options={})=>{calls.push(path);requests.push({path,options});if(path==='/api/state'&&stateDisconnected)throw Error('Failed to fetch');return {ok:!(path==='/api/edit'&&failSave)&&!(path==='/api/open-video'&&folderFail),statusText:'failed',
    json:async()=>path==='/api/state'?structuredClone(state):path==='/api/edit'&&failSave?{error:'磁盘写入失败'}:path==='/api/open-video'?(folderFail?{error:'无法打开文件夹：0x80070005'}:{message:'已打开文件夹并选中视频',folder:'D:\\视频目录'}):{ok:true,updated:1}};},console});
vm.runInContext(fs.readFileSync('dashboard_ui.js','utf8'),context);
(async()=>{
  await context.refresh();
  element('#rows').innerHTML='正在编辑的内容';element('#rows').listeners.input();
  await context.refresh();
  assert.equal(element('#rows').innerHTML,'正在编辑的内容','离开输入框后定时刷新也不能丢失修改');
  element('[data-i="0"][data-k="kw"]').value='产品';element('[data-i="0"][data-k="prompt"]').value='新内容';
  failSave=true;await context.run();
  assert.ok(!calls.includes('/api/run'),'保存失败不能继续启动');
  assert.match(element('#log').textContent,/磁盘写入失败/);
  failSave=false;await context.run();assert.ok(calls.includes('/api/run'));
  const saved=JSON.parse(requests.filter(r=>r.path==='/api/edit').at(-1).options.body);
  const started=JSON.parse(requests.filter(r=>r.path==='/api/run').at(-1).options.body);
  assert.ok(!('task_count' in saved) && !('task_count' in started),'保存与启动不能附带人为脚本数量');
  const folderStatus={textContent:''};
  const button={disabled:false,dataset:{path:"D:\\中文 目录\\视频's.mp4"},closest(){return {querySelector(){return folderStatus;}};}};
  await context.openFolder(button);
  assert.equal(JSON.parse(requests.at(-1).options.body).path,button.dataset.path,'原样传递中文、空格和引号');
  assert.match(folderStatus.textContent,/已打开文件夹/);assert.equal(button.disabled,false);
  folderFail=true;await context.openFolder(button);
  assert.match(folderStatus.textContent,/0x80070005/,'错误显示在视频按钮旁');assert.equal(button.disabled,false);
  stateDisconnected=true;await context.refresh();
  assert.match(element('#log').textContent,/自动重试/);
  stateDisconnected=false;await context.refresh();
  assert.equal(element('#log').textContent,'连接已恢复','后台重载后成功轮询必须清除过时连接错误');
  stateDisconnected=true;await context.refresh();context.msg('保存失败：文件被占用');
  stateDisconnected=false;await context.refresh();
  assert.equal(element('#log').textContent,'保存失败：文件被占用','恢复连接不能覆盖用户操作结果');
  console.log('UI regression: dirty edits, save gating, launch, folder path and visible errors passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
