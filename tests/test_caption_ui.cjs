// Workbench caption flow with an in-memory DOM and API.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const elements = new Map();
const busyMessage = {textContent:''};
const generateButton = {dataset:{captionUnavailable:'false'},disabled:false};
function element(selector) {
  if (selector === 'meta[name="csrf-token"]') return {content:'fixture-csrf'};
  if (!elements.has(selector)) elements.set(selector, {
    value:'',textContent:'',innerHTML:'',listeners:{},
    addEventListener(name,handler){this.listeners[name]=handler;},
    querySelector:element,
    querySelectorAll:selector=>selector==='[data-caption-busy]'?[busyMessage]:[generateButton],
  });
  return elements.get(selector);
}
const id = 'fixture&source';
const row = {record_id:id,keyword:'产品',prompt:'原提示词',images:['display-only.webp'],
  videos:['a.webp','b.webp','c.webp','d.webp','e.webp','f.webp','商品.mp4','g.webp','创意.mp4'],
  caption_images:['产品<1>.webp','产品2.webp'],
  caption_videos:[{index:6,path:'商品.mp4'},{index:8,path:'创意.mp4'}],captions:{images:null,video:null}};
const state = {rows:[row],running:false,returncode:null};
const writes = [];
let holdGenerate = false, releaseGenerate, holdState = false, releaseState, failGenerate = false;
const reply = data => ({ok:true,json:async()=>structuredClone(data)});
const context = vm.createContext({document:{querySelector:element,getElementById:element,activeElement:null},
  FormData:class{},setInterval(){},console,
  fetch:async(path,options={})=>{
    if (path === '/api/state') {
      const snapshot = structuredClone(state);
      if (holdState) { holdState=false; await new Promise(resolve=>releaseState=resolve); }
      return reply(snapshot);
    }
    const body = JSON.parse(options.body);
    writes.push([path,body]);
    if (path === '/api/edit') { row.prompt=body.rows[0].prompt_text; return reply({updated:1}); }
    if (path === '/api/captions/generate') {
      if (holdGenerate) { holdGenerate=false; await new Promise(resolve=>releaseGenerate=resolve); }
      if (failGenerate) return {ok:false,status:503,json:async()=>({error:'识别服务暂不可用'})};
      const prior = row.captions[body.kind];
      const target = {index:body.index,role:body.role,fingerprint:`${body.kind}/${body.index}/${body.role}`};
      const text = `识别 <${body.kind}/${body.index}>`;
      if (prior?.edited) return reply({entry:prior,candidate_text:text,candidate_target:target});
      row.captions[body.kind] = {...target,text,enabled:true,edited:false,revision:(prior?.revision || 0)+1};
      return reply({entry:row.captions[body.kind]});
    }
    if (path === '/api/captions/save') {
      const {kind,record_id,...fields} = body;
      assert.equal(record_id,id);
      assert.equal(fields.revision,row.captions[kind].revision);
      row.captions[kind] = {...fields,edited:true,revision:fields.revision+1};
      return reply({entry:row.captions[kind]});
    }
    if (path === '/api/captions/link/generate') {
      if (holdGenerate) { holdGenerate=false; await new Promise(resolve=>releaseGenerate=resolve); }
      if (failGenerate) return {ok:false,status:503,json:async()=>({error:'无法读取产品页面'})};
      const prior = row.captions.link;
      assert.equal(body.revision,prior?.revision ?? null);
      const text = '识别 <链接产品描述>';
      const sameUrl = prior?.url === body.url;
      if (sameUrl && prior.edited) return reply({entry:prior,candidate_text:text,candidate_url:body.url});
      row.captions.link = {url:body.url,text,enabled:true,
        edited:false,revision:(prior?.revision || 0)+1};
      return reply({entry:row.captions.link});
    }
    if (path === '/api/captions/link/save') {
      const {record_id,...fields} = body;
      assert.equal(record_id,id);
      assert.equal(fields.revision,row.captions.link.revision);
      row.captions.link = {...fields,edited:true,revision:fields.revision+1};
      return reply({entry:row.captions.link});
    }
    if (path === '/api/captions/preview') return reply({prompt:`${row.prompt}\n<实际拼接描述>`
      + (row.captions.link ? '\n'+row.captions.link.text : '')});
    throw Error('unexpected request '+path);
  },
});
vm.runInContext(fs.readFileSync('dashboard_ui.js','utf8'),context);
const sourceRows = element('#rows');
const mount = element('captions-'+id);
const panel = kind => ({dataset:{captionKind:kind},querySelector:()=>generateButton});
const control = (kind,properties={}) => ({...properties,dataset:properties.dataset || {},
  closest:selector=>selector==='[data-caption-row]'?{dataset:{captionRow:id}}:kind?panel(kind):null});
const choose = (kind,choice,value)=>context.chooseCaption(control(kind,{dataset:{captionChoice:choice},value}));
const input = (kind,field,properties)=>sourceRows.listeners.input({target:control(kind,{dataset:{captionField:field},...properties})});
const link = control('link'), images = control('images'), video = control('video'), preview = control(null);
const lastBody = route=>writes.filter(([path])=>path==='/api/captions/'+route).at(-1)?.[1];

(async()=>{
  await context.refresh();
  assert.match(sourceRows.innerHTML,/data-caption-kind="link".*data-caption-kind="images"/s);
  assert.match(sourceRows.innerHTML,/onclick="generateLinkCaption\(this\)" disabled>一键识别链接/);
  assert.match(sourceRows.innerHTML,/一键识别图片/);
  assert.match(sourceRows.innerHTML,/一键识别视频/);
  assert.match(sourceRows.innerHTML,/当前产品整组（2 张）/);
  assert.match(sourceRows.innerHTML,/产品&lt;1&gt;\.webp/);
  assert.match(sourceRows.innerHTML,/value="reference" selected>创意参考/);
  const videoPanel = sourceRows.innerHTML.split('data-caption-kind="video"')[1];
  assert.doesNotMatch(videoPanel,/\.webp/);
  assert.match(videoPanel,/value="6">视频 1 · 商品\.mp4/);
  await context.generateCaption(video);
  assert.equal(writes.length,0,'choose a video when there is more than one');

  choose('video','media','8');
  choose('video','role','product');
  holdState=true;
  const stalePoll = context.refresh();
  holdGenerate=true;
  const recognizing = context.generateCaption(images);
  assert.equal(generateButton.disabled,true);
  assert.match(busyMessage.textContent,/识别正在进行/);
  await context.generateCaption(video);
  assert.equal(writes.length,1,'recognition is globally exclusive');
  assert.deepEqual(lastBody('generate'),{record_id:id,kind:'images',index:null,role:'product'});
  const sourcePrompt = element('[data-i="0"][data-k="prompt"]');
  sourcePrompt.value='识别期间的未保存原提示词';
  sourceRows.listeners.input({target:{closest:()=>null}});
  releaseGenerate(); await recognizing;
  releaseState(); await stalePoll;
  assert.match(mount.innerHTML,/识别 &lt;images\/null&gt;/,'stale polling must not replace newly saved caption');
  assert.equal(sourcePrompt.value,'识别期间的未保存原提示词');
  assert.equal(row.prompt,'原提示词');
  assert.equal(generateButton.disabled,false);
  await context.generateCaption(video);
  await context.previewCaption(preview);
  assert.equal(writes.length,1,'unsaved source prompts block recognition and preview');
  assert.match(mount.innerHTML,/先保存原提示词/);
  element('[data-i="0"][data-k="kw"]').value=row.keyword;
  await context.saveRows(false);

  choose('images','media','1');
  await context.generateCaption(images);
  assert.deepEqual(lastBody('generate'),{record_id:id,kind:'images',index:1,role:'product'});
  await context.generateCaption(video);
  assert.deepEqual(lastBody('generate'),{record_id:id,kind:'video',index:8,role:'product'});
  assert.match(mount.innerHTML,/视频描述.*识别 &lt;video\/8&gt;/s);

  input('video','text',{value:'人工编辑<视频>'});
  input('video','enabled',{checked:false});
  await context.refresh();
  assert.match(sourceRows.innerHTML,/人工编辑&lt;视频&gt;/,'poll retains caption drafts');
  assert.doesNotMatch(sourceRows.innerHTML,/data-caption-field="enabled" checked/);
  await context.saveCaption(video);
  assert.deepEqual(lastBody('save'),{record_id:id,kind:'video',text:'人工编辑<视频>',revision:1,
    fingerprint:'video/8/product',index:8,role:'product',enabled:false});
  assert.equal(row.captions.images.text,'识别 <images/1>');

  choose('video','media','6');
  choose('video','role','reference');
  await context.generateCaption(video);
  assert.equal(row.captions.video.text,'人工编辑<视频>');
  assert.match(mount.innerHTML,/人工编辑&lt;视频&gt;/);
  assert.match(mount.innerHTML,/新视频描述.*识别 &lt;video\/6&gt;/s);
  await context.saveCaption(video,true);
  assert.deepEqual(lastBody('save'),{record_id:id,kind:'video',text:'识别 <video/6>',revision:2,
    fingerprint:'video/6/reference',index:6,role:'reference',enabled:false});
  await context.previewCaption(preview);
  assert.deepEqual(lastBody('preview'),{record_id:id});
  assert.match(mount.innerHTML,/识别期间的未保存原提示词\s+&lt;实际拼接描述&gt;/);
  assert.equal(sourcePrompt.value,'识别期间的未保存原提示词');
  assert.equal(writes.filter(([path])=>path==='/api/edit').length,1);

  row.captions.images.stale=true;
  await context.refresh();
  assert.match(sourceRows.innerHTML,/素材已变化，请重新识别/);
  assert.match(sourceRows.innerHTML,/onclick="saveCaption\(this\)" disabled>保存图片描述/);
  failGenerate=true; await context.generateCaption(images);
  assert.match(mount.innerHTML,/识别服务暂不可用/);
  assert.equal(generateButton.disabled,false);

  // Keep URLs and descriptions scoped to this row. Never fetch a draft URL
  // while the user is typing or expose it as markup.
  const url = 'https://products.example.com/item?sku=1&view=detail';
  const nextUrl = 'https://products.example.com/another';
  let before = writes.length;
  input('link','url',{value:'"><img src=x>'});
  await context.refresh();
  assert.match(sourceRows.innerHTML,/value="&quot;&gt;&lt;img src=x&gt;"/);
  input('link','url',{value:' '+url+' '});
  await context.refresh();
  assert.match(sourceRows.innerHTML,/sku=1&amp;view=detail/);
  await context.previewCaption(preview);
  await context.syncFeishu();
  await context.run();
  assert.equal(writes.length,before,'unsaved URL drafts block preview, sync and new run');

  failGenerate=false;
  holdGenerate=true;
  const linking = context.generateLinkCaption(link);
  assert.equal(generateButton.disabled,true);
  await context.generateCaption(images);
  assert.deepEqual(lastBody('link/generate'),{record_id:id,url,revision:null});
  assert.equal(writes.length,before+1,'link recognition shares the media lock');
  releaseGenerate(); await linking;
  assert.match(mount.innerHTML,/识别 &lt;链接产品描述&gt;/);
  assert.doesNotMatch(mount.innerHTML.split('data-caption-kind="images"')[0],
    /data-caption-field="enabled"|带入最终提示词/);
  await context.previewCaption(preview);
  assert.match(mount.innerHTML,/<pre[^>]*>.*&lt;实际拼接描述&gt;\s+识别 &lt;链接产品描述&gt;/s);

  input('link','text',{value:'人工编辑 <链接描述>'});
  input('link','url',{value:nextUrl});
  await context.refresh();
  assert.match(sourceRows.innerHTML,/人工编辑 &lt;链接描述&gt;/);
  assert.match(sourceRows.innerHTML,/value="https:\/\/products\.example\.com\/another"/);
  before = writes.length;
  await context.generateLinkCaption(link);
  assert.equal(writes.length,before,'save or restore edited description before recognition');
  await context.saveLinkCaption(link);
  assert.deepEqual(lastBody('link/save'),{record_id:id,url,text:'人工编辑 <链接描述>',enabled:true,revision:1},
    'manually saving the existing description must not bind it to the newly pasted URL');
  context.restoreLinkUrl(link);

  await context.generateLinkCaption(link);
  assert.equal(row.captions.link.text,'人工编辑 <链接描述>');
  assert.match(mount.innerHTML,/新链接描述.*识别 &lt;链接产品描述&gt;/s);
  await context.saveLinkCaption(link,true);
  assert.deepEqual(lastBody('link/save'),{record_id:id,url,text:'识别 <链接产品描述>',enabled:true,revision:2});
  await context.previewCaption(preview);
  assert.match(mount.innerHTML,/<pre[^>]*>.*识别 &lt;链接产品描述&gt;/s);

  input('link','url',{value:nextUrl});
  failGenerate=true; await context.generateLinkCaption(link);
  assert.match(mount.innerHTML,/无法读取产品页面/);
  assert.match(mount.innerHTML,/value="https:\/\/products\.example\.com\/another"/);
  assert.equal(row.captions.link.url,url);
  failGenerate=false; await context.generateLinkCaption(link);
  assert.deepEqual(lastBody('link/generate'),{record_id:id,url:nextUrl,revision:3});
  assert.equal(row.captions.link.enabled,true,'new URL recognition is included by default');
  console.log('Caption UI: media selection, link recognition, shared busy, draft persistence, candidate review, save and preview passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
