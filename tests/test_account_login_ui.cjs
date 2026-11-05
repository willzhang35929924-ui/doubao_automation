// Visible login-start feedback; all account/browser data are fixtures.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const elements=new Map();
function el(s){
 if(s==='meta[name="csrf-token"]')return {content:'fixture-csrf'};
 if(!elements.has(s))elements.set(s,{textContent:'',innerHTML:'',disabled:false,style:{display:'none'},addEventListener(){}});
 return elements.get(s);
}
let rows=[],mode='slow',release,adds=0,lists=0;
const ctx=vm.createContext({document:{querySelector:el,activeElement:null},FormData:class{},console,setInterval(){},
 fetch:async(path,opts={})=>{
  if(path==='/api/state')return {ok:true,json:async()=>({rows:[]})};
  if(path==='/api/accounts'){lists++;return {ok:true,json:async()=>({accounts:structuredClone(rows)})};}
  if(path==='/api/accounts/add'){
   adds++; if(mode==='slow')await new Promise(r=>release=r);
   rows=[{name:'fixture',status:'error',note:'fixture browser missing',enabled:false}];
   if(mode==='fail')return {ok:false,status:503,json:async()=>({error:'fixture browser missing',saved:true,name:'fixture'})};
   rows=[{name:'fixture',status:'opening',login_window_open:true,login_status:'opening',enabled:false}];
   return {ok:true,status:202,json:async()=>({name:'fixture',status:'opening',message:'浏览器正在启动'})};
  }
  throw Error('unexpected request '+path);
 }});
vm.runInContext(fs.readFileSync('dashboard_ui.js','utf8'),ctx);
(async()=>{
 const first=ctx.addAccount();await ctx.addAccount();assert.equal(adds,1);
 assert.equal(el('[onclick="addAccount()"]').disabled,true);
 release();await first;assert.match(el('#log').textContent,/正在启动/);
 assert.doesNotMatch(el('#log').textContent,/已打开/);
 rows=[{name:'fixture',status:'error',note:'fixture browser missing',enabled:false}];
 await ctx.loadAccounts();assert.match(el('#log').textContent,/登录窗口启动失败.*fixture browser missing/);
 mode='fail';const before=lists;await ctx.addAccount();
 assert.ok(lists>before,'saved failed account must be refreshed for retry');
 assert.match(el('#accountRows').innerHTML,/fixture browser missing/);
 assert.equal(el('[onclick="addAccount()"]').disabled,false);
 console.log('Account login UI: no duplicate writes, honest pending status, asynchronous failure and retryable saved account passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
