let data, accounts = [], dirty = false;
const accountPending = new Map(), accountFeedback = new Map();
const openingLoginAccounts = new Set(), openingAccountWindows = new Set();
let accountFetchSequence = 0, accountMarkup = '', accountAddPending = false;
const videoFolderFeedback = new Map(), videoApprovalErrors = new Map(), videoApprovalPending = new Set(), videoApprovalJobs = new Map();
const videoDeleteFeedback = new Map(), videoDeletePending = new Set();
let videoRows = [], videoFetchSequence = 0, videoMarkup = '';
const refreshFailures = new Map();
function refreshFailed(kind, error) {
  const detail = error.message === 'Failed to fetch' ? '服务连接暂时中断' : error.message;
  const notice = kind+'刷新暂未完成，将自动重试：'+detail;
  refreshFailures.set(kind,notice); msg(notice);
}
function refreshRecovered(kind) {
  const previous = refreshFailures.get(kind);
  refreshFailures.delete(kind);
  if (previous && document.querySelector('#log').textContent === previous) {
    msg(refreshFailures.size ? [...refreshFailures.values()].at(-1) : '连接已恢复');
  }
}
// The random token is supplied by the server-rendered same-origin document.
let csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || '';
async function api(path, body) {
  // Never forward the token to a caller-provided URL or another origin.
  if (typeof path !== 'string' || !/^\/api\/[a-z0-9_/-]+$/i.test(path)) {
    throw Error('无效的本地 API 路径');
  }
  const writing = body !== undefined;
  const formData = writing && body instanceof FormData;
  const request = () => {
    const headers = {};
    if (writing) {
      headers['X-CSRF-Token'] = csrfToken;
      if (!formData) headers['Content-Type'] = 'application/json';
    }
    // Safari sends Origin: null for same-origin POSTs under the document's
    // no-referrer policy. Keep origin information on these local API requests;
    // same-origin still suppresses Referer on any cross-origin redirect.
    return fetch(path, {method: writing ? 'POST' : 'GET',
      body: formData ? body : writing ? JSON.stringify(body) : undefined,
      headers, mode:'same-origin', credentials:'same-origin', referrerPolicy:'same-origin'});
  };
  const parse = async response => {
    try { return await response.json(); }
    catch { throw Error('服务响应异常或正在更新（HTTP '+(response.status || '未知')+'），请稍后重试'); }
  };
  let r = await request(), j = await parse(r);
  if (writing && r.status === 403 && j.code === 'csrf_invalid') {
    // The server rejects this code before the route runs. Only this specific
    // response is safe to retry after a service restart; other errors never retry.
    let next;
    try {
      const fresh = await fetch('/api/csrf', {method:'GET', cache:'no-store', mode:'same-origin', credentials:'same-origin', referrerPolicy:'same-origin'});
      const payload = await parse(fresh);
      if (!fresh.ok || typeof payload.token !== 'string' || !payload.token) throw Error('invalid token');
      next = payload.token;
    } catch {
      throw Error('安全令牌更新失败，请刷新页面后重试');
    }
    csrfToken = next;
    const meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) meta.content = next;
    r = await request(); j = await parse(r);
  }
  if (!r.ok) {
    if (r.status === 403 && j.code === 'csrf_invalid') throw Error('安全令牌已失效，请刷新页面后重试');
    throw Error(j.error || r.statusText);
  }
  return j;
}
function msg(x) { document.querySelector('#log').textContent = typeof x === 'string' ? x : JSON.stringify(x, null, 2); }
function esc(s) { return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
async function tab(t) {
  for (const x of ['studio','accounts','videos']) {
    document.querySelector('#'+x).style.display = t === x ? '' : 'none';
    document.querySelector('#nav'+x).classList.toggle('active', t === x);
  }
  try { if (t === 'accounts') await loadAccounts(); if (t === 'videos') await loadVideos(); }
  catch(e) { msg(e.message); }
}
function mediaUrl(path) { return '/media-file?path='+encodeURIComponent(path); }
async function refresh() {
  try {
    const next = await api('/api/state');
    refreshRecovered('工作台');
    document.querySelector('#status').textContent = next.running ? '● 正在运行' : next.returncode === null ? '● 已就绪' : next.returncode === 0 ? '● 运行完成' : '● 运行未完成，请查看日志';
    document.querySelector('#runlog').textContent = next.log || '暂无运行日志';
    if (dirty || document.activeElement?.closest('#rows')) return;
    data = next;
    document.querySelector('#rows').innerHTML = data.rows.map((x,i) => `<article class="row"><button class="btn red" data-record-id="${esc(x.record_id)}" onclick="deleteRow(this)">删除此记录</button><div class="grid"><label>keyword<input data-i="${i}" data-k="kw" value="${esc(x.keyword)}"></label><label>提示词<textarea data-i="${i}" data-k="prompt">${esc(x.prompt)}</textarea></label><div>视频 ${x.videos.length} 个</div><div>图片 ${x.images.length} 张<div class="thumbs">${x.images.map(im => `<img src="${mediaUrl(im)}">`).join('')}</div></div></div></article>`).join('') || '暂无素材记录';
  } catch(e) { refreshFailed('工作台',e); }
}
async function saveRows(show = true) {
  if (!data) { await refresh(); if (!data) throw Error('素材尚未加载，请稍后重试'); }
  const rows = data.rows.map((r,i) => ({record_id:r.record_id,
    keyword:document.querySelector(`[data-i="${i}"][data-k="kw"]`)?.value ?? r.keyword,
    prompt_text:document.querySelector(`[data-i="${i}"][data-k="prompt"]`)?.value ?? r.prompt}));
  const result = await api('/api/edit', {rows});
  dirty = false; if (show) msg('已保存 '+result.updated+' 条修改（运行时使用这些素材）');
  await refresh(); return result;
}
let syncPending = false;
async function syncFeishu() {
  if (syncPending) return;
  const button = document.querySelector('[onclick="syncFeishu()"]');
  const status = document.querySelector('#sync-result');
  const show = text => { if (status) status.textContent = text; msg(text); };
  if (dirty) { show('请先保存本地修改，再同步飞书'); return; }
  syncPending = true;
  if (button) { button.disabled = true; button.textContent = '同步中…'; }
  show('正在连接飞书并下载素材，请稍候…');
  try {
    const result = await api('/api/sync', {});
    show(`同步完成：产品 ${result.products ?? 0} 条，素材 ${result.sources ?? 0} 条。`);
    await refresh();
  } catch(e) {
    const detail = e.message === 'Failed to fetch' ? '无法连接工作台，请确认服务已启动' : e.message;
    show('同步失败：' + detail);
  } finally {
    syncPending = false;
    if (button) { button.disabled = false; button.textContent = '同步飞书'; }
  }
}
async function upload() {
  const f = new FormData(); f.append('keyword',document.querySelector('#newkw').value); f.append('prompt',document.querySelector('#newprompt').value);
  for (const x of document.querySelector('#newvid').files) f.append('videos',x);
  for (const x of document.querySelector('#newimg').files) f.append('images',x);
  try { msg(await api('/api/upload',f)); await refresh(); } catch(e) { msg(e.message); }
}
async function run() {
  try {
    const current = await api('/api/state');
    const pendingEdits = dirty;
    if (!current.can_resume) await saveRows(false);
    const result = await api('/api/run',{});
    msg((result.message || (result.resumed ? '继续上次'+result.script_count+'个脚本，已完成视频不重复生成' : '已启动新任务'))
      + (current.can_resume && pendingEdits ? '。未保存修改保留供后续新任务' : ''));
    await refresh();
  } catch(e) { msg('未启动：'+e.message); }
}
async function stop() { try { msg(await api('/api/stop',{})); await refresh(); } catch(e) { msg(e.message); } }
async function previewClearHistory() {
  try {
    const result = await api('/api/history/preview');
    msg('历史清理仅提供预览，未删除记录或文件。\n' + JSON.stringify(result, null, 2));
  } catch (e) { msg('历史清理预览失败：' + e.message); }
}
async function deleteRow(target) { const id = typeof target === 'string' ? target : target.dataset.recordId; if (!confirm('从当前工作台移除此记录？飞书记录会在下次同步时恢复。')) return; try { msg(await api('/api/rows/delete',{record_id:id})); dirty=false; await refresh(); } catch(e) { msg(e.message); } }
const accountLabels = {available:'已登录，可运行', busy:'运行中', checking:'正在验证登录', needs_login:'待登录', exhausted:'今日额度耗尽', disabled:'不可用', opening:'正在打开浏览器', error:'浏览器启动失败'};
const accountWindowLabels = {opening:'正在打开账号窗口…', open:'账号窗口已打开', error:'账号窗口启动失败，请重试并查看启动日志'};
async function loadAccounts() {
  const sequence = ++accountFetchSequence;
  const result = await api('/api/accounts');
  if (sequence !== accountFetchSequence) return;
  refreshRecovered('账号');
  const priorWindows = new Map(accounts.filter(a => a.account_window_open).map(a => [a.name,a.account_window_status]));
  accounts = result.accounts || [];
  for (const a of accounts) {
    const status = a.account_window_status;
    const finishedOpening = openingAccountWindows.has(a.name) && status !== 'opening';
    const windowEnded = priorWindows.has(a.name) && !accountPending.has(a.name) && status !== 'opening'
      && (!a.account_window_open || (status === 'error' && priorWindows.get(a.name) !== 'error'));
    if (finishedOpening || windowEnded) {
      openingAccountWindows.delete(a.name);
      const previous = accountFeedback.get(a.name);
      const message = a.account_window_note || accountWindowLabels[status] || '账号窗口已关闭';
      accountFeedback.set(a.name,message);
      if (finishedOpening || document.querySelector('#log').textContent === previous) msg(message);
    }
    if (!openingLoginAccounts.has(a.name)) continue;
    let message;
    if (!a.login_window_open && a.status === 'error') message = '登录窗口启动失败：'+(a.note || '请重试并查看启动日志');
    else if (a.login_window_open && a.login_status !== 'opening') message = a.name+' 登录窗口已打开，请手动登录';
    else if (!a.login_window_open && a.status === 'available') message = a.name+' 登录已验证，账号配置已保存';
    if (message) {
      openingLoginAccounts.delete(a.name); accountFeedback.set(a.name,message); msg(message);
    }
  }
  renderAccounts();
}
function accountQuota(a) {
  const q = a.quota;
  return q && ['limit','used','reserved','unknown','remaining'].every(k => Number.isInteger(q[k]) && q[k] >= 0) && q.limit > 0 ? q : null;
}
function renderAccounts() {
  const markup = accounts.map(a => {
    const q = accountQuota(a), pending = accountPending.get(a.name);
    let label = accountLabels[a.status] || a.status;
    if (a.status === 'available' && a.enabled === false) label = '已停用';
    else if (a.status === 'available' && q?.remaining === 0) label = '已登录，今日次数已用完';
    const quota = q ? `<p class="account-quota"><strong>今日已使用 ${q.used}/${q.limit}</strong> · 今日剩余 ${q.remaining} 次 · 预留 ${q.reserved} · 结果未知 ${q.unknown}</p><p class="muted">${esc(q.date || '')} · 按北京时间每日重置 · ${q.estimated === false ? '已按平台显示校准（非实时）' : '本地估算，平台余额待核验'}${q.remaining === 0 ? ' · 剩余为0，后续任务自动跳过' : ''}${q.unknown ? ' · 结果未知的次数暂占余额' : ''}${q.blocked_reason ? ' · '+esc(q.blocked_reason) : ''}</p>` : '<p class="muted account-quota">每日次数统计暂不可用</p>';
    const login = a.login_window_open ? `<p class="muted">登录窗口：${esc(a.login_note || accountLabels[a.login_status] || '等待登录验证')}</p>` : '';
    const name = esc(a.name), openingWindow = a.account_window_status === 'opening';
    const accountWindow = a.account_window_open || openingWindow;
    const feedback = pending || accountFeedback.get(a.name) || a.account_window_note || accountWindowLabels[a.account_window_status] || '';
    return `<article class="row"><b>${name}</b> · 配置已保存 · ${esc(label)}<p class="muted">${esc(a.note || '')}</p>${quota}${login}<div class="actions">
      <button class="btn gray" data-name="${name}" onclick="openAccount(this)"${pending || a.login_window_open || accountWindow ? ' disabled' : ''}>打开登录窗口</button>
      <button class="btn gray" data-name="${name}" onclick="visitAccount(this)"${pending || a.login_window_open || openingWindow ? ' disabled' : ''}>打开账号</button>
      <button class="btn gray" data-name="${name}" onclick="confirmAccount(this)"${pending ? ' disabled' : ''}>检查登录状态</button>
      <button class="btn gray" data-name="${name}" onclick="closeAccount(this)"${pending ? ' disabled' : ''}>${accountWindow ? '关闭账号窗口' : '关闭登录窗口'}</button>
      <button class="btn red" data-name="${name}" onclick="deleteAccount(this)"${pending ? ' disabled' : ''}>永久删除</button>
      </div><p class="muted account-result" role="status">${esc(feedback)}</p></article>`;
  }).join('') || '暂无豆包账号';
  if (markup !== accountMarkup) {
    document.querySelector('#accountRows').innerHTML = markup;
    accountMarkup = markup;
  }
}
async function addAccount() {
  if (accountAddPending) return;
  const button = document.querySelector('[onclick="addAccount()"]');
  accountAddPending = true; ++accountFetchSequence;
  if (button) button.disabled = true;
  try {
    const result = await api('/api/accounts/add',{});
    if (result.status === 'opening') openingLoginAccounts.add(result.name);
    msg(result.message || result.name+' 配置已保存，浏览器正在启动，请稍候查看账号状态。');
    await loadAccounts();
  } catch(e) {
    msg('登录窗口未能打开：'+e.message);
    // A failed browser launch still preserves the new account/Profile for retry.
    try { await loadAccounts(); } catch(refreshError) { refreshFailed('账号',refreshError); }
  }
  finally { accountAddPending = false; if (button) button.disabled = false; }
}
function accountName(target) { return typeof target === 'string' ? target : target.dataset.name; }
async function accountAction(target, action, pendingText, fallbackMessage) {
  const name = accountName(target);
  if (accountPending.has(name)) return;
  accountPending.set(name,pendingText); accountFeedback.delete(name); ++accountFetchSequence; renderAccounts();
  try {
    const result = await api('/api/accounts/'+action,{name});
    if (action === 'open' && result.status === 'opening') openingLoginAccounts.add(name);
    if (action === 'visit' && result.status === 'opening') openingAccountWindows.add(name);
    if (action === 'close' || action === 'delete') openingAccountWindows.delete(name);
    const message = result.message || (action === 'visit' ? accountWindowLabels[result.status] : '') || fallbackMessage;
    accountFeedback.set(name,message); msg(message);
    if (action === 'delete') { accounts = accounts.filter(a => a.name !== name); accountFeedback.delete(name); }
    await loadAccounts();
  } catch(e) { accountFeedback.set(name,e.message); msg(e.message); }
  finally { ++accountFetchSequence; accountPending.delete(name); renderAccounts(); }
}
async function openAccount(target) { await accountAction(target,'open','正在打开登录窗口…','登录窗口已打开，请完成登录并等待自动验证'); }
async function visitAccount(target) { await accountAction(target,'visit','正在打开账号窗口…','账号窗口已打开'); }
async function confirmAccount(target) { await accountAction(target,'confirm','正在检查登录状态…','账号状态已检查'); }
async function closeAccount(target) {
  const account = accounts.find(a => a.name === accountName(target));
  const windowLabel = account?.account_window_open || account?.account_window_status === 'opening' ? '账号窗口' : '登录窗口';
  await accountAction(target,'close',`正在关闭${windowLabel}…`,`${windowLabel}已关闭`);
}
async function deleteAccount(target) {
  const name = accountName(target);
  if (accountPending.has(name) || !confirm('永久删除账号 '+name+' 的磁盘配置？此操作无法撤销。')) return;
  await accountAction(target,'delete','正在删除账号配置…','账号配置已删除');
}
async function loadVideos() {
  const sequence = ++videoFetchSequence;
  const d = await api('/api/videos');
  if (sequence !== videoFetchSequence) return;
  refreshRecovered('视频');
  videoRows = d.videos || [];
  for (const v of videoRows) videoApprovalJobs.delete(v.path);
  renderVideos();
}
function approvalText(job) {
  if (!job) return '';
  if (job.status === 'done') return '处理完成 · 已去水印并提升至1080P';
  if (job.status === 'failed') return '处理失败：' + (job.error || '请查看日志后重试');
  const progress = Number.isFinite(Number(job.progress)) ? Math.max(0,Math.min(100,Math.round(Number(job.progress)))) : 0;
  return (job.stage || (job.status === 'queued' ? '已审核通过，等待处理' : '正在处理')) + ' · ' + progress + '%';
}
function renderVideos() {
  const markup = videoRows.map(v => {
    const job = videoApprovalJobs.get(v.path) || v.postprocess;
    const submitting = videoApprovalPending.has(v.path);
    const active = submitting || job?.status === 'queued' || job?.status === 'running';
    const complete = job?.status === 'done';
    const deleting = videoDeletePending.has(v.path);
    const folder = videoFolderFeedback.get(v.path) || {};
    const label = submitting ? '正在提交…' : active ? '处理中…' : complete ? '处理完成' : job?.status === 'failed' ? '重试处理' : '审核通过';
    const status = videoApprovalErrors.get(v.path) || (submitting ? '正在提交审核并启动自动处理…' : approvalText(job));
    const output = complete && job.output_path ? `<a class="btn" target="_blank" rel="noopener" href="/media-video?path=${encodeURIComponent(job.output_path)}">查看1080P成片</a>` : '';
    const failureLog = job?.status === 'failed' && job.log_tail ? `<details><summary>查看处理日志</summary><pre class="log">${esc(job.log_tail)}</pre></details>` : '';
    const deletion = `<button class="btn red" data-path="${esc(v.path)}" onclick="deleteVideo(this)" title="${active ? '处理中，暂不能删除' : '永久删除该视频文件'}"${active || deleting ? ' disabled' : ''}>${deleting ? '正在删除…' : '删除'}</button>${active ? '<span class="muted">处理中，暂不能删除</span>' : ''}`;
    return `<article class="row"><b title="${esc(v.name)}">${esc(v.label || v.name)}</b> ${esc(v.size)}<div class="actions"><a class="btn" target="_blank" rel="noopener" href="/media-video?path=${encodeURIComponent(v.path)}">打开文件</a><button class="btn gray" data-path="${esc(v.path)}" onclick="openFolder(this)"${folder.pending || deleting ? ' disabled' : ''}>打开文件夹</button><button class="btn" data-path="${esc(v.path)}" onclick="approveVideo(this)"${active || complete || deleting ? ' disabled' : ''}>${label}</button>${output}${deletion}</div><p class="muted folder-result" role="status">${esc(folder.text || '')}</p><p class="muted approval-result" role="status">${esc(status)}</p><p class="muted delete-result" role="status">${esc(videoDeleteFeedback.get(v.path) || '')}</p>${failureLog}</article>`;
  }).join('') || '暂无视频';
  if (markup !== videoMarkup) {
    document.querySelector('#videoRows').innerHTML = markup;
    videoMarkup = markup;
  }
}
async function approveVideo(button) {
  const path = button.dataset.path;
  if (videoApprovalPending.has(path) || videoDeletePending.has(path)) return;
  const status = button.closest('.row').querySelector('.approval-result');
  videoApprovalPending.add(path); videoApprovalErrors.delete(path); ++videoFetchSequence;
  button.disabled = true; status.textContent = '正在提交审核并启动自动处理…';
  try {
    const result = await api('/api/videos/approve',{path});
    videoApprovalJobs.set(path,result.job || {status:'queued',stage:'已审核通过，等待处理',progress:0});
    status.textContent = approvalText(videoApprovalJobs.get(path)); msg(status.textContent);
  } catch(e) {
    videoApprovalErrors.set(path,'启动处理失败：'+e.message);
    status.textContent = videoApprovalErrors.get(path); msg(status.textContent);
  } finally {
    ++videoFetchSequence; videoApprovalPending.delete(path); button.disabled = false;
    renderVideos();
  }
}
async function deleteVideo(button) {
  const path = button.dataset.path;
  if (videoDeletePending.has(path)) return;
  const status = button.closest('.row').querySelector('.delete-result');
  const job = videoApprovalJobs.get(path) || videoRows.find(v => v.path === path)?.postprocess;
  if (videoApprovalPending.has(path) || job?.status === 'queued' || job?.status === 'running') {
    videoDeleteFeedback.set(path,'处理中，暂不能删除');
    status.textContent = videoDeleteFeedback.get(path); renderVideos(); return;
  }
  if (!confirm('永久删除这条视频的原始 MP4 文件？\n'+path+'\n\n磁盘删除无法撤销。')) return;
  videoDeletePending.add(path); ++videoFetchSequence;
  videoDeleteFeedback.set(path,'正在删除磁盘文件…');
  button.disabled = true; status.textContent = videoDeleteFeedback.get(path);
  let deleted = false;
  try {
    const result = await api('/api/videos/delete',{path});
    deleted = true;
    videoRows = videoRows.filter(v => v.path !== path);
    for (const map of [videoFolderFeedback,videoApprovalErrors,videoApprovalJobs,videoDeleteFeedback]) map.delete(path);
    msg(result.message || '已删除该视频文件');
  } catch(e) {
    videoDeleteFeedback.set(path,'删除失败：'+e.message);
    status.textContent = videoDeleteFeedback.get(path); msg(status.textContent);
  } finally {
    ++videoFetchSequence; videoDeletePending.delete(path); button.disabled = false; renderVideos();
  }
  if (deleted) {
    try { await loadVideos(); }
    catch(e) { msg('视频已删除，列表刷新暂未完成：'+e.message); }
  }
}
async function openFolder(button) {
  const path = button.dataset.path;
  const status = button.closest('.row').querySelector('.folder-result');
  button.disabled = true; status.textContent = '正在打开文件夹…';
  videoFolderFeedback.set(path,{text:status.textContent,pending:true});
  try {
    const result = await api('/api/open-video',{path});
    status.textContent = result.message + '：' + result.folder; msg(status.textContent);
  } catch(e) { status.textContent = e.message; msg(e.message); }
  finally { videoFolderFeedback.set(path,{text:status.textContent,pending:false}); button.disabled = false; }
}
document.querySelector('#rows').addEventListener('input',() => { dirty = true; });
document.querySelector('[onclick="saveRows()"]').onclick = () => saveRows().catch(e => msg('保存失败：'+e.message));
refresh();
setInterval(() => { refresh(); if (document.querySelector('#accounts').style.display !== 'none') loadAccounts().catch(e => refreshFailed('账号',e)); if (document.querySelector('#videos').style.display !== 'none') loadVideos().catch(e => refreshFailed('视频',e)); },4000);
