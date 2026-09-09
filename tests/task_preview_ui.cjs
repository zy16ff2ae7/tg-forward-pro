const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, { textContent: '', className: '', disabled: false,
    classList: { add() {}, remove() {}, contains() { return false; } }, focus() {} });
  return elements.get(id);
}
const context = vm.createContext({ window: {}, location: { search: '?demo=1' }, URLSearchParams,
  document: { getElementById: element }, console, setTimeout, clearTimeout });
const source = fs.readFileSync(path.join(__dirname, '../webapp/app.js'), 'utf8');
const markup = fs.readFileSync(path.join(__dirname, '../webapp/index.html'), 'utf8');
vm.runInContext(source.replace(/boot\(\);\s*$/, ''), context);
const run = code => vm.runInContext(code, context);
(async () => {
  run("state.me = {subscription: {active: true}}");
  assert.equal(run("taskBadge({enabled:true, paused_until:new Date(Date.now()+60000).toISOString()}).label"), 'ограничен');
  assert.equal(run("taskBadge({enabled:true, window_opens_at:new Date(Date.now()+60000).toISOString()}).label"), 'ждёт окна');
  assert.equal(run("taskBadge({enabled:false, paused_until:new Date(Date.now()+60000).toISOString()}).label"), 'пауза');
  assert.equal(run("taskBadge({enabled:true, account_online:false}).label"), 'нет связи');
  assert.equal(run("taskBadge({enabled:true, mailing:{messages_gone:2,messages_count:0}}).label"), 'нет сообщений');
  assert.equal(run("taskBadge({enabled:true, oneshot:true, join_queue:{state:'running'}}).label"), 'вступает');
  assert.match(run("taskPauseButton({id:42, oneshot:true, join_queue:{state:'running'}})"), /data-action="stop"/);
  assert.match(run("joinQueueHtml({state:'stopped', remaining:2, items:{'<script>':{status:'requested'}}})"), /&lt;script&gt;/);
  assert.match(run("joinQueueHtml({state:'stopped', remaining:2})"), /Осталось минимум 2/);
  assert.equal(run("taskBadge({enabled:true, warmup:{state:'scheduled'}}).label"), 'по плану');
  assert.equal(run("taskBadge({enabled:false, warmup:{state:'review'}}).label"), 'проверьте шаг');
  assert(!source.includes('profile-account'));
  assert.match(markup, /id="warmupStories" type="checkbox">/);
  assert.doesNotMatch(markup, /id="warmupStories" type="checkbox" checked/);
  assert.match(run("journalRowHtml({status:'info', error:'Шаг запланирован', created_at:'2026-09-09T10:00:00Z'}, 'warmup')"), /Ход выполнения/);
  assert.match(run("journalRowHtml({status:'ok', error:'Описание заполнено', created_at:'2026-09-09T10:00:00Z'}, 'warmup')"), /Описание заполнено/);
  assert.match(run("warmupStepsHtml([{label:'История',status:'waiting',planned_at:'2026-09-09T10:00:00Z',at:'2026-09-09T11:00:00Z',attempts:1,note:'Premium required'}])"), /Следующая попытка/);
  const count = run('DEMO_STATE.tasks.length');
  const review = run("reviewTaskBeforeStart({command:'mailing', targets:['@test'], message:'Текст'})");
  await new Promise(setImmediate);
  assert.equal(element('confirmTitle').textContent, 'Проверка перед запуском');
  assert.equal(element('confirmOk').textContent, 'Запустить');
  assert.match(element('confirmText').textContent, /Текст/);
  run('resolveConfirm(false)');
  assert.equal(await review, false);
  assert.equal(run('DEMO_STATE.tasks.length'), count);
  run(`
    state.activeCommand = {id:'copy_channel', kind:'forward'};
    collectTaskPayload = () => ({body:{command:'copy_channel', account_id:1, source:'@source', target:'@target'}});
    withLoading = async (button, action) => action();
    discardTaskDraft = () => {};
    closeSheets = () => {};
    toast = () => {};
    refreshAllTaskLists = async () => {};
    switchTab = () => {};
    document.querySelectorAll = () => [];
  `);
  const cancelled = run('submitTask()');
  await new Promise(setImmediate);
  run('resolveConfirm(false)');
  await cancelled;
  assert.equal(run('DEMO_STATE.tasks.length'), count);
  const confirmed = run('submitTask()');
  await new Promise(setImmediate);
  run('resolveConfirm(true)');
  await confirmed;
  assert.equal(run('DEMO_STATE.tasks.length'), count + 1);
  console.log('UI checks passed: statuses, preview, cancellation, confirmed creation');
})().catch(error => { console.error(error); process.exitCode = 1; });
