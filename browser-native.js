/**
 * PCM websocket bridge. Run this page on the loopback server at port 8766.
 * Native input -> worklet -> MediaStream for Retell.
 * Retell remote stream -> worklet -> native output. No audible browser output.
 */
export async function startNativeBridge({ outputMode = 'direct-hfp', onStatus = () => {}, onLevel = () => {}, onClose = () => {} } = {}) {
  if (!['direct-hfp', 'virtual-mic'].includes(outputMode)) throw new Error('Неизвестный способ передачи звука.');
  if (!['localhost', '127.0.0.1', '[::1]'].includes(location.hostname)) {
    throw new Error('Аудиомост разрешён только на localhost.');
  }
  const Context = window.AudioContext || window.webkitAudioContext;
  if (!Context || !window.AudioWorkletNode) throw new Error('Этот браузер не поддерживает AudioWorklet.');
  const ctx = new Context({ sampleRate: 48000, latencyHint: 'interactive' });
  let ws;
  let input;
  let destination;
  let silent;
  let closed = false;
  let closeReason;
  let formatReady = false;
  let audioReady = false;
  let rejectOpening;
  const closeListeners = new Set();
  const remotes = new Set();
  let openTimer;
  let heartbeatTimer;
  let diagnosticTimer;
  const diagnosticStats = { receivedPackets: 0, capturePeak: 0, captureFrames: 0, remotePackets: 0, remotePeak: 0, sentPackets: 0, stalePackets: 0 };
  const status = (message) => { try { onStatus(message); } catch (error) { console.error(error); } };
  const diagnostic = (message) => { console.info('[native-audio] ' + message); status(message); };
  const stop = (reason = 'Аудиомост остановлен') => {
    if (closed) return;
    closed = true;
    closeReason = reason;
    clearTimeout(openTimer);
    clearInterval(heartbeatTimer);
    clearInterval(diagnosticTimer);
    rejectOpening?.(new Error(reason));
    rejectOpening = undefined;
    for (const detach of [...remotes]) detach();
    input?.port.postMessage({ type: 'stop' });
    input?.port.close();
    try { input?.disconnect(); } catch {}
    try { silent?.disconnect(); } catch {}
    destination?.stream.getTracks().forEach(track => track.stop());
    try { if (ws && ws.readyState < WebSocket.CLOSING) ws.close(1000, 'bridge stopped'); } catch {}
    void ctx.close().catch(() => {});
    for (const listener of closeListeners) { try { listener(reason); } catch (error) { console.error(error); } }
    closeListeners.clear();
    try { onClose(reason); } catch (error) { console.error(error); }
  };
  const ensureOpen = () => {
    if (closed || !audioReady || ws?.readyState !== WebSocket.OPEN) throw new Error('Аудиомост не подключён.');
  };

  try {
    // Called directly from a click so browser autoplay policies can unlock it.
    await ctx.resume();
    if (ctx.sampleRate !== 48000) throw new Error('Браузер не открыл аудио 48000 Гц.');
    await ctx.audioWorklet.addModule('/native-worklet.js');
    destination = ctx.createMediaStreamDestination();
    input = new AudioWorkletNode(ctx, 'native-capture-source', {
      numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [1],
    });
    input.onprocessorerror = () => stop('Ошибка обработки речи собеседника в браузере.');
    input.port.onmessage = ({ data }) => {
      if (data?.type !== 'capture_stats') return;
      diagnosticStats.captureFrames += data.frames || 0;
      diagnosticStats.capturePeak = Math.max(diagnosticStats.capturePeak, data.peak || 0);
    };
    silent = ctx.createGain();
    silent.gain.value = 0;
    silent.connect(ctx.destination);
    input.connect(destination);
    input.connect(silent);

    const socketUrl = new URL('/audio', location.href);
    socketUrl.searchParams.set('mode', outputMode);
    socketUrl.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(socketUrl);
    ws.binaryType = 'arraybuffer';
    const connected = new Promise((resolve, reject) => {
      rejectOpening = reject;
      openTimer = setTimeout(() => stop('Аудиоустройства не готовы за 10 секунд.'), 10000);
      ws.onopen = () => {
        status('Локальное соединение открыто; проверка аудиоформата…');
        // Keep the server's receive timeout alive even during a silent meter test.
        heartbeatTimer = setInterval(() => {
          if (closed || ws.readyState !== WebSocket.OPEN) return;
          try { ws.send(JSON.stringify({ type: 'ping' })); }
          catch { stop('Не удалось проверить соединение с аудиомостом.'); }
        }, 5000);
      };
      ws.onmessage = ({ data }) => {
        if (closed) return;
        if (typeof data === 'string') {
          let message;
          try { message = JSON.parse(data); } catch { stop('Некорректный ответ аудиомоста.'); return; }
          if (message.type === 'format') {
            if (message.sampleRate !== 48000 || message.channels !== 1) {
              stop('Несовместимый формат локального аудиомоста.'); return;
            }
            formatReady = true;
            status('Формат проверен: 48000 Гц, один канал. Открываю аудиоустройства…');
          } else if (message.type === 'ready') {
            if (!formatReady) { stop('Аудиомост не подтвердил формат звука.'); return; }
            if (audioReady) return;
            audioReady = true;
            clearTimeout(openTimer);
            rejectOpening = undefined;
            const source = typeof message.source === 'string' && message.source.trim() ? message.source.trim() : 'звук звонка на компьютере';
            const output = typeof message.output === 'string' && message.output.trim() ? message.output.trim() : 'телефон';
            status(`Аудиомост готов. Речь собеседника: ${source}. Голос робота: ${output}.`);
            diagnosticTimer = setInterval(() => {
              // Only numeric counters; never tokens, call content, or raw PCM.
              console.info('[native-audio] audio ' + JSON.stringify({
                context: ctx.state,
                nativePackets: diagnosticStats.receivedPackets,
                captureFrames: diagnosticStats.captureFrames,
                capturePeak: Number(diagnosticStats.capturePeak.toFixed(4)),
                remotePackets: diagnosticStats.remotePackets,
                remotePeak: Number(diagnosticStats.remotePeak.toFixed(4)),
                sentPackets: diagnosticStats.sentPackets,
                stalePackets: diagnosticStats.stalePackets,
              }));
              diagnosticStats.capturePeak = diagnosticStats.remotePeak = 0;
            }, 5000);
            resolve();
          } else if (message.type === 'level') {
            try { onLevel(message); } catch (error) { console.error(error); }
          } else if (message.type === 'error') {
            const reason = message.message || message.error || 'Ошибка локального аудиомоста.';
            status(String(reason));
            stop(String(reason));
          } else if (message.type === 'status') {
            status(message.message || message.status || 'Обновление аудиомоста.');
          } else if (message.type === 'stopped') {
            const reasons = {
              time_limit: 'Аудиомост завершил сессию по лимиту диагностики.',
              user_stop: 'Аудиомост остановлен пользователем.',
              connection_closed: 'Локальное аудиосоединение закрыто.',
              slow_or_closed_connection: 'Соединение с аудиомостом прервалось или не успевает передавать звук.',
              server_shutdown: 'Локальная программа аудиомоста завершена.',
            };
            stop(reasons[message.reason] || message.reason || 'Локальный аудиомост остановлен.');
          }
          return;
        }
        if (!audioReady || !(data instanceof ArrayBuffer) || data.byteLength % 4 || data.byteLength > 48000 * 4) {
          stop('Некорректный PCM-пакет аудиомоста.'); return;
        }
        if (ctx.state !== 'running') {
          // Do not accumulate stale caller speech while browser audio is suspended.
          input.port.postMessage({ type: 'clear' });
          return;
        }
        diagnosticStats.receivedPackets++;
        input.port.postMessage(data, [data]);
      };
      ws.onerror = () => { status('Ошибка соединения с локальным аудиомостом.'); stop('Соединение с аудиомостом потеряно.'); };
      ws.onclose = (event) => stop(event.reason || 'Локальное аудиосоединение закрыто.');
    });
    await connected;
    if (closed) throw new Error('Аудиомост закрылся во время подключения.');
  } catch (error) {
    stop(error instanceof Error ? error.message : String(error));
    throw error;
  }

  return {
    stream: destination.stream,
    diagnostic,
    get closed() { return closed; },
    get closeReason() { return closeReason; },
    subscribeClose(listener) {
      if (closed) { queueMicrotask(() => listener('Аудиомост закрыт.')); return () => {}; }
      closeListeners.add(listener);
      return () => closeListeners.delete(listener);
    },
    async resume() { ensureOpen(); await ctx.resume(); },
    attachRemoteStream(stream) {
      ensureOpen();
      if (!stream?.getAudioTracks().some(track => track.readyState === 'live')) {
        throw new Error('Retell не предоставил живую аудиодорожку.');
      }
      // This single-call bridge must never mix duplicate remote streams.
      for (const detach of [...remotes]) detach();
      const remoteTrack = stream.getAudioTracks()[0];
      diagnostic(`Аудиодорожка Retell получена: ${remoteTrack.readyState}, muted=${remoteTrack.muted}, enabled=${remoteTrack.enabled}.`);
      // Chromium remote WebRTC streams may not feed WebAudio until a media
      // renderer is attached. Both official Retell transports attach/play one;
      // LiveKit's own WebAudio path keeps it muted. Never render to speakers.
      const renderer = document.createElement('audio');
      renderer.autoplay = true;
      renderer.muted = true;
      renderer.volume = 0;
      renderer.setAttribute('playsinline', '');
      renderer.srcObject = stream;
      const source = ctx.createMediaStreamSource(stream);
      const sink = new AudioWorkletNode(ctx, 'native-remote-sink', {
        numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1],
        channelCount: 1, channelCountMode: 'explicit',
      });
      sink.onprocessorerror = () => stop('Ошибка обработки голоса Retell в браузере.');
      let detached = false;
      let sawPacket = false;
      let sawSignal = false;
      const onUnmute = () => diagnostic('Аудиодорожка Retell начала получать данные.');
      const onMute = () => diagnostic('Аудиодорожка Retell временно не получает данные.');
      remoteTrack.addEventListener('unmute', onUnmute);
      remoteTrack.addEventListener('mute', onMute);
      const detach = () => {
        if (detached) return;
        detached = true;
        remotes.delete(detach);
        sink.port.onmessage = null;
        sink.port.postMessage({ type: 'stop' });
        sink.port.close();
        try { source.disconnect(); } catch {}
        try { sink.disconnect(); } catch {}
        remoteTrack.removeEventListener('unmute', onUnmute);
        remoteTrack.removeEventListener('mute', onMute);
        renderer.pause();
        renderer.srcObject = null;
      };
      remotes.add(detach);
      sink.port.onmessage = ({ data }) => {
        if (closed || detached || data?.type !== 'pcm' || ws.readyState !== WebSocket.OPEN) return;
        diagnosticStats.remotePackets++;
        diagnosticStats.remotePeak = Math.max(diagnosticStats.remotePeak, data.peak || 0);
        if (!sawPacket) { sawPacket = true; diagnostic('Обработка аудиодорожки Retell запущена.'); }
        if (!sawSignal && data.peak > 0.00001) {
          sawSignal = true;
          diagnostic('Получен ненулевой звук Retell; передаю его в телефон.');
        }
        // Audio worklet messages can queue when the main thread stalls. Skip stale speech.
        if (ctx.currentTime - data.time > 0.2) { diagnosticStats.stalePackets++; return; }
        // WebSocket cannot discard already queued data: stop instead of building delay.
        if (ws.bufferedAmount > 48000 * 4 * 0.2) {
          status('Переполнение очереди вывода; тест остановлен.');
          stop('Браузер не успевает отправлять звук в телефон.');
          return;
        }
        ws.send(data.buffer);
        diagnosticStats.sentPackets++;
      };
      source.connect(sink);
      sink.connect(silent);
      void renderer.play().then(() => {
        if (!closed && !detached) diagnostic('Приём звука Retell активирован в браузере.');
      }).catch(error => {
        if (!closed && !detached) diagnostic(`Браузер не запустил приём Retell: ${error?.name || 'ошибка воспроизведения'}.`);
      });
      void ctx.resume().catch(error => stop(error.message));
      return detach;
    },
    tone() { ensureOpen(); ws.send(JSON.stringify({ type: 'tone' })); },
    stop,
  };
}
