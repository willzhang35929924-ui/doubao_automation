// Resume launch keeps unsaved edits and never calls save before continuing a plan.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const elements=new Map();
function element(selector){
  if (selector === 'meta[name="csrf-token"]') return {content:'test-only-csrf-token'};
  if(!elements.has(selector))elements.set(selector,{value:'',textContent:'',innerHTML:'',style:{display:'none'},listeners:{},addEventListener(name,fn){this.listeners[name]=fn;}});
  return elements.get(selector);
}
const calls=[];
let state={rows:[{record_id:'fixture',keyword:'product',prompt:'saved',videos:[],images:[]}],requested_script_count:2,resume_script_count:2,can_resume:true,running:false,returncode:null};
let failStart=false;
const context=vm.createContext({document:{querySelector:element,activeElement:null},FormData:class{},setInterval(){},console,
  fetch:async(path,options={})=>{
    calls.push({path,options});
    return {ok:!(path==='/api/run'&&failStart),status:failStart?409:200,statusText:'failed',json:async()=>{
      if(path==='/api/state')return structuredClone(state);
      if(path==='/api/run')return failStart?{error:'流水线已在运行'}:{ok:true,resumed:state.can_resume,script_count:2,message:state.can_resume?'继续上次2个脚本，已完成视频不重复生成':'已启动新任务'};
      return {ok:true,updated:0};
    }};
  }});
vm.runInContext(fs.readFileSync('dashboard_ui.js','utf8'),context);
(async()=>{
  await context.refresh();
  element('#rows').innerHTML='unsaved rows';element('#rows').listeners.input();
  await context.run();
  assert.equal(calls.filter(c=>c.path==='/api/edit').length,0,'resume cannot clear existing tasks through auto-save');
  assert.equal(calls.filter(c=>c.path==='/api/settings/count').length,0,'resume cannot save a replacement count');
  const request=calls.find(c=>c.path==='/api/run');assert.ok(!('task_count' in JSON.parse(request.options.body)));
  assert.equal(element('#rows').innerHTML,'unsaved rows');
  assert.equal(vm.runInContext('dirty',context),true,'resume preserves unsaved rows');
  assert.match(element('#log').textContent,/继续上次2个脚本/);
  assert.match(element('#log').textContent,/未保存修改保留供后续新任务/);
  failStart=true;await context.run();
  assert.match(element('#log').textContent,/未启动：流水线已在运行/);
  assert.equal(element('#rows').innerHTML,'unsaved rows');
  failStart=false;state.can_resume=false;await context.run();
  assert.ok(calls.some(c=>c.path==='/api/edit'),'new plans retain auto-save behavior');
  assert.match(element('#log').textContent,/已启动新任务/);
  console.log('Resume UI: skip save on resume, retain dirty input, clear launch feedback and new-plan saving passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
