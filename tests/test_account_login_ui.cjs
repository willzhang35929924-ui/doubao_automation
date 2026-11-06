// Login and account-window feedback; all account/browser data are fixtures.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const elements=new Map();
function el(s){
 if(s==='meta[name="csrf-token"]')return {content:'fixture-csrf'};
 if(!elements.has(s))elements.set(s,{textContent:'',innerHTML:'',disabled:false,style:{display:'none'},addEventListener(){}});
 return elements.get(s);
}
let rows=[],mode='slow',release,adds=0,lists=0;
let visitMode='slow',releaseVisit;
const accountWrites=[];
const updateAccount=(name,values)=>{ rows=rows.map(row=>row.name===name?{...row,...values}:row); };
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
  if(['/api/accounts/visit','/api/accounts/open','/api/accounts/close'].includes(path)){
   assert.equal(opts.method,'POST');
   const body=JSON.parse(opts.body), {name}=body;
   accountWrites.push([path,body]);
   if(path==='/api/accounts/open'){
    updateAccount(name,{login_window_open:true,login_status:'opening'});
    return {ok:true,status:202,json:async()=>({status:'opening',message:'登录浏览器正在启动'})};
   }
   if(path==='/api/accounts/close'){
    updateAccount(name,{account_window_open:false,account_window_status:'',account_window_note:''});
    return {ok:true,json:async()=>({})};
   }
   if(visitMode==='fail')return {ok:false,status:503,json:async()=>({error:'fixture account browser unavailable'})};
   if(visitMode==='focus'){
    updateAccount(name,{account_window_open:true,account_window_status:'open',account_window_note:''});
    return {ok:true,status:200,json:async()=>({status:'open',message:'已聚焦账号窗口'})};
   }
   await new Promise(r=>releaseVisit=r);
   updateAccount(name,{account_window_open:true,account_window_status:'opening'});
   return {ok:true,status:202,json:async()=>({status:'opening',message:'账号窗口正在启动'})};
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

 const name='fixture & two', button={dataset:{name}};
 const accountRow=()=>el('#accountRows').innerHTML.split('<article class="row">').find(row=>row.startsWith('<b>fixture &amp; two</b>'));
 rows.push({name,status:'available',enabled:true});
 await ctx.loadAccounts();
 assert.match(accountRow(),/onclick="openAccount\(this\)">打开登录窗口<\/button>\s*<button[^>]+data-name="fixture &amp; two" onclick="visitAccount\(this\)">打开账号/);
 const visiting=ctx.visitAccount(button);
 await ctx.visitAccount(button);
 assert.deepEqual(accountWrites,[['/api/accounts/visit',{name}]],'same account must submit only once while opening');
 assert.match(accountRow(),/onclick="visitAccount\(this\)" disabled/);
 releaseVisit();await visiting;
 assert.match(el('#log').textContent,/账号窗口正在启动/);
 assert.match(accountRow(),/onclick="openAccount\(this\)" disabled/);
 assert.match(accountRow(),/onclick="visitAccount\(this\)" disabled/);
 assert.match(accountRow(),/onclick="closeAccount\(this\)">关闭账号窗口/);

 updateAccount(name,{account_window_status:'open',account_window_note:'已打开账号浏览器'});
 await ctx.loadAccounts();
 assert.match(accountRow(),/已打开账号浏览器/);
 assert.match(accountRow(),/onclick="visitAccount\(this\)">打开账号/);
 assert.match(accountRow(),/onclick="openAccount\(this\)" disabled/);
 visitMode='focus';await ctx.visitAccount(button);
 assert.deepEqual(accountWrites.at(-1),['/api/accounts/visit',{name}]);
 assert.match(el('#log').textContent,/已聚焦账号窗口/);
 await ctx.closeAccount(button);
 assert.deepEqual(accountWrites.at(-1),['/api/accounts/close',{name}]);
 assert.match(el('#log').textContent,/账号窗口已关闭/);
 assert.match(accountRow(),/onclick="closeAccount\(this\)">关闭登录窗口/);

 await ctx.visitAccount(button);
 updateAccount(name,{account_window_open:false,account_window_status:'',account_window_note:''});
 const beforePoll=accountWrites.length;
 await ctx.loadAccounts();
 assert.equal(accountWrites.length,beforePoll,'native close needs no additional account action');
 assert.match(accountRow(),/账号窗口已关闭/);
 assert.doesNotMatch(accountRow(),/已聚焦账号窗口/);
 assert.match(el('#log').textContent,/账号窗口已关闭/);
 assert.match(accountRow(),/onclick="openAccount\(this\)">打开登录窗口/);

 await ctx.visitAccount(button);
 updateAccount(name,{account_window_status:'error',account_window_note:'fixture account window disconnected'});
 await ctx.loadAccounts();
 assert.match(accountRow(),/fixture account window disconnected/);
 assert.match(el('#log').textContent,/fixture account window disconnected/);
 updateAccount(name,{account_window_open:false});
 await ctx.loadAccounts();
 assert.match(accountRow(),/fixture account window disconnected/);

 visitMode='fail';await ctx.visitAccount(button);
 assert.match(accountRow(),/fixture account browser unavailable/);
 assert.match(accountRow(),/onclick="visitAccount\(this\)">打开账号/);
 await ctx.openAccount(button);
 assert.deepEqual(accountWrites.at(-1),['/api/accounts/open',{name}]);
 assert.match(accountRow(),/onclick="visitAccount\(this\)" disabled/);
 assert.match(el('#log').textContent,/登录浏览器正在启动/);
 console.log('Account windows UI: login preserved, selected account visit, no duplicate writes, pending/open/focus/close/error states passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
