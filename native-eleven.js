/** Session lifecycle matching the Retell adapter; secrets stay in Python. */
export function createNativeElevenCall({ mode, bridge, hooks = {} }) {
  const url = new URL('/eleven/conversation', location.href);
  url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  url.searchParams.set('mode', mode);
  const socket = new WebSocket(url);
  let ended = false, opened = false;
  const session = { status: 'connecting', end };
  const unsubscribe = bridge.subscribeClose(() => end());
  const heartbeat = setInterval(() => {
    if (socket.readyState === WebSocket.OPEN) socket.send('ping');
  }, 5000);
  const startup = setTimeout(() => {
    if (!opened && !ended) { hooks.onError?.(new Error('ElevenLabs не подключился за 20 секунд.')); end(); }
  }, 20000);
  function end() {
    if (ended) return;
    ended = true; session.status = 'ended';
    clearInterval(heartbeat); clearTimeout(startup); unsubscribe();
    if (socket.readyState === WebSocket.OPEN) socket.send('stop');
    socket.close();
    hooks.onEnd?.();
  }
  socket.onmessage = ({ data }) => {
    if (ended || data === 'pong') return;
    let event;
    try { event = JSON.parse(data); } catch { return; }
    if (event.type === 'live') {
      opened = true; clearTimeout(startup); session.status = 'live'; hooks.onStatus?.('live');
    } else if (event.type === 'error') { hooks.onError?.(new Error(event.message)); end(); }
    else if (event.type === 'ended') end();
    else if (event.type === 'latency') {
      bridge.diagnostic?.(`ElevenLabs: начало голоса через ${Number(event.audioSeconds).toFixed(2)} с после распознавания.`);
    } else if (event.type === 'interrupted') bridge.diagnostic?.('Собеседник перебил агента; старый ответ отменён.');
    else if (event.type === 'activity' && event.speaker === 'agent') hooks.onAgentStartTalking?.();
  };
  socket.onerror = () => { if (!ended) hooks.onError?.(new Error('Не удалось подключить ElevenLabs. Проверьте настройки и ответ на звонок.')); end(); };
  socket.onclose = () => end();
  return session;
}
