/**
 * Experimental custom-MediaStream adapter for retell-client-js-sdk 3.0.2.
 * Signaling/event contracts follow that installed SDK (ISC, Retell AI).
 * The SDK's ControlApi and WebCallSession remain the source of API/auth logic.
 * These small transports replace only capture/playback; no global API patches.
 */
import { ControlApi } from './node_modules/retell-client-js-sdk/src/control/api';
import { WebCallSession } from './node_modules/retell-client-js-sdk/src/session/web-call-session';
import { DATA_CHANNEL_EVENTS, SessionHooks } from './node_modules/retell-client-js-sdk/src/session/events';
import { StartCallConfig, Transport, TransportHandlers, selectTransport } from './node_modules/retell-client-js-sdk/src/transport';
import { SDK_VERSION } from './node_modules/retell-client-js-sdk/src/version';
import { LocalAudioTrack, RemoteAudioTrack, Room, RoomEvent, Track } from 'livekit-client';

export interface NativeAudioBridge {
  stream: MediaStream;
  readonly closed: boolean;
  diagnostic?(message: string): void;
  attachRemoteStream(stream: MediaStream): () => void;
  subscribeClose(listener: (reason: string) => void): () => void;
  resume(): Promise<void>;
  stop(reason?: string): void;
}

function cloneCapture(bridge: NativeAudioBridge): MediaStreamTrack {
  const track = bridge.stream.getAudioTracks()[0];
  if (bridge.closed || !track || track.readyState !== 'live') throw new Error('Локальный аудиомост не готов.');
  return track.clone();
}

class NativeGatewayTransport implements Transport {
  private pc?: RTCPeerConnection;
  private dc?: RTCDataChannel;
  private local?: MediaStream;
  private detachRemote?: () => void;
  private sessionId?: string;
  private candidates: RTCIceCandidate[] = [];
  private closed = false;
  private readonly base: string;
  private readonly controller = new AbortController();
  private disconnectedTimer?: ReturnType<typeof setTimeout>;
  private statsTimer?: ReturnType<typeof setInterval>;
  constructor(private config: StartCallConfig, private bridge: NativeAudioBridge) {
    this.base = `${(config.baseURL || 'https://api.retellai.com').replace(/\/+$/, '')}/webrtc-proxy/${encodeURIComponent(config.callId || '')}`;
  }
  private headers(extra: Record<string, string> = {}) {
    return { Authorization: `Bearer ${this.config.accessToken}`, 'X-Retell-Client-JS-SDK-Version': SDK_VERSION, ...extra };
  }
  private checkOpen() { if (this.closed || this.bridge.closed) throw new Error('Аудиосоединение закрыто.'); }
  private deleteSession(id: string) {
    void fetch(`${this.base}/v1/webrtc/sessions/${encodeURIComponent(id)}`, {
      method: 'DELETE', headers: this.headers(), keepalive: true,
    }).catch(() => {});
  }
  async connect(handlers: TransportHandlers): Promise<void> {
    if (!this.config.callId || !this.config.accessToken) throw new Error('Не получены параметры WebRTC Retell.');
    this.checkOpen();
    const pc = new RTCPeerConnection({ iceServers: this.config.iceServers?.length ? this.config.iceServers : [{ urls: 'stun:stun.l.google.com:19302' }] });
    this.pc = pc;
    this.local = new MediaStream([cloneCapture(this.bridge)]);
    for (const track of this.local.getAudioTracks()) pc.addTrack(track, this.local);
    const dc = pc.createDataChannel('control');
    this.dc = dc;
    dc.onmessage = ({ data }) => { try { handlers.onData(JSON.parse(data)); } catch {} };
    let connected = false;
    let ready = false;
    pc.ontrack = (event) => {
      if (this.closed || event.track.kind !== 'audio') return;
      this.bridge.diagnostic?.('Retell WebRTC: получена аудиодорожка агента.');
      try {
        this.detachRemote?.();
        this.detachRemote = this.bridge.attachRemoteStream(event.streams[0] || new MediaStream([event.track]));
        if (!ready) { ready = true; handlers.onCallReady(null); }
      } catch (error) { handlers.onError(String(error)); handlers.onDisconnected(); }
    };
    pc.onicecandidate = ({ candidate }) => {
      if (!candidate || this.closed) return;
      if (this.sessionId) void this.sendCandidate(candidate, handlers);
      else this.candidates.push(candidate);
    };
    const stateChanged = () => {
      if (this.closed || !connected) return;
      this.bridge.diagnostic?.(`Retell WebRTC: соединение ${pc.connectionState}.`);
      clearTimeout(this.disconnectedTimer);
      if (pc.connectionState === 'failed' || pc.connectionState === 'closed') handlers.onDisconnected();
      else if (pc.connectionState === 'disconnected') {
        this.disconnectedTimer = setTimeout(() => {
          if (pc.connectionState === 'disconnected') handlers.onDisconnected();
        }, 5000);
      }
    };
    pc.onconnectionstatechange = stateChanged;
    const offer = await pc.createOffer();
    this.checkOpen();
    await pc.setLocalDescription(offer);
    const timeout = setTimeout(() => this.controller.abort(), 15000);
    try {
      const response = await fetch(`${this.base}/v1/webrtc/sessions`, {
        method: 'POST', headers: this.headers({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({ identity: this.config.identity || 'client', sdp: pc.localDescription!.sdp }),
        signal: this.controller.signal,
      });
      if (!response.ok) throw new Error(`Retell WebRTC: HTTP ${response.status}`);
      const answer = await response.json();
      if (typeof answer.session_id !== 'string' || typeof answer.sdp !== 'string') throw new Error('Некорректный WebRTC-ответ Retell.');
      if (this.closed) { this.deleteSession(answer.session_id); throw new Error('Соединение отменено.'); }
      this.sessionId = answer.session_id;
      connected = true;
      handlers.onConnected();
      await pc.setRemoteDescription({ type: 'answer', sdp: answer.sdp });
      this.checkOpen();
      for (const candidate of this.candidates.splice(0)) void this.sendCandidate(candidate, handlers);
      this.statsTimer = setInterval(() => {
        if (this.closed) return;
        void pc.getStats().then(report => {
          let sent = 0, received = 0, energy = 0;
          report.forEach(stat => {
            if (stat.kind !== 'audio' && stat.mediaType !== 'audio') return;
            if (stat.type === 'outbound-rtp') sent += stat.bytesSent || 0;
            if (stat.type === 'inbound-rtp') { received += stat.bytesReceived || 0; energy += stat.totalAudioEnergy || 0; }
          });
          console.info('[native-retell] gateway audio ' + JSON.stringify({ state: pc.connectionState, sent, received, energy }));
        }).catch(() => {});
      }, 5000);
    } finally { clearTimeout(timeout); }
  }
  private async sendCandidate(candidate: RTCIceCandidate, handlers: TransportHandlers) {
    if (!this.sessionId || this.closed) return;
    try {
      const response = await fetch(`${this.base}/v1/webrtc/sessions/${encodeURIComponent(this.sessionId)}`, {
        method: 'PATCH', headers: this.headers({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({ candidate: candidate.toJSON() }), signal: this.controller.signal,
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
    } catch (error) { if (!this.closed) handlers.onError(`Retell ICE: ${String(error)}`); }
  }
  setMicEnabled(enabled: boolean) { this.local?.getAudioTracks().forEach(track => { track.enabled = enabled; }); }
  async resumeAudioPlayback() { await this.bridge.resume(); }
  close() {
    if (this.closed) return;
    this.closed = true;
    clearTimeout(this.disconnectedTimer);
    clearInterval(this.statsTimer);
    this.controller.abort();
    if (this.sessionId) this.deleteSession(this.sessionId);
    this.detachRemote?.();
    this.local?.getTracks().forEach(track => track.stop());
    this.dc?.close();
    this.pc?.close();
    this.candidates = [];
    this.sessionId = undefined;
    this.detachRemote = undefined;
    this.local = undefined;
    this.dc = undefined;
    this.pc = undefined;
  }
}

class NativeLiveKitTransport implements Transport {
  private room?: Room;
  private mic?: LocalAudioTrack;
  private detachRemote?: () => void;
  private closed = false;
  private statsTimer?: ReturnType<typeof setInterval>;
  constructor(private config: StartCallConfig, private bridge: NativeAudioBridge) {}
  async connect(handlers: TransportHandlers): Promise<void> {
    if (!this.config.accessToken) throw new Error('Не получен токен LiveKit Retell.');
    if (this.closed || this.bridge.closed) throw new Error('Аудиомост закрыт.');
    const room = new Room();
    this.room = room;
    let connected = false;
    let ready = false;
    room.on(RoomEvent.Disconnected, () => { if (connected && !this.closed) handlers.onDisconnected(); });
    room.on(RoomEvent.TrackSubscribed, (track) => {
      if (this.closed || !(track instanceof RemoteAudioTrack)) return;
      this.bridge.diagnostic?.('Retell LiveKit: получена аудиодорожка агента.');
      try {
        this.detachRemote?.();
        this.detachRemote = this.bridge.attachRemoteStream(new MediaStream([track.mediaStreamTrack]));
        if (!ready) { ready = true; handlers.onCallReady(null); }
        clearInterval(this.statsTimer);
        this.statsTimer = setInterval(() => {
          if (this.closed) return;
          void track.getReceiverStats().then(stats => {
            if (stats) console.info('[native-retell] livekit audio ' + JSON.stringify({ received: stats.bytesReceived, energy: stats.totalAudioEnergy }));
          }).catch(() => {});
        }, 5000);
      } catch (error) { handlers.onError(String(error)); handlers.onDisconnected(); }
    });
    room.on(RoomEvent.TrackUnsubscribed, (track) => {
      if (track instanceof RemoteAudioTrack) {
        this.bridge.diagnostic?.('Retell LiveKit: аудиодорожка агента отключена.');
        clearInterval(this.statsTimer);
        this.detachRemote?.(); this.detachRemote = undefined;
      }
    });
    room.on(RoomEvent.DataReceived, (payload, participant) => {
      if (participant?.identity !== 'server') return;
      try { handlers.onData(JSON.parse(new TextDecoder().decode(payload))); } catch {}
    });
    await room.connect(this.config.url || 'wss://retell-ai-4ihahnq7.livekit.cloud', this.config.accessToken);
    if (this.closed || this.bridge.closed) { void room.disconnect(); throw new Error('Соединение отменено.'); }
    // Mark userProvidedTrack to prevent LiveKit ever reacquiring a physical mic.
    this.mic = new LocalAudioTrack(cloneCapture(this.bridge), { channelCount: 1, sampleRate: 48000 }, true);
    await room.localParticipant.publishTrack(this.mic, { source: Track.Source.Microphone, name: 'microphone' });
    if (this.closed || this.bridge.closed) throw new Error('Соединение отменено.');
    connected = true;
    this.bridge.diagnostic?.('Retell LiveKit: звук собеседника опубликован.');
    handlers.onConnected();
  }
  setMicEnabled(enabled: boolean) {
    // Track toggle does not invoke getUserMedia or change the owned source.
    if (this.mic) this.mic.mediaStreamTrack.enabled = enabled;
  }
  async resumeAudioPlayback() { await this.bridge.resume(); }
  close() {
    if (this.closed) return;
    this.closed = true;
    clearInterval(this.statsTimer);
    this.detachRemote?.();
    this.detachRemote = undefined;
    this.mic?.stop();
    this.mic = undefined;
    if (this.room) void this.room.disconnect().catch(() => {});
    this.room = undefined;
  }
}

class NativeWebCallSession extends WebCallSession {
  constructor(api: ControlApi, agentId: string, private bridge: NativeAudioBridge, hooks?: SessionHooks, recaptchaToken?: string) {
    super(api, { agent_id: agentId, hooks, recaptchaToken });
  }
  protected async connectTransport(config: StartCallConfig): Promise<void> {
    if (this.ended || this.bridge.closed) throw new Error('Аудиомост закрыт.');
    const kind = selectTransport(config, 'livekit');
    this.bridge.diagnostic?.(`Retell: подключение через ${kind}.`);
    const transport: Transport = kind === 'gateway'
      ? new NativeGatewayTransport(config, this.bridge)
      : new NativeLiveKitTransport(config, this.bridge);
    this.transport = transport;
    const current = () => !this.ended && this.transport === transport;
    try {
      await transport.connect({
        onConnected: () => {}, onCallReady: () => {},
        onData: (event) => {
          if (!current()) return;
          if (event?.type === 'status') {
            if (event.state === 'ended' || event.state === 'replaced') this.finish({});
            return;
          }
          const name = DATA_CHANNEL_EVENTS[event?.event_type];
          if (name === 'agent_start_talking' || name === 'agent_stop_talking') {
            this.isAgentTalking = name === 'agent_start_talking';
            this.bridge.diagnostic?.(this.isAgentTalking ? 'Retell: агент начал ответ.' : 'Retell: агент закончил ответ.');
            this.emit(name);
          } else if (name === 'update' || name === 'metadata' || name === 'node_transition') {
            this.emit(name, event);
          }
        },
        onDisconnected: () => {
          if (!current()) return;
          // A normal Retell status=ended/replaced already calls finish() above.
          // Losing the transport first is ambiguous: stop continuous mode rather
          // than silently treating a failed connection as a completed session.
          this.emit('error', new Error('Соединение с Retell прервалось без подтверждения завершения.'));
          this.finish({});
        },
        onError: (message) => { if (current()) this.emit('error', new Error(message)); },
      });
    } catch (error) {
      if (this.transport === transport) this.dropTransport(); else transport.close();
      throw error;
    }
    if (this.ended || this.bridge.closed) throw new Error('Соединение завершено.');
  }
}

/** Starts a billable web call only when explicitly invoked by the UI. */
export function createNativeRetellCall({ key, agentId, bridge, hooks, recaptchaToken }: {
  key: string; agentId: string; bridge: NativeAudioBridge; hooks?: SessionHooks; recaptchaToken?: string;
}): WebCallSession {
  if (!key?.startsWith('public_key_')) throw new Error('Нужен Public Key Retell, а не секретный API-ключ.');
  if (!agentId?.startsWith('agent_')) throw new Error('Некорректный Agent ID.');
  if (bridge.closed || !bridge.stream.getAudioTracks().some(track => track.readyState === 'live')) throw new Error('Сначала запустите аудиомост.');
  const session = new NativeWebCallSession(new ControlApi({ auth: { key }, baseURL: `${location.origin}/retell` }), agentId, bridge, hooks, recaptchaToken);
  const unsubscribe = bridge.subscribeClose(() => { void session.end(); });
  session.once('end', () => { unsubscribe(); bridge.stop('Звонок Retell завершён.'); });
  return session;
}
