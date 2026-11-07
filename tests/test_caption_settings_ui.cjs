// Settings dialog regression with fixture DOM and same-origin responses.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

function element() {
  return {value:'',textContent:'',innerHTML:'',style:{},disabled:false,checked:false,events:{},attributes:{},
    addEventListener(name,handler){this.events[name]=handler;},
    setAttribute(name,value){this.attributes[name]=value;}};
}
function lookup() {
  const elements = new Map();
  return name => {
    if (!elements.has(name)) elements.set(name,element());
    return elements.get(name);
  };
}
const page = lookup(), field = lookup(), inDialog = lookup(), dialog = element();
dialog.querySelector = inDialog;
dialog.showModal = () => { dialog.open=true; };
dialog.close = () => { dialog.open=false; dialog.events.close(); };
inDialog('form').elements = {namedItem:field};
const saved = {base_url:'https://api.example.com/v1',model:'vision-fixture',api_key_configured:true,
  api_key_mask:'****',mode:'cloud',image_prompt:'<图片提示>',product_video_prompt:'商品提示',reference_video_prompt:'创意提示'};
const writes = [];
let appended = 0, failLoad = false, holdSave = false, releaseSave, failSave = false;
const ok = settings => ({ok:true,json:async()=>structuredClone(settings)});
const error = message => ({ok:false,status:503,json:async()=>({error:message})});
const context = vm.createContext({document:{querySelector:page,activeElement:null,
  createElement:tag=>{assert.equal(tag,'dialog');return dialog;},body:{append(node){assert.equal(node,dialog);appended++;}}},
  FormData:class{},console,setInterval(){},
  fetch:async(path,options={})=>{
    if (path === '/api/state') return ok({rows:[]});
    assert.equal(path,'/api/captions/settings');
    if (options.method === 'GET') return failLoad ? error('配置暂不可读取') : ok(saved);
    const payload = JSON.parse(options.body);
    writes.push(payload);
    if (holdSave) { holdSave=false; await new Promise(resolve=>releaseSave=resolve); }
    if (failSave) return error(`写入失败 ${payload.api_key}`);
    const {api_key,clear_api_key,...settings} = payload;
    Object.assign(saved,settings);
    if (api_key || clear_api_key) saved.api_key_configured=Boolean(api_key);
    saved.api_key_mask=saved.api_key_configured?'****':'';
    saved.mode=saved.base_url&&saved.model&&saved.api_key_configured?'cloud':'local';
    return ok(saved);
  },
});
page('meta[name="csrf-token"]').content='test-csrf';
vm.runInContext(fs.readFileSync('dashboard_ui.js','utf8'),context);
const form = inDialog('form'), feedback = inDialog('[data-model-feedback]'), mode = inDialog('[data-model-mode]');
const close = inDialog('[data-model-close]'), keyNote = inDialog('#caption-model-key-note');
const submit = () => form.events.submit({currentTarget:form,preventDefault(){}});

(async()=>{
  await context.openCaptionModelSettings();
  assert.equal(dialog.open,true);
  assert.equal(dialog.attributes['aria-labelledby'],'caption-model-title');
  assert.match(dialog.innerHTML,/type="password"/);
  assert.match(dialog.innerHTML,/视频抽帧发送至你配置的 HTTPS OpenAI 兼容 API/);
  assert.equal(field('image_prompt').value,'<图片提示>');
  assert.equal(field('api_key').value,'');
  assert.match(keyNote.textContent,/\*\*\*\*/);
  assert.match(mode.textContent,/云端识别/);

  field('base_url').value=' HTTPS://API.EXAMPLE.COM/v1/ ';
  field('image_prompt').value='自定义图片提示';
  field('product_video_prompt').value='自定义实拍提示';
  field('reference_video_prompt').value='自定义参考提示';
  await submit();
  assert.deepEqual(writes.at(-1),{base_url:'https://api.example.com/v1',model:'vision-fixture',
    image_prompt:'自定义图片提示',product_video_prompt:'自定义实拍提示',reference_video_prompt:'自定义参考提示'});
  assert.match(feedback.textContent,/已保存/);

  field('base_url').value='https://other.example.com/v1';
  await submit();
  assert.equal(writes.length,1,'a blank password must not reuse the key at a different URL');
  assert.match(feedback.textContent,/填写新 API Key/);

  field('api_key').value='replacement-fixture';
  failSave=true;
  await submit();
  assert.match(feedback.textContent,/保存设置失败/);
  assert.doesNotMatch(feedback.textContent,/replacement-fixture/);
  assert.equal(inDialog('fieldset').disabled,false);
  failSave=false;
  holdSave=true;
  const saving = submit();
  await submit();
  assert.equal(writes.length,3,'same settings must not save twice while pending');
  assert.match(feedback.textContent,/正在保存/);
  assert.equal(close.disabled,true);
  let prevented = false;
  dialog.events.cancel({preventDefault(){prevented=true;}});
  assert.equal(prevented,true);
  releaseSave(); await saving;
  assert.equal(writes.at(-1).api_key,'replacement-fixture');
  assert.equal(field('api_key').value,'');

  field('clear_api_key').checked=true;
  field('clear_api_key').events.change();
  assert.equal(field('api_key').disabled,true);
  field('base_url').value='';
  await submit();
  assert.equal(writes.at(-1).clear_api_key,true);
  assert.ok(!Object.hasOwn(writes.at(-1),'api_key'));
  assert.match(mode.textContent,/本地分析/);
  assert.match(keyNote.textContent,/尚未保存/);
  field('api_key').value='discard-fixture';
  close.events.click();
  assert.equal(dialog.open,false);
  assert.equal(field('api_key').value,'');

  failLoad=true; await context.openCaptionModelSettings();
  assert.match(feedback.textContent,/读取设置失败.*配置暂不可读取/);
  assert.equal(inDialog('fieldset').disabled,true);
  close.events.click();
  failLoad=false; await context.openCaptionModelSettings();
  assert.equal(appended,1);
  assert.equal(field('reference_video_prompt').value,'自定义参考提示');
  assert.equal(field('api_key').value,'');
  console.log('Caption model settings UI: masked key, provider binding, explicit clear, persisted prompts, load/save errors and dialog lifecycle passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
