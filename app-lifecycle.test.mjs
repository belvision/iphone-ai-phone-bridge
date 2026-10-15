// Offline integration checks of the actual app and gate. No browser, microphone,
// phone APIs, Retell SDK, network access, credentials, or billable calls are used.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';

const [appSource, gateSource, html] = await Promise.all([
  readFile(new URL('./app-source.js', import.meta.url), 'utf8'),
  readFile(new URL('./auto-call.js', import.meta.url), 'utf8'),
  readFile(new URL('./index.html', import.meta.url), 'utf8'),
]);
const phone = (callPhase, callSequence = 0, extra = {}) => ({
  available: true, active: callPhase === 'connected', callPhase, callSequence,
  callDirection: 'outgoing', audioOnComputer: callPhase === 'connected', ageMs: 10, ...extra,
});
const defer = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const settle = async () => {
  // Drain promise-only app work without advancing its polling/countdown timers.
  for (let i = 0; i < 30; i++) await Promise.resolve();
  await new Promise(resolve => setImmediate(resolve));
};

async function harness(initial = phone('idle'), { virtualInstalled = true, prepareBlock = null, savedConnection = null, loadBlock = null } = {}) {
  const elements = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(([, id]) => [id, {
    id, value: '', checked: false, textContent: '', disabled: false, hidden: false,
    open: false, dataset: {}, focus() { this.focused = true; },
  }]));
  const element = id => {
    assert(elements.has(id), `app requested missing DOM element #${id}`);
    return elements.get(id);
  };
  element('key').value = 'public_key_offline_test_only';
  element('agent').value = 'agent_offline_test_only';
  element('confirmed').checked = true;
  element('audioMode').value = 'direct-hfp';
  const routeCommands = [];
  const savedConnections = [];
  let virtualPrepared = false;
  const calls = [], bridges = [], timers = new Map(), listeners = new Map();
  let snapshot = initial, timerId = 0, now = 10000, nextBridgeOpenBlock;
  class TestDate extends Date {
    constructor(...args) { super(...(args.length ? args : [now])); }
    static now() { return now; }
  }
  const context = vm.createContext({
    document: { getElementById: element },
    window: { addEventListener: (name, fn) => listeners.set(name, fn) },
    AbortController, AbortSignal, Date: TestDate,
    console: { log() {}, info() {}, warn() {}, error() {} },
    setTimeout: (fn, delay) => { const id = ++timerId; timers.set(id, { fn, delay }); return id; },
    clearTimeout: id => timers.delete(id),
    setInterval: (fn, delay) => { const id = ++timerId; timers.set(id, { fn, delay, interval: true }); return id; },
    clearInterval: id => timers.delete(id),
    fetch: async (url, options) => {
      if (url === '/network-status') return { ok: true, json: async () => ({ proxyEnabled: true, country: 'US' }) };
      if (url === '/connection') {
        if (options?.method === 'POST') {
          savedConnections.push(JSON.parse(options.body));
          return { ok: true, json: async () => ({ saved: true }) };
        }
        if (loadBlock) await loadBlock.promise;
        return { ok: true, json: async () => ({ saved: !!savedConnection, ...(savedConnection || {}) }) };
      }
      if (url === '/audio-routes') return { ok: true, json: async () => ({ virtualMicInstalled: virtualInstalled, virtualMicPrepared: virtualPrepared }) };
      if (url === '/audio-route') {
        const command = JSON.parse(options.body); routeCommands.push(command);
        if (command.action === 'prepare') {
          if (prepareBlock) await prepareBlock.promise;
          if (!virtualInstalled) return { ok: false, json: async () => ({ error: 'VB-CABLE не установлен.' }) };
          virtualPrepared = true;
        } else if (command.action === 'reset') virtualPrepared = false;
        return { ok: true, json: async () => ({ virtualMicInstalled: virtualInstalled, virtualMicPrepared: virtualPrepared }) };
      }
      assert.equal(url, '/phone-state', 'the app test must never access an external service');
      const value = snapshot === null ? null : { ...snapshot };
      return { ok: true, json: async () => value };
    },
    startNativeBridge: async hooks => {
      const bridge = {
        outputMode: hooks.outputMode,
        closed: false, closeReason: undefined, stopCalls: 0, stream: { offline: true },
        tone() {},
        emitClose(reason, beforeClose) {
          if (this.closed) return;
          this.closed = true; this.closeReason = reason;
          beforeClose?.();
          hooks.onClose(reason);
        },
        async stop() {
          this.stopCalls++;
          this.emitClose('Аудиомост остановлен');
        },
      };
      bridges.push(bridge);
      const block = nextBridgeOpenBlock;
      nextBridgeOpenBlock = null;
      if (block) await block.promise;
      return bridge;
    },
    createNativeRetellCall: options => {
      assert.equal(options.key, 'public_key_offline_test_only');
      assert.equal(options.agentId, 'agent_offline_test_only');
      const session = {
        status: 'live', hooks: options.hooks, endCalls: 0, endEmitted: false, endBlock: null,
        emitEnd() {
          this.status = 'ended'; this.endEmitted = true;
          this.hooks.onEnd();
        },
        async end() {
          this.endCalls++;
          if (!this.endEmitted) this.emitEnd();
          if (this.endBlock) await this.endBlock.promise;
        },
      };
      calls.push(session);
      return session;
    },
  });
  // Imports are replaced with the controlled dependencies above. Production
  // functions and the real AutoCallGate remain otherwise unchanged.
  const executable = gateSource.replace(/^export\s+/gm, '') + '\n'
    + appSource.replace(/^import\s+.*?;\r?\n/gm, '') + '\n'
    + `globalThis.__app = {
      observe: observePhone,
      stop: stopAll,
      state: () => ({mode: gate.mode, automatic: gate.automatic, sequence: gate.sequence,
        call, bridge, stopping, pending, operation})
    };`;
  vm.runInContext(executable, context, { filename: 'app-under-test.js' });
  await settle();
  return {
    calls, bridges, element, routeCommands, savedConnections,
    state: () => context.__app.state(),
    async click(id) {
      assert.equal(element(id).disabled, false, `#${id} must be available to the user`);
      await element(id).onclick(); await settle();
    },
    async observe(value) { snapshot = value; context.__app.observe(value); await settle(); },
    async end(session = calls.at(-1)) { session.emitEnd(); await settle(); },
    async error(session = calls.at(-1), message = 'offline simulated fatal failure') {
      session.hooks.onError(new Error(message)); await settle();
    },
    async closeBridge(reason, retellFirst = true) {
      const session = calls.at(-1);
      bridges.at(-1).emitClose(reason, retellFirst ? () => session.emitEnd() : undefined);
      if (!retellFirst) session.emitEnd();
      await settle();
    },
    deferNextBridge() { nextBridgeOpenBlock = defer(); return nextBridgeOpenBlock; },
    async input(id, value) {
      element(id).value = value; element(id).oninput();
      await settle();
    },
    async runDelay(delay) {
      for (const [id, timer] of [...timers]) if (timer.delay === delay && !timer.interval) { timers.delete(id); timer.fn(); }
      await settle();
    },
    async tickIntervals() {
      for (const timer of [...timers.values()]) if (timer.interval) timer.fn();
      await settle();
    },
    advance(ms) { now += ms; },
    async close() { await context.__app.stop(); listeners.get('pagehide')?.(); await settle(); },
  };
}

async function running() {
  const h = await harness();
  await h.click('arm');
  await h.observe(phone('connected', 1));
  assert.equal(h.calls.length, 1, 'answered outgoing call starts one mocked session');
  assert.equal(h.state().mode, 'running');
  return h;
}

// A single explicit enable survives ordinary call completion, without overlap.
{
  const h = await running();
  for (const sequence of [1, 2]) {
    const completed = h.calls.at(-1);
    await h.observe(phone('ended', sequence));
    assert.equal(h.state().mode, 'armed', 'normal hangup preserves enabled mode');
    assert.equal(h.state().call, null);
    assert.equal(completed.endCalls, 1, 'completed session is cleaned up once');
    await h.observe(phone('connected', sequence + 1));
    assert.equal(h.calls.length, sequence + 1, 'new outgoing sequence starts automatically');
    assert.equal(h.state().mode, 'running');
  }
  await h.close();
}

// Retell can end before cellular hangup. It must wait for a different sequence.
{
  const h = await running();
  await h.end();
  assert.equal(h.state().mode, 'armed');
  for (const phase of ['connected', 'ended', 'connected']) await h.observe(phone(phase, 1));
  assert.equal(h.calls.length, 1, 'Retell ending cannot restart on the same cellular call');
  await h.observe(phone('connected', 2));
  assert.equal(h.calls.length, 2, 'a later outgoing call still starts');
  await h.close();
}

// Stop remains effective while an old session takes time to clean up.
{
  const h = await running();
  const completed = h.calls[0];
  completed.endBlock = defer();
  await h.end(completed);
  assert.equal(h.state().stopping, true, 'cleanup remains pending');
  assert.equal(h.element('stopAll').disabled, false, 'manual stop must remain accessible during automatic cleanup');
  const stopped = h.element('stopAll').onclick();
  await settle();
  assert.equal(h.state().mode, 'idle', 'manual stop immediately cancels enabled mode');
  completed.endBlock.resolve();
  await stopped; await settle();
  assert.equal(h.state().mode, 'idle', 'late cleanup cannot re-enable after manual stop');
  await h.observe(phone('connected', 2));
  assert.equal(h.calls.length, 1);
  await h.close();
}

// Event delivery from an already completed session must not affect a new one.
{
  const h = await running();
  const old = h.calls[0];
  await h.end(old);
  await h.observe(phone('connected', 2));
  const current = h.calls[1];
  await h.end(old);
  await h.error(old, 'late error from the previous session');
  assert.equal(h.state().call, current, 'stale events must not stop the current session');
  assert.equal(h.state().mode, 'running');
  assert.equal(current.endCalls, 0);
  await h.close();
}

// Fatal transport errors disable continuous mode until explicit re-enable.
{
  const h = await running();
  await h.error();
  assert.equal(h.state().mode, 'idle');
  assert.equal(h.state().call, null);
  await h.observe(phone('ended', 1));
  await h.observe(phone('connected', 2));
  assert.equal(h.calls.length, 1, 'failure must never silently rearm');
  await h.close();
}

// Native bridge close notifies its Retell subscriber before the app callback in
// production. Exercise both event orders, including this less obvious ordering.
for (const retellFirst of [true, false]) {
  const h = await running();
  await h.closeBridge('Соединение с аудиомостом потеряно.', retellFirst);
  assert.equal(h.state().mode, 'idle', 'bridge failure disables waiting even when Retell onEnd arrives first');
  await h.observe(phone('connected', 2));
  assert.equal(h.calls.length, 1);
  await h.close();
}
{
  const h = await running();
  h.advance(180000);
  await h.tickIntervals();
  assert.equal(h.state().mode, 'running', 'conversation continues beyond two minutes');
  assert.equal(h.calls[0].endCalls, 0, 'elapsed-time display must not terminate Retell');
  assert.equal(h.element('timeLeft').textContent, 'Длительность 3:00');
  await h.observe(phone('ended', 1));
  assert.equal(h.state().mode, 'armed', 'hangup still releases audio and waits for the next call');
  await h.close();
}

// A call can finish while Windows is still opening audio. Polling must leave the
// next answer available until cleanup has released the old startup operation.
{
  const h = await harness();
  await h.click('arm');
  const opening = h.deferNextBridge();
  await h.observe(phone('connected', 1));
  assert.equal(h.state().pending, 1);
  await h.observe(phone('ended', 1));
  await h.observe(phone('connected', 2));
  assert.equal(h.state().mode, 'armed', 'pending old startup cannot consume the next sequence');
  assert.equal(h.calls.length, 0);
  opening.resolve(); await settle();
  assert.equal(h.state().pending, 0);
  await h.observe(phone('connected', 2));
  assert.equal(h.calls.length, 1, 'next answer starts after the old startup finishes cleanup');
  await h.close();
}

// The virtual microphone is prepared before dialing and remains between calls.
{
  const h = await harness();
  h.element('audioMode').value = 'virtual-mic';
  await h.click('arm');
  assert.equal(h.routeCommands[0].action, 'prepare');
  await h.observe(phone('connected', 1));
  assert.equal(h.bridges[0].outputMode, 'virtual-mic');
  await h.observe(phone('ended', 1));
  assert.equal(h.routeCommands.filter(c => c.action === 'reset').length, 0);
  await h.observe(phone('connected', 2));
  assert.equal(h.calls.length, 2);
  await h.click('stopAll');
  assert.equal(h.routeCommands.at(-1).action, 'reset', 'explicit stop restores normal microphone');
  await h.close();
}
{
  const h = await harness(phone('idle'), { virtualInstalled: false });
  h.element('audioMode').value = 'virtual-mic';
  await h.click('arm');
  assert.equal(h.state().mode, 'idle');
  assert.equal(h.calls.length, 0, 'missing driver must not create a billable call');
  assert.match(h.element('notice').textContent, /VB-CABLE/);
  await h.close();
}
{
  const block = defer();
  const h = await harness(phone('idle'), { prepareBlock: block });
  h.element('audioMode').value = 'virtual-mic';
  const arming = h.element('arm').onclick();
  await settle();
  await h.click('stopAll');
  block.resolve(); await arming; await settle();
  assert.equal(h.state().mode, 'idle');
  assert.equal(h.routeCommands.at(-1).action, 'reset', 'late preparation cannot leave microphone redirected');
  await h.close();
}
{
  const savedConnection = { key: 'public_key_offline_saved', agentId: 'agent_offline_saved' };
  const h = await harness(phone('idle'), { savedConnection });
  assert.equal(h.element('key').value, savedConnection.key);
  assert.equal(h.element('agent').value, savedConnection.agentId);
  assert.equal(h.calls.length, 0, 'loading a saved connection must never start a paid session');
  await h.input('key', 'public_key_offline_changed');
  await h.runDelay(700);
  assert.equal(h.savedConnections.at(-1).key, 'public_key_offline_changed');
  assert.match(h.element('connectionStatus').textContent, /сохранено/);
  await h.close();
}
{
  const block = defer();
  const h = await harness(phone('idle'), { loadBlock: block, savedConnection: { key: 'public_key_old', agentId: 'agent_old' } });
  await h.input('key', 'public_key_user_edit');
  block.resolve(); await settle();
  assert.equal(h.element('key').value, 'public_key_user_edit', 'late load must not overwrite user edits');
  await h.close();
}
// Enabled mode applies to incoming calls and already answered conversations.
for (const direction of ['incoming', 'outgoing', 'unknown']) {
  const h = await harness(phone('connected', 7, { callDirection: direction }));
  await h.click('arm');
  await h.observe(phone('connected', 7, { callDirection: direction }));
  assert.equal(h.calls.length, 1, 'enabling during a confirmed call must connect it');
  await h.observe(phone('ended', 7, { callDirection: direction }));
  await h.observe(phone('incoming', 8, { callDirection: 'incoming' }));
  assert.equal(h.calls.length, 1, 'ringing alone must not start Retell');
  await h.observe(phone('connected', 8, { callDirection: 'incoming' }));
  assert.equal(h.calls.length, 2, 'answered incoming call starts automatically');
  await h.close();
}
{
  const h = await running();
  await h.observe(phone('connected', 2));
  assert.equal(h.state().mode, 'armed', 'missed hangup must still clean up the previous call');
  await h.observe(phone('connected', 2));
  assert.equal(h.calls.length, 2);
  await h.close();
}
{
  const h = await harness();
  await h.click('routeVirtual');
  assert.equal(h.element('audioMode').value, 'virtual-mic');
  assert.equal(h.element('routeVirtual').ariaPressed, 'true');
  assert.equal(h.savedConnections.at(-1).audioMode, 'virtual-mic', 'button selection persists');
  await h.close();
}
console.log('PASS app lifecycle: any answered call, saved mode buttons, key persistence, sequential cleanup and cancellation; offline mocks only');
