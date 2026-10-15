import assert from 'node:assert/strict';
import { AutoCallGate, trustedPhoneState } from './auto-call.js';
const state = (phase, sequence = 1, extra = {}) => ({
  available: true, active: phase === 'connected', callPhase: phase,
  callSequence: sequence, callDirection: 'outgoing', audioOnComputer: phase === 'connected', ageMs: 10, ...extra,
});
const armed = (phase = 'idle', sequence = 1) => { const gate = new AutoCallGate(); gate.arm(state(phase, sequence)); return gate; };

// Every direction, including polling which missed the ringing phase.
for (const callDirection of ['outgoing', 'incoming', 'unknown', undefined]) {
  const gate = armed();
  assert.equal(gate.observe(state('incoming', 2, { callDirection })), null);
  assert.deepEqual(gate.observe(state('connected', 2, { callDirection })), { type: 'start', sequence: 2 });
  assert(gate.canStart(state('connected', 2, { callDirection }), 2));
  assert.equal(gate.observe(state('connected', 2, { callDirection })), null);
}

// Enable before dialing, while ringing, or in an already answered conversation.
for (const phase of ['dialing', 'incoming', 'connected', 'held', 'unknown']) {
  const gate = armed(phase, 4);
  assert.equal(gate.observe(state('connected', 4)).type, 'start', phase);
}
for (const phase of ['idle', 'ended', 'lost']) {
  const gate = armed(phase, 4);
  assert.equal(gate.observe(state('connected', 4)), null);
  assert.equal(gate.observe(state('connected', 5)).type, 'start');
}

// No voice session before answer, or while sound is still on the handset.
{
  const gate = armed();
  assert.equal(gate.observe(state('dialing', 2)), null);
  assert.equal(gate.observe(state('incoming', 2)), null);
  assert.equal(gate.observe(state('connected', 2, { audioOnComputer: false })), null);
  assert.equal(gate.observe(state('connected', 2)).type, 'start');
}
for (const phase of ['starting', 'running']) {
  const gate = armed('dialing', 4);
  gate.observe(state('connected', 4));
  if (phase === 'running') assert(gate.markRunning(4));
  assert(!gate.waitNext(4));
  assert(gate.finish(4));
  assert(gate.finish(4));
  assert.equal(gate.observe(state('connected', 4)), null);
  assert(gate.waitNext(4));
  assert.equal(gate.observe(state('connected', 4)), null, 'same cellular call never creates another paid session');
  assert.equal(gate.observe(state('connected', 5, { callDirection: 'incoming' })).type, 'start');
}
{
  const gate = armed();
  gate.observe(state('connected', 2)); gate.markRunning(2);
  const end = gate.observe(state('ended', 2));
  assert.equal(end.resume, true);
  assert.equal(gate.mode, 'finishing');
  assert.equal(gate.observe(state('ended', 2)), null);
  assert(!gate.waitNext(1));
  assert(gate.waitNext(2));
  assert.equal(gate.observe(state('connected', 3), 0, { allowStart: false }), null);
  assert.equal(gate.mode, 'armed');
  assert.equal(gate.observe(state('connected', 3), 500).type, 'start');
}
// A fast next call may replace the previous one between polling snapshots.
{
  const gate = armed();
  gate.observe(state('connected', 2)); gate.markRunning(2);
  const end = gate.observe(state('connected', 3));
  assert.equal(end.resume, true);
  assert.equal(end.sequence, 2);
  assert.equal(gate.observe(state('connected', 3)), null, 'cleanup cannot overlap another connection');
  assert(gate.waitNext(2));
  assert.equal(gate.observe(state('connected', 3)).type, 'start');
}
// Brief monitor delays suspend starting; three seconds of loss cancels waiting.
for (const bad of [null, state('connected', 2, { available: false }), state('connected', 2, { ageMs: 2001 }), state('unknown', 2)]) {
  let gate = armed();
  assert.equal(gate.observe(bad, 0), null);
  assert.equal(gate.observe(bad, 2999), null);
  assert(gate.observe(bad, 3000));
  assert.equal(gate.mode, 'idle');
  assert.equal(gate.observe(state('connected', 2)), null);
  gate = armed();
  gate.observe(bad, 0);
  assert.equal(gate.observe(state('dialing', 2), 1000), null);
  assert.equal(gate.observe(bad, 2000), null);
  assert.equal(gate.observe(state('connected', 2), 2500).type, 'start', 'fresh recovery retains enabled mode');
}
{
  const gate = armed();
  assert(gate.observe(state('multiple', 2), 0, { allowStart: false }));
  assert.equal(gate.mode, 'idle');
}
for (const ended of ['ended', 'lost', 'idle', 'held', 'multiple']) {
  const gate = new AutoCallGate();
  gate.startManual(state('connected', 8)); gate.markRunning(8);
  assert.equal(gate.observe(state(ended, 8)).type, 'stop');
}
for (const changed of [state('connected', 9), state('connected', 8, { audioOnComputer: false })]) {
  const gate = new AutoCallGate();
  gate.startManual(state('connected', 8)); gate.markRunning(8);
  assert.equal(gate.observe(changed).type, 'stop');
}
{
  const gate = new AutoCallGate();
  gate.startManual(state('connected', 8)); gate.markRunning(8);
  assert.equal(gate.observe(null, 0), null);
  assert.equal(gate.observe(null, 2999), null);
  assert.equal(gate.observe(null, 3000).type, 'stop');
}
{
  const gate = armed();
  gate.observe(state('connected', 2));
  gate.finish(2); gate.cancel();
  assert(!gate.waitNext(2), 'manual stop wins over delayed cleanup');
  assert(!gate.canStart(state('connected', 2), 2));
  assert(!gate.markRunning(2));
  assert.equal(gate.observe(state('connected', 3)), null);
}
assert.throws(() => new AutoCallGate().startManual(state('dialing')), /дождитесь ответа/);
assert(!trustedPhoneState(state('connected', 2, { ageMs: -1 })));
assert(!trustedPhoneState(state('connected', 2, { ageMs: 2001 })));
console.log('PASS any answered call, current-call attachment, no ringing charge, one start per call, quick successive calls, cleanup ordering and monitor grace; no cloud calls');
