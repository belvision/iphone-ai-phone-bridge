import { startNativeBridge } from './browser-native.js';
import { createNativeRetellCall } from './native-retell.ts';
import { createNativeElevenCall } from './native-eleven.js';
import { AutoCallGate, describePhoneState } from './auto-call.js';
import './crm-ui.js';
import './contacts-ui.js';
const el = id => document.getElementById(id);
const gate = new AutoCallGate();
let bridge, call, stopping = false, pending = 0, operation = 0, countdown;
let phoneRequest, pollTimer, disposed = false;
let routeTimer, preparedMode = null, routePolling = false;
let connectionLoading = true, connectionDirty = false, connectionVersion = 0, connectionTimer;
let connectionSaveQueue = Promise.resolve();
let elevenConfig = {}, elevenLoading = true, elevenSaving = false;
function provider() { return el('provider').value || 'retell'; }
function providerName() { return {retell:'Retell',agents:'ElevenLabs Agents',custom:'Свой диалог'}[provider()]; }
async function loadEleven() {
  try {
    const response = await fetch('/eleven/settings', {cache:'no-store', signal:AbortSignal.timeout(4000)});
    if (!response.ok) throw new Error();
    elevenConfig = await response.json();
    for (const [field,key] of [['elevenAgent','agentId'],['voiceId','voiceId'],['llmUrl','llmUrl'],['llmModel','llmModel'],['greeting','greeting'],['dialogPrompt','prompt']]) el(field).value = elevenConfig[key] || '';
    el('replyPauseMs').value = String(elevenConfig.replyPauseMs || 350);
    el('elevenStatus').textContent = elevenConfig.hasElevenKey ? 'Ключ ElevenLabs сохранён на этом компьютере.' : 'Добавьте ключ ElevenLabs.';
  } catch { el('elevenStatus').textContent = 'Не удалось загрузить настройки ElevenLabs.'; }
  finally { elevenLoading = false; controls(); }
}
el('saveEleven').onclick = async () => {
  elevenSaving = true; controls();
  try {
    const body = {elevenKey:el('elevenKey').value.trim(),llmKey:el('llmKey').value.trim(),agentId:el('elevenAgent').value.trim(),voiceId:el('voiceId').value.trim(),llmUrl:el('llmUrl').value.trim(),llmModel:el('llmModel').value.trim(),greeting:el('greeting').value,prompt:el('dialogPrompt').value,replyPauseMs:Number(el('replyPauseMs').value || 350),syncAgent:provider()==='agents'};
    const response = await fetch('/eleven/settings', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),signal:AbortSignal.timeout(20000)});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Не удалось сохранить настройки.');
    elevenConfig = result; el('elevenKey').value = el('llmKey').value = '';
    el('elevenStatus').textContent = body.syncAgent ? 'Инструкция и управление разговором сохранены в ElevenLabs и на этом компьютере.' : 'Настройки сохранены с шифрованием Windows.';
  } catch (error) { el('elevenStatus').textContent = error.message; }
  finally { elevenSaving = false; controls(); }
};
async function loadConnection() {
  const version = connectionVersion;
  try {
    const response = await fetch('/connection', { cache: 'no-store', signal: AbortSignal.timeout(4000) });
    const saved = await response.json();
    if (!response.ok || saved.error) throw new Error('load');
    if (disposed) return;
    if (saved.saved && version === connectionVersion && !connectionDirty) {
      el('key').value = saved.key;
      el('agent').value = saved.agentId;
      if (['direct-hfp', 'virtual-mic'].includes(saved.audioMode)) el('audioMode').value = saved.audioMode;
      el('connectionStatus').textContent = 'Подключение загружено с этого компьютера.';
    } else if (!connectionDirty) el('connectionStatus').textContent = 'Введите публичный ключ один раз — он сохранится автоматически.';
  } catch {
    if (!disposed && !connectionDirty) el('connectionStatus').textContent = 'Не удалось загрузить подключение. Можно ввести публичный ключ вручную.';
  } finally { connectionLoading = false; controls(); }
}
function saveConnection() {
  clearTimeout(connectionTimer);
  const key = el('key').value.trim(), agentId = el('agent').value.trim(), version = connectionVersion, audioMode = selectedMode();
  if (!/^public_key_[A-Za-z0-9_-]+$/.test(key) || !/^agent_[A-Za-z0-9_-]+$/.test(agentId)) {
    el('connectionStatus').textContent = 'Для сохранения нужны полный Public Key и ID агента.';
    return Promise.resolve(false);
  }
  connectionSaveQueue = connectionSaveQueue.catch(() => {}).then(async () => {
    try {
      const response = await fetch('/connection', { method: 'POST',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key, agentId, audioMode }),
        keepalive: true, signal: AbortSignal.timeout(5000),
      });
      const result = await response.json();
      if (!response.ok || !result.saved) throw new Error('save');
      if (!disposed && version === connectionVersion) {
        connectionDirty = false;
        el('connectionStatus').textContent = 'Подключение сохранено на этом компьютере.';
      }
      return true;
    } catch {
      if (!disposed && version === connectionVersion) el('connectionStatus').textContent = 'Подключение не сохранилось. Проверьте локальную программу и попробуйте ещё раз.';
      return false;
    }
  });
  return connectionSaveQueue;
}
for (const id of ['key', 'agent']) el(id).oninput = () => {
  connectionDirty = true; connectionVersion++;
  el('connectionStatus').textContent = 'Сохраняю подключение…';
  clearTimeout(connectionTimer);
  connectionTimer = setTimeout(() => void saveConnection(), 700);
};
function selectedMode() { return el('audioMode').value || 'direct-hfp'; }
async function loadNetworkStatus() {
  try {
    const response = await fetch('/network-status', { cache: 'no-store', signal: AbortSignal.timeout(3000) });
    if (!response.ok) throw new Error('network');
    const status = await response.json();
    el('networkStatus').textContent = status.proxyEnabled ? 'Прокси США для Retell и ElevenLabs настроен.' : 'Американский прокси не настроен.';
  } catch { el('networkStatus').textContent = 'Не удалось проверить подключение Retell.'; }
}
async function routeRequest(command) {
  const response = await fetch('/audio-route', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(command), signal: AbortSignal.timeout(5000),
  });
  const result = await response.json();
  if (!response.ok || result.error) throw new Error(result.error || 'Не удалось настроить передачу звука.');
  return result;
}
function routeStatus(result) {
  const mode = selectedMode();
  if (result?.virtualMicInstalled && el('notice').textContent.startsWith('VB-CABLE не найден.')) el('notice').textContent = '';
  el('routeStatus').textContent = mode === 'direct-hfp'
    ? 'Нынешний способ работает, но в тестах теряет около 10% аудиоданных на выходе.'
    : !result?.virtualMicInstalled ? 'Для этого режима нужно установить VB-CABLE и перезагрузить компьютер.'
    : result.virtualMicPrepared ? 'Виртуальный микрофон подготовлен. Сделайте новый звонок через «Связь с телефоном».'
    : 'VB-CABLE найден. При включении помощника подготовится виртуальный микрофон.';
  el('routeNotice').hidden = mode !== 'virtual-mic';
}
async function resetRoute() {
  const oldMode = preparedMode; preparedMode = null;
  if (oldMode === 'virtual-mic') {
    try { routeStatus(await routeRequest({ action: 'reset' })); }
    catch (error) { el('notice').textContent = error.message + ' Если микрофон не вернулся, выберите обычный микрофон в настройках Windows.'; log(error.message); }
  }
}
async function prepareRoute(token) {
  const mode = selectedMode();
  if (preparedMode === mode) return;
  if (mode === 'virtual-mic') {
    const result = await routeRequest({ action: 'prepare', mode });
    preparedMode = mode;
    routeStatus(result);
    if (token !== operation || disposed) await resetRoute();
  } else {
    await resetRoute();
    preparedMode = mode;
  }
}
async function pollRoutes() {
  if (disposed || routePolling) return;
  routePolling = true;
  const token = operation;
  try {
    const result = preparedMode === 'virtual-mic'
      ? await routeRequest({ action: 'heartbeat' })
      : await fetch('/audio-routes', { cache: 'no-store', signal: AbortSignal.timeout(3000) }).then(r => { if (!r.ok) throw new Error('routes'); return r.json(); });
    if (token !== operation) return;
    routeStatus(result);
    if (preparedMode === 'virtual-mic' && !result.virtualMicPrepared) {
      el('notice').textContent = 'Настройки виртуального микрофона изменились. Помощник выключен.';
      void stopAll('Настройки микрофона изменились.');
    }
  } catch { if (token === operation) el('routeStatus').textContent = 'Не удалось проверить устройства. Нынешний способ доступен; VB-CABLE требует проверки локальной программы.'; }
  finally { routePolling = false; if (!disposed) routeTimer = setTimeout(pollRoutes, 5000); }
}
function log(message) { el('log').textContent = `${new Date().toLocaleTimeString()} ${message}\n` + el('log').textContent.slice(0, 5500); }
function controls() {
  const busy = pending > 0 || stopping || connectionLoading || elevenSaving;
  const armed = gate.mode === 'armed';
  for (const [id, name] of [['engineRetell','retell'],['engineAgents','agents'],['engineCustom','custom']]) {
    el(id).disabled = busy || !!bridge || !!call || gate.mode !== 'idle';
    el(id).ariaPressed = String(provider() === name);
  }
  el('saveEleven').disabled = busy || !!bridge || !!call || gate.mode !== 'idle' || elevenLoading;
  for (const [id, voice] of [['voiceMale','cjVigY5qzO86Huf0OWal'],['voiceFemale','hpp4J3VqNfWAUOO0d1Us']]) {
    el(id).disabled = el('saveEleven').disabled;
    el(id).ariaPressed = String(el('voiceId').value.trim() === voice);
  }
  el('voiceId').disabled = el('saveEleven').disabled;
  el('saveEleven').textContent = provider() === 'agents' ? 'Сохранить инструкцию агента' : 'Сохранить настройки';
  el('retellFields').hidden = provider() !== 'retell';
  el('elevenFields').hidden = provider() === 'retell';
  el('agentsFields').hidden = provider() !== 'agents';
  el('customFields').hidden = provider() !== 'custom';
  el('providerStatus').textContent = provider() === 'retell' ? 'Retell — проверенный вариант.' : provider() === 'agents' ? 'Диалог целиком ведёт агент ElevenLabs.' : 'Наш диалог: распознавание → модель ответов → голос ElevenLabs.';
  el('bridgeStart').disabled = busy || !!bridge || armed;
  el('tone').disabled = busy || !bridge || !!call || armed;
  el('stopAll').disabled = stopping ? !gate.automatic : (!pending && !bridge && !call && gate.mode === 'idle');
  el('start').disabled = busy || !!call || armed;
  el('arm').disabled = busy || !!bridge || !!call || armed;
  el('audioMode').disabled = busy || !!bridge || !!call || gate.mode !== 'idle';
  for (const [id, mode] of [['routeDirect', 'direct-hfp'], ['routeVirtual', 'virtual-mic']]) {
    el(id).disabled = el('audioMode').disabled;
    el(id).ariaPressed = String(selectedMode() === mode);
  }
  el('status').textContent = stopping ? 'Завершаю подключение…' : armed ? 'Жду следующий звонок' : call ? (call.status === 'live' ? 'Помощник на связи' : 'Подключаю помощника…') : busy ? 'Подключение…' : bridge ? 'Проверка звука включена' : 'Помощник выключен';
  el('status').dataset.state = armed ? 'ready' : call?.status === 'live' ? 'live' : 'idle';
  el('autoState').textContent = stopping ? (gate.automatic ? 'Затем помощник будет ждать следующий звонок.' : 'Помощник выключается.') : armed ? 'AI подключится к любому отвеченному звонку: входящему или исходящему.' : call ? (gate.automatic ? 'После разговора буду ждать следующий звонок.' : 'После завершения разговора помощник отключится сам.') : busy ? 'Подождите несколько секунд.' : 'Включите помощника один раз для входящих и исходящих звонков.';
}
function resetMeter() { el('inputLevel').value = 0; el('outputLevel').value = 0; }
function clockStart() {
  clearInterval(countdown);
  const startedAt = Date.now();
  countdown = setInterval(() => {
    const elapsed = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
    el('timeLeft').textContent = `Длительность ${Math.floor(elapsed / 60)}:${String(elapsed % 60).padStart(2, '0')}`;
  }, 250);
}
function validateAgent() {
  el('notice').textContent = ''; el('settingsError').textContent = '';
  if (!el('confirmed').checked) {
    el('confirmed').focus();
    el('notice').textContent = 'Отметьте, что собеседник предупреждён о тесте с AI.';
    throw new Error(el('notice').textContent);
  }
  if (provider() !== 'retell') {
    if (elevenLoading || !elevenConfig[provider() === 'agents' ? 'agentsReady' : 'customReady']) {
      el('settings').open = true;
      throw new Error('Заполните и сохраните настройки выбранного варианта ElevenLabs.');
    }
    return {};
  }
  const key = el('key').value.trim(), agentId = el('agent').value.trim();
  const fieldError = (id, message) => {
    el('settings').open = true;
    el('settingsError').textContent = message;
    el(id).focus();
    throw new Error(message);
  };
  if (!/^public_key_[A-Za-z0-9_-]+$/.test(key)) fieldError('key', 'Введите публичный ключ Retell, который начинается с public_key_.');
  if (!/^agent_[A-Za-z0-9_-]+$/.test(agentId)) fieldError('agent', 'Проверьте ID агента: он должен начинаться с agent_.');
  return { key, agentId };
}
async function readPhoneState() {
  // Arming, polling and final checks share one bounded request.
  if (phoneRequest) return phoneRequest;
  phoneRequest = (async () => {
    const abort = new AbortController();
    const timeout = setTimeout(() => abort.abort(), 1500);
    try {
      const response = await fetch('/phone-state', { cache: 'no-store', signal: abort.signal });
      if (!response.ok) throw new Error('Наблюдение за телефоном недоступно.');
      return await response.json();
    } finally { clearTimeout(timeout); }
  })();
  try { return await phoneRequest; } finally { phoneRequest = null; }
}
function observePhone(state) {
  el('phoneState').textContent = describePhoneState(state);
  el('phoneState').hidden = !call && ['idle', 'ended'].includes(state?.callPhase);
  const action = gate.observe(state, Date.now(), { allowStart: !pending && !stopping });
  controls();
  if (action?.type === 'start') {
    log('Собеседник ответил. Подключаю агента к новому звонку…');
    void startAgent({ sequence: action.sequence }).catch(error => log(error?.message || String(error)));
  } else if (action?.type === 'stop') {
    void stopAll(action.reason, { resumeSequence: action.resume ? action.sequence : undefined });
  } else if (action?.type === 'warning') {
    el('notice').textContent = action.reason;
    log(action.reason);
    if (!stopping) void resetRoute();
  }
}
async function pollPhone() {
  if (disposed) return;
  try { observePhone(await readPhoneState()); } catch { observePhone(null); }
  if (!disposed) pollTimer = setTimeout(pollPhone, 500);
}
function finishAgent(message) {
  return stopAll(message, { resumeSequence: gate.automatic ? gate.sequence : undefined });
}
async function stopAll(message = 'Помощник и ожидание выключены. Сотовый звонок завершите отдельно.', { resumeSequence } = {}) {
  const token = ++operation;
  const resume = resumeSequence !== undefined && gate.finish(resumeSequence);
  if (!resume) gate.cancel();
  if (stopping) { controls(); return; }
  stopping = true;
  clearInterval(countdown); el('timeLeft').textContent = '';
  const oldCall = call, oldBridge = bridge;
  call = bridge = null;
  controls();
  try { await oldCall?.end(); } catch { gate.cancel(); log('Не удалось штатно завершить сессию Retell; ожидание выключено.'); }
  try { await oldBridge?.stop(); } catch {}
  resetMeter();
  el('bridgeState').textContent = 'Аудиомост отключён.';
  const waiting = resume && token === operation && !disposed && gate.waitNext(resumeSequence);
  if (!waiting) await resetRoute();
  stopping = false;
  controls(); log(message + (waiting ? ' Ожидаю следующий звонок.' : ''));
}
async function openBridge() {
  let current;
  current = await startNativeBridge({
    outputMode: selectedMode(),
    onStatus: value => { el('bridgeState').textContent = typeof value === 'string' ? value : value?.message || value?.type || 'Проверка соединения'; },
    onLevel: stats => {
      el('inputLevel').value = Math.min(1, Math.max(0, stats.inputPeak || 0) * 3);
      el('outputLevel').value = Math.min(1, Math.max(0, stats.outputPeak || 0) * 3);
    },
    onClose: reason => {
      if (!current || bridge !== current || stopping) return;
      void stopAll('Аудиомост закрыт. Помощник и ожидание выключены.');
    },
  });
  if (current.closed) throw new Error('Аудиомост закрылся при запуске.');
  return current;
}
async function enableBridge() {
  if (pending || bridge || stopping || gate.mode === 'armed') return;
  const token = ++operation; pending++; controls();
  let opened;
  try {
    await prepareRoute(token);
    if (token !== operation) return;
    opened = await openBridge();
    if (token !== operation) { opened.stop(); return; }
    bridge = opened; clockStart();
    log('Аудиомост включён. Retell пока не подключён. Попросите собеседника говорить и проверьте индикатор.');
  } catch (error) {
    if (token === operation) await stopAll('Проверка звука остановлена.');
    throw error;
  } finally { pending--; controls(); }
}
async function armNextCall() {
  if (pending || bridge || call || stopping || gate.mode === 'armed') return;
  validateAgent();
  const token = ++operation; pending++; controls();
  try {
    await saveConnection();
    if (token !== operation) return;
    await prepareRoute(token);
    if (token !== operation) return;
    const state = await readPhoneState();
    if (token !== operation) return;
    gate.arm(state);
    el('phoneState').textContent = describePhoneState(state);
    log(state.callPhase === 'connected'
      ? 'Включено: подключаюсь к текущему разговору на компьютере.'
      : 'Ожидаю любой отвеченный звонок до нажатия «Остановить»: входящий или исходящий.');
  } catch (error) {
    if (token === operation) await stopAll('Включение помощника отменено.');
    throw error;
  } finally { pending--; controls(); }
}
async function startAgent({ sequence: automaticSequence } = {}) {
  if (pending || call || stopping) return;
  let token;
  let opened;
  let sequence = automaticSequence;
  try {
    validateAgent();
    if (automaticSequence === undefined && bridge) await stopAll('Перезапускаю аудиомост для нового разговора.');
    token = ++operation; pending++; controls();
    await saveConnection();
    if (token !== operation) return;
    await prepareRoute(token);
    if (token !== operation) return;
    let state = await readPhoneState();
    if (token !== operation) return;
    if (automaticSequence === undefined) sequence = gate.startManual(state);
    else {
      observePhone(state);
      if (token !== operation || !gate.canStart(state, sequence)) return;
    }
    opened = await openBridge();
    if (token !== operation) { opened.stop(); return; }
    bridge = opened;
    // Windows audio startup takes time: recheck before any billable Retell call.
    state = await readPhoneState();
    observePhone(state);
    if (token !== operation || !gate.canStart(state, sequence)) {
      opened.stop(); if (bridge === opened) bridge = null;
      if (token === operation) await stopAll('Запуск отменён: разговор не подтверждён.');
      return;
    }
    const { key, agentId } = validateAgent();
    if (!gate.markRunning(sequence)) { opened.stop(); return; }
    clockStart();
    const createCall = provider() === 'retell' ? createNativeRetellCall : createNativeElevenCall;
    const service = providerName();
    call = createCall({ key, agentId, mode: provider(), bridge, hooks: {
      onStatus: status => { if (token !== operation || stopping) return; controls(); log(status === 'live' ? service + ' подключён. Собеседник может говорить с агентом.' : service + ': ' + status); },
      onAgentStartTalking: () => { if (token === operation && !stopping) log(service + ' начал ответ голосом.'); },
      onError: error => {
        if (token !== operation || stopping) return;
        const message = String(error?.message || error || 'Ошибка соединения').replace(/(?:public_key_|key_)[A-Za-z0-9_-]+/g, '[ключ скрыт]');
        log(service + ': ' + message.slice(0, 600));
        el('notice').textContent = /sanctioned region/i.test(message)
          ? 'Retell отклонил доступ из региона. Агент не подключён. Проверьте американский прокси.'
          : message.includes('американский прокси') ? message : 'Ошибка ' + service + ': ' + message.slice(0, 240);
        void stopAll('Разговор остановлен после ошибки ' + service + '.');
      },
      onEnd: () => {
        if (token !== operation || stopping) return;
        if (opened.closed) {
          void stopAll('Аудиомост прервался. Помощник и ожидание выключены.');
        } else void finishAgent('Сессия ' + service + ' завершена. Сотовый звонок завершите отдельно.');
      },
    } });
    log('Подключаю ' + service + ' через аудиомост Windows…');
  } catch (error) {
    if (token === undefined || token === operation) await stopAll('Подключение остановлено.');
    else opened?.stop();
    throw error;
  } finally {
    if (token !== undefined) pending--;
    controls();
  }
}
for (const [id, fn] of [
  ['bridgeStart', enableBridge], ['tone', () => bridge?.tone()], ['start', () => startAgent()],
  ['arm', armNextCall], ['stopAll', () => stopAll()],
]) {
  el(id).onclick = () => Promise.resolve().then(fn).catch(error => {
    const message = error?.message || String(error);
    if (!el('settingsError').textContent) el('notice').textContent = message;
    log(message); controls();
  });
}
window.addEventListener('pagehide', () => {
  if (connectionDirty) void saveConnection();
  clearTimeout(connectionTimer);
  disposed = true; operation++; gate.cancel(); clearTimeout(pollTimer); clearTimeout(routeTimer); clearInterval(countdown);
  void call?.end(); void bridge?.stop();
  if (preparedMode === 'virtual-mic') void fetch('/audio-route', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'reset' }), keepalive: true,
  }).catch(() => {});
});
el('audioMode').onchange = () => { clearTimeout(routeTimer); el('notice').textContent = ''; routeStatus(null); void resetRoute().then(pollRoutes); };
for (const [id, mode] of [['routeDirect', 'direct-hfp'], ['routeVirtual', 'virtual-mic']]) el(id).onclick = () => {
  if (el(id).disabled) return;
  el('audioMode').value = mode; connectionVersion++; connectionDirty = true;
  el('audioMode').onchange(); controls(); void saveConnection();
};
controls();
for (const [id, voice] of [['voiceMale','cjVigY5qzO86Huf0OWal'],['voiceFemale','hpp4J3VqNfWAUOO0d1Us']]) el(id).onclick = () => {
  if (el(id).disabled) return;
  el('voiceId').value = voice;
  el('elevenStatus').textContent = 'Голос выбран. Нажмите «Сохранить», чтобы применить его к следующему разговору.';
  controls();
};
el('voiceId').oninput = controls;
for (const [id,name] of [['engineRetell','retell'],['engineAgents','agents'],['engineCustom','custom']]) el(id).onclick = () => {
  if (el(id).disabled) return;
  el('provider').value = name; el('notice').textContent = '';
  controls();
};
void loadConnection();
void loadEleven();
void loadNetworkStatus();
void pollPhone();
void pollRoutes();
