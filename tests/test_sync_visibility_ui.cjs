// Source editing and normal sync restoration without the removed overview.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const elements=new Map();
function el(s){
 if(s==='meta[name="csrf-token"]')return {content:'fixture-csrf'};
 assert.ok(!['#productRows','#sync-summary'].includes(s),'removed overview must not be queried');
 if(!elements.has(s))elements.set(s,{value:'',textContent:'',innerHTML:'',disabled:false,style:{display:'none'},listeners:{},addEventListener(k,f){this.listeners[k]=f;}});
 return elements.get(s);
}
const summary={products:{remote_records:5,imported_records:2,skipped_no_images:3},
 sources:{remote_records:8,imported_records:3,skipped_empty:5,skipped_oversized_only:0,oversized_attachments:0,hidden_records:2,visible_records:1},
 unmatched_products:1,sources_without_images:0,restored_hidden:0};
const state={rows:[{record_id:'r1',keyword:'linked',prompt:'fixture prompt',videos:[],images:['one.png']}],
 product_rows:[{record_id:'p1',keyword:'linked',images:['one.png'],linked_sources:1},
               {record_id:'p2',keyword:'<product-only>',prompt:'<product-prompt>',images:['two.png'],linked_sources:0}],
 sync_summary:structuredClone(summary),hidden_sources:2,running:false,returncode:null};
const writes=[];
let fail=false;
const ctx=vm.createContext({document:{querySelector:el,activeElement:null},FormData:class{},console,setInterval(){},
 fetch:async(path,options={})=>{
  if(path==='/api/state')return {ok:true,json:async()=>structuredClone(state)};
  if(path==='/api/sync'){
   const body=JSON.parse(options.body);writes.push(body);
   if(fail)return {ok:false,status:503,json:async()=>({error:'fixture TLS failure'})};
   let restored=0;
   if(body.restore_hidden!==false){restored=2;state.hidden_sources=0;state.sync_summary.sources.hidden_records=0;state.sync_summary.sources.visible_records=3;state.sync_summary.restored_hidden=2;}
   return {ok:true,json:async()=>({products:2,sources:3,restored_hidden:restored,summary:structuredClone(state.sync_summary)})};
  }
  throw Error('unexpected request '+path);
 }});
vm.runInContext(fs.readFileSync('dashboard_ui.js','utf8'),ctx);
(async()=>{
 await ctx.refresh();
 assert.doesNotMatch(el('#rows').innerHTML,/product-only|product-prompt|产品提示词/,'product records cannot become generation input rows');
 assert.match(el('#rows').innerHTML,/fixture prompt/);
 assert.match(el('#rows').innerHTML,/\/media-file\?path=one.png/,'source images remain visible');
 el('#rows').innerHTML='unsaved fixture';el('#rows').listeners.input();
 await ctx.syncFeishu();assert.equal(writes.length,0,'unsaved source edits block sync');
 await ctx.refresh();assert.equal(el('#rows').innerHTML,'unsaved fixture','polling preserves edits');
 vm.runInContext('dirty=false',ctx);
 fail=true;await ctx.syncFeishu();
 assert.equal(state.hidden_sources,2);assert.match(el('#sync-result').textContent,/同步失败.*fixture TLS failure/);
 fail=false;await ctx.syncFeishu();
 assert.deepEqual(writes.at(-1),{},'ordinary sync must restore deleted Feishu records without an extra action');
 assert.equal(state.hidden_sources,0);
 assert.equal(el('#sync-result').textContent,'同步完成：产品 2 条，素材 3 条。');
 assert.equal(el('#log').textContent,'同步完成：产品 2 条，素材 3 条。');
 console.log('Sync visibility UI: simplified source list, concise feedback, dirty guard and normal sync restore passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
