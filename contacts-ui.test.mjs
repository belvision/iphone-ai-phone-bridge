import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('./contacts-ui.js', import.meta.url), 'utf8');
const untrusted = '<img src=x onerror="globalThis.stolen=true">';
const contact = {id: 'contact-id/one', name: untrusted, phones: ['+375291112233', untrusted]};

function harness() {
  class Node {
    constructor(tag = 'div') {
      this.tagName = tag; this.children = []; this.textContent = ''; this.value = '';
      this.open = false; this.disabled = false; this.files = []; this.listeners = {};
    }
    set innerHTML(_) { throw new Error('Imported contacts must never be rendered as HTML'); }
    append(...nodes) { this.children.push(...nodes); }
    replaceChildren(...nodes) { this.children = nodes; }
    addEventListener(name, fn) { this.listeners[name] = fn; }
  }
  const nodes = new Map(), requests = [], windowEvents = {};
  const node = id => { if (!nodes.has(id)) nodes.set(id, new Node()); return nodes.get(id); };
  let list = async () => ({items: [contact], total: 1});
  let post = async path => path.startsWith('/crm/contacts/import') ? {added: 1, updated: 2, skipped: 3} : {linked: true};
  let crmRefreshes = 0;
  node('contactsSource').value = 'vcard';
  node('crmRefresh').onclick = async () => { crmRefreshes++; };
  const context = vm.createContext({
    console, AbortSignal, URLSearchParams,
    document: {getElementById: node, createElement: tag => new Node(tag)},
    window: {addEventListener(name, fn) { windowEvents[name] = fn; }},
    fetch: async (path, options = {}) => {
      requests.push({path, ...options});
      const result = options.method === 'POST' ? await post(path, options) : await list(path);
      return result?.httpError
        ? {ok: false, json: async () => ({error: result.httpError})}
        : {ok: true, json: async () => structuredClone(result)};
    },
  });
  vm.runInContext(source, context, {filename: 'contacts-ui.js'});
  return {context, node, requests, windowEvents,
    setList: fn => { list = fn; }, setPost: fn => { post = fn; },
    selected(id) { node('crmLinkTarget').value = id; node('crmLinkTarget').listeners.change(); },
    async open() { node('contacts').open = true; await context.refreshContacts(); },
    get crmRefreshes() { return crmRefreshes; },
    get bindButton() { return node('contactsRows').children[0].children[2].children[0]; },
  };
}
const flatten = node => node.textContent + node.children.map(flatten).join('');

{
  const h = harness();
  assert.equal(h.requests.length, 0, 'closed directory must not load contacts at startup');
  await h.context.refreshContacts();
  assert.equal(h.requests.length, 0);
  await h.open();
  assert.ok(flatten(h.node('contactsRows')).includes(untrusted));
  assert.equal(h.context.stolen, undefined);
  assert.equal(h.bindButton.disabled, true, 'a card must be selected before linking');
  await h.bindButton.onclick();
  assert.equal(h.requests.filter(request => request.method === 'POST').length, 0);
  h.selected('call/one');
  assert.equal(h.bindButton.disabled, false);
  await h.bindButton.onclick();
  const request = h.requests.find(request => request.method === 'POST');
  assert.equal(request.path, '/crm/calls/call%2Fone/contact');
  assert.deepEqual(JSON.parse(request.body), {contact_id: contact.id});
  assert.equal(h.crmRefreshes, 1);
  assert.match(h.node('contactsStatus').textContent, /Контакт привязан/);
  h.selected('');
  assert.equal(h.bindButton.disabled, true);
  const before = h.requests.length;
  await h.bindButton.onclick();
  assert.equal(h.requests.length, before, 'stale row button rechecks the selected call');
}

{
  const h = harness();
  await h.open();
  const bytes = Uint8Array.from([66, 69, 71, 73, 78, 58, 255, 0, 13, 10]);
  h.node('contactsFile').files = [{name: 'private-address-book.vcf', size: bytes.byteLength,
    arrayBuffer: async () => bytes.buffer}];
  h.node('contactsFile').value = 'private-address-book.vcf';
  h.node('contactsSource').value = 'google';
  await h.node('contactsImport').onclick();
  const request = h.requests.find(request => request.path.startsWith('/crm/contacts/import'));
  assert.equal(request.path, '/crm/contacts/import?source=google');
  assert.equal(request.headers['Content-Type'], 'text/vcard');
  assert.deepEqual(new Uint8Array(request.body), bytes, 'original bytes and charset must not be re-encoded');
  assert.equal(h.node('contactsFile').value, '');
  assert.match(h.node('contactsStatus').textContent, /добавлено 1, обновлено 2, пропущено 3/);
  assert.ok(!h.node('contactsStatus').textContent.includes('private-address-book'));
}

{
  const h = harness();
  await h.open();
  const before = h.requests.length;
  h.node('contactsFile').files = [{size: 16 * 1024 * 1024 + 1,
    arrayBuffer: async () => { throw new Error('oversized file must not be read'); }}];
  await h.node('contactsImport').onclick();
  assert.equal(h.requests.length, before);
  assert.match(h.node('contactsStatus').textContent, /16 МБ/);
}

{
  const h = harness();
  h.setList(async () => ({items: [contact], total: 101}));
  await h.open();
  let resolveList;
  h.setList(() => new Promise(resolve => { resolveList = resolve; }));
  const pending = h.node('contactsNext').onclick();
  assert.equal(h.node('contactsNext').disabled, true);
  await h.node('contactsNext').onclick();
  resolveList({items: [contact], total: 101});
  await pending;
  assert.match(h.requests.at(-1).path, /offset=50/);
  assert.equal(h.node('contactsCount').textContent, '51–100 из 101');
  h.setList(async () => ({items: [contact], total: 1}));
  h.node('contactsSearch').value = '+375 & "Имя"';
  await h.node('contactsSearchButton').onclick();
  const query = new URL(h.requests.at(-1).path, 'http://localhost').searchParams;
  assert.equal(query.get('q'), '+375 & "Имя"');
  assert.equal(query.get('offset'), '0');
}

{
  const h = harness();
  await h.open(); h.selected('call-one');
  h.setPost(async () => ({httpError: untrusted}));
  await h.bindButton.onclick();
  assert.equal(h.node('contactsStatus').textContent, untrusted);
  assert.equal(h.crmRefreshes, 0);
  assert.equal(h.bindButton.disabled, false);
  h.windowEvents.pagehide();
  const before = h.requests.length;
  await h.node('contactsSearchButton').onclick();
  assert.equal(h.requests.length, before);
}

console.log('PASS contacts UI: lazy local loading, inert text, exact selected-card link, original vCard bytes, upload bounds, pagination locking and safe errors; offline mocks only');
