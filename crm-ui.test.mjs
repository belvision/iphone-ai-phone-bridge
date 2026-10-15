import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('./crm-ui.js', import.meta.url), 'utf8');
const unsafe = '<img src=x onerror="globalThis.stolen=true">';
const record = (id = 'call-one') => ({
  id, mode: 'agents', started_at: '2026-10-15T09:00:00Z', duration_seconds: 42,
  state: 'finished', analysis_status: 'pending', summary: unsafe, next_step: unsafe,
  interest: 'unclear', name: unsafe, phone: '', telegram: '', notes: '', follow_up: '',
  status: 'new', transcript: [{role: 'user', text: unsafe}], analyzing: false,
});

function harness() {
  class Node {
    constructor(tag = 'div') {
      this.tagName = tag; this.children = []; this.textContent = ''; this.value = '';
      this.hidden = false; this.disabled = false; this.open = false; this.listeners = {};
    }
    set innerHTML(_) { throw new Error('Untrusted CRM content must never enter innerHTML'); }
    append(...nodes) { this.children.push(...nodes); }
    prepend(...nodes) { this.children.unshift(...nodes); }
    replaceChildren(...nodes) { this.children = nodes; }
    addEventListener(name, fn) { this.listeners[name] = fn; }
    scrollIntoView() {}
  }
  const nodes = new Map(), requests = [], rows = new Map();
  rows.set('call-one', record()); rows.set('call-two', record('call-two'));
  const node = id => { if (!nodes.has(id)) nodes.set(id, new Node()); return nodes.get(id); };
  let post = async (_path, changes) => ({...record(), ...changes});
  const context = vm.createContext({
    console, AbortSignal, setTimeout: () => 1, clearTimeout() {},
    document: {hidden: true, getElementById: node, createElement: tag => new Node(tag)},
    window: {addEventListener() {}},
    fetch: async (path, options = {}) => {
      const changes = options.body === undefined ? undefined : JSON.parse(options.body);
      requests.push({path, changes});
      const value = changes === undefined
        ? path.startsWith('/crm/calls?')
          ? {items: [...rows.values()], total: rows.size}
          : rows.get(decodeURIComponent(path.slice('/crm/calls/'.length)))
        : await post(path, changes);
      if (value?.httpError) return {ok: false, json: async () => ({error: value.httpError})};
      return {ok: true, json: async () => structuredClone(value)};
    },
  });
  vm.runInContext(source, context, {filename: 'crm-ui.js'});
  return {context, node, requests, rows, setPost: fn => { post = fn; },
    edit(id, value) { node(id).value = value; node(id).oninput(); }};
}
const flatten = node => node.textContent + node.children.map(flatten).join('');

{
  const h = harness();
  await h.node('crmRefresh').onclick();
  assert.ok(flatten(h.node('crmRows')).includes(unsafe));
  await h.context.openCard('call-one');
  assert.equal(h.node('crmName').value, unsafe);
  assert.equal(h.node('crmSummary').textContent, unsafe);
  assert.equal(h.node('crmTranscript').textContent, 'Абонент: ' + unsafe);
  assert.equal(h.context.stolen, undefined);
}

{
  const h = harness();
  // Analysis has not populated contacts yet. Editing a note must not mark blank
  // contacts as deliberate manual overrides in the database.
  h.rows.set('call-one', {...record(), name: ''});
  await h.context.openCard('call-one');
  h.edit('crmNotes', 'Уточнить детали');
  await h.node('crmSave').onclick();
  const save = h.requests.find(request => request.changes !== undefined);
  assert.deepEqual(save.changes, {notes: 'Уточнить детали'});
}

{
  const h = harness();
  await h.context.openCard('call-one');
  h.edit('crmNotes', 'Первая версия');
  let resolveSave;
  h.setPost(() => new Promise(resolve => { resolveSave = resolve; }));
  const pending = h.node('crmSave').onclick();
  h.edit('crmNotes', 'Новая версия во время сохранения');
  h.edit('crmPhone', '+375291112233');
  resolveSave(record());
  await pending;
  assert.equal(h.node('crmNotes').value, 'Новая версия во время сохранения');
  assert.equal(h.node('crmPhone').value, '+375291112233');
  await h.context.openCard('call-two');
  assert.match(h.node('crmDetailStatus').textContent, /Сначала сохраните/);
  assert.equal(h.node('crmNotes').value, 'Новая версия во время сохранения');
  await h.node('crmRefresh').onclick();
  assert.equal(h.node('crmNotes').value, 'Новая версия во время сохранения');
  h.setPost(async (_path, changes) => ({...record(), ...changes}));
  await h.node('crmSave').onclick();
  const saves = h.requests.filter(request => request.changes !== undefined);
  assert.deepEqual(saves[1].changes, {notes: 'Новая версия во время сохранения', phone: '+375291112233'});
}

{
  const h = harness();
  await h.context.openCard('call-one');
  h.edit('crmNotes', 'Не терять при ошибке');
  h.setPost(async () => ({httpError: unsafe}));
  await h.node('crmSave').onclick();
  assert.equal(h.node('crmError').textContent, unsafe);
  assert.equal(h.node('crmNotes').value, 'Не терять при ошибке');
  assert.equal(h.node('crmSave').disabled, false);
  await h.context.openCard('call-two');
  assert.match(h.node('crmDetailStatus').textContent, /Сначала сохраните/);
  assert.equal(h.node('crmNotes').value, 'Не терять при ошибке');
}

console.log('PASS CRM UI: untrusted text stays inert, partial updates preserve analysis fields, concurrent edits survive save/refresh, API errors retain drafts; offline mocks only');
