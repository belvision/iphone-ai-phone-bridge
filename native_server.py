"""Local-only HTTP/WebSocket endpoint for the bounded phone audio experiment."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import json
from pathlib import Path
import queue
import time

from aiohttp import web, WSMsgType

from native_audio import NativeAudio, WEB_RATE, MAX_SESSION_SECONDS
from phone_state import PhoneStateMonitor
from audio_routes import MicrophoneLease, MODES
from connection_store import ConnectionStore
from retell_relay import RetellRelay

ROOT = Path(__file__).resolve().parent
ALLOWED_HOSTS = {"localhost:8766", "127.0.0.1:8766"}
ALLOWED_ORIGINS = {"http://localhost:8766", "http://127.0.0.1:8766"}
STATIC_FILES = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/native-worklet.js": "native-worklet.js"}
MAX_FRAME_BYTES = 65536


class RateLimit:
    """Token bucket: permit normal 48 kHz float audio with bounded burst/jitter."""
    def __init__(self, bytes_per_second=WEB_RATE * 4 * 1.5, capacity=MAX_FRAME_BYTES):
        self.rate = bytes_per_second
        self.capacity = capacity
        self.tokens = capacity
        self.updated = time.monotonic()

    def consume(self, count):
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        if count > self.tokens:
            return False
        self.tokens -= count
        return True


@web.middleware
async def local_only(request, handler):
    if request.headers.get("Host", "").lower() not in ALLOWED_HOSTS:
        raise web.HTTPForbidden(text="Only localhost:8766 is allowed")
    response = await handler(request)
    if not response.prepared:
        response.headers.update({
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "frame-ancestors 'none'; base-uri 'none'; object-src 'none'",
        })
    return response


class BridgeServer:
    def __init__(self, audio_factory=NativeAudio, phone_monitor=None):
        self.audio_factory = audio_factory
        self.lock = asyncio.Lock()
        self.bridge = None
        self.websocket = None
        self.last_session = None
        self.phone_monitor = phone_monitor if phone_monitor is not None else PhoneStateMonitor()
        self.mic_lease = MicrophoneLease()
        self.lease_task = None
        self.connection_store = ConnectionStore()
        self.retell_relay = RetellRelay()

    async def network_status(self, request):
        return web.json_response(self.retell_relay.status())

    async def relay_retell(self, request):
        if request.headers.get("Origin") not in ALLOWED_ORIGINS:
            raise web.HTTPForbidden(text="Only the local panel may call Retell")
        if request.path == "/retell/v3/create-web-call":
            state = self.phone_monitor.snapshot()
            if not (state.get("available") and state.get("active") and state.get("callPhase") == "connected"
                    and state.get("audioOnComputer") and 0 <= state.get("ageMs", 9999) <= 2000
                    and self.bridge is not None and self.bridge.alive):
                return web.json_response({"error_message": "Разговор или аудиомост не подтверждён; Retell не запущен."}, status=409)
        return await self.retell_relay.handle(request)

    async def connection(self, request):
        origin = request.headers.get("Origin")
        if (origin is not None and origin not in ALLOWED_ORIGINS) or request.headers.get("Sec-Fetch-Site") == "cross-site":
            raise web.HTTPForbidden(text="Only the local panel may access the saved connection")
        if request.method == "POST":
            if origin not in ALLOWED_ORIGINS:
                raise web.HTTPForbidden(text="Only the local panel may save the connection")
            if request.content_length is None or request.content_length > 2048:
                raise web.HTTPBadRequest(text="Invalid connection data")
            try:
                await asyncio.to_thread(self.connection_store.save, await request.json())
            except ValueError as exc:
                return web.json_response({"error": str(exc)}, status=400)
            except OSError:
                return web.json_response({"error": "Не удалось сохранить подключение на этом компьютере."}, status=500)
            return web.json_response({"saved": True})
        try:
            connection = await asyncio.to_thread(self.connection_store.read)
        except (ValueError, OSError):
            return web.json_response({"error": "Не удалось открыть сохранённое подключение. Введите публичный ключ снова."}, status=500)
        return web.json_response({"saved": connection is not None, **(connection or {})})

    async def startup(self, app):
        await self.retell_relay.start()
        with suppress(Exception):
            await asyncio.to_thread(self.mic_lease.restore)
        await asyncio.to_thread(self.phone_monitor.start)
        self.lease_task = asyncio.create_task(self._watch_microphone())

    async def _watch_microphone(self):
        while True:
            await asyncio.sleep(5)
            if self.mic_lease.expired():
                if self.bridge:
                    self.bridge.reason = "microphone_lease_expired"
                    self.bridge.stop_event.set()
                with suppress(Exception):
                    await asyncio.to_thread(self.mic_lease.restore)

    async def audio_routes(self, request):
        return web.json_response(await asyncio.to_thread(self.mic_lease.status))

    async def route_command(self, request):
        if request.headers.get("Origin") not in ALLOWED_ORIGINS:
            raise web.HTTPForbidden(text="Only the local panel may change audio routing")
        if request.content_length is None or request.content_length > 512:
            raise web.HTTPBadRequest(text="Invalid route command")
        try:
            command = await request.json()
            action = command.get("action")
            if action == "heartbeat":
                self.mic_lease.heartbeat()
            elif action == "reset":
                if self.bridge is not None and self.bridge.alive:
                    await asyncio.to_thread(self.bridge.stop, "user_stop")
                    if self.bridge.alive:
                        raise RuntimeError("Аудиомост ещё завершается; микрофон будет восстановлен после отключения.")
                await asyncio.to_thread(self.mic_lease.restore)
            elif action == "prepare":
                mode = command.get("mode")
                if mode not in MODES:
                    raise ValueError("Неизвестный способ передачи звука.")
                if self.lock.locked():
                    raise RuntimeError("Сначала остановите аудиомост.")
                if mode == "virtual-mic" and not await asyncio.to_thread(self.mic_lease.ready):
                    state = self.phone_monitor.snapshot()
                    if state.get("callPhase") not in ("idle", "ended", "dialing"):
                        raise RuntimeError("Включите виртуальный микрофон до начала разговора. Затем наберите новый звонок.")
                await asyncio.to_thread(self.mic_lease.prepare, mode)
            else:
                raise ValueError("Неизвестная команда звука.")
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response(await asyncio.to_thread(self.mic_lease.status))

    async def phone_state(self, request):
        return web.json_response(self.phone_monitor.snapshot())

    async def health(self, request):
        return web.json_response({
            "ok": True,
            "audioActive": self.bridge is not None and self.bridge.alive,
            "maxSessionSeconds": MAX_SESSION_SECONDS,
            "sampleRate": WEB_RATE,
            "audioStats": self.bridge.stats(reset_peaks=False) if self.bridge is not None else None,
            "lastSession": self.last_session,
        })

    async def static(self, request):
        filename = STATIC_FILES.get(request.path)
        if not filename:
            raise web.HTTPNotFound()
        path = ROOT / filename
        if not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    async def _send_audio(self, ws, bridge):
        next_stats = time.monotonic() + 0.5
        try:
            while not ws.closed and bridge.alive and not bridge.stop_event.is_set():
                try:
                    packet = await asyncio.to_thread(bridge.capture_packets.get, True, 0.1)
                except queue.Empty:
                    packet = None
                if packet is not None:
                    await asyncio.wait_for(ws.send_bytes(packet), timeout=0.5)
                if time.monotonic() >= next_stats:
                    await asyncio.wait_for(ws.send_json(bridge.stats()), timeout=0.5)
                    next_stats = time.monotonic() + 0.5
            if not ws.closed:
                if bridge.error:
                    await asyncio.wait_for(ws.send_json({"type": "error", "message": bridge.error}), timeout=0.5)
                await asyncio.wait_for(ws.send_json({"type": "stopped", "reason": bridge.reason}), timeout=0.5)
                await ws.close()
        except (asyncio.TimeoutError, ConnectionError):
            bridge.stop_event.set()
            bridge.reason = "slow_or_closed_connection"
            await ws.close()

    async def audio(self, request):
        if request.headers.get("Origin") not in ALLOWED_ORIGINS:
            raise web.HTTPForbidden(text="WebSocket origin is not allowed")
        mode = request.query.get("mode", "direct-hfp")
        if mode not in MODES:
            raise web.HTTPBadRequest(text="Unknown audio route")
        if mode == "virtual-mic" and not await asyncio.to_thread(self.mic_lease.ready):
            raise web.HTTPConflict(text="Prepare the virtual microphone before calling")
        if self.lock.locked() or (self.bridge is not None and self.bridge.alive):
            raise web.HTTPConflict(text="One audio session is already running")
        await self.lock.acquire()
        ws = web.WebSocketResponse(max_msg_size=MAX_FRAME_BYTES, heartbeat=10, receive_timeout=15,
                                   timeout=1, compress=False)
        bridge = None
        sender = None
        try:
            await ws.prepare(request)
            self.websocket = ws
            bridge = self.audio_factory(output_mode=mode)
            self.bridge = bridge
            await ws.send_json({"type": "format", "sampleRate": WEB_RATE, "channels": 1})
            await asyncio.to_thread(bridge.start)
            await ws.send_json({"type": "ready", "source": bridge.source_name, "output": bridge.output_name,
                                "maxSessionSeconds": MAX_SESSION_SECONDS})
            sender = asyncio.create_task(self._send_audio(ws, bridge))
            byte_limit = RateLimit()
            # Also tolerate a worklet sending native 128-frame quanta (375/s).
            message_limit = RateLimit(bytes_per_second=500, capacity=600)
            async for message in ws:
                if not message_limit.consume(1):
                    raise ValueError("Too many WebSocket messages")
                if message.type == WSMsgType.BINARY:
                    if not byte_limit.consume(len(message.data)):
                        raise ValueError("Incoming audio rate exceeded")
                    bridge.push_audio(message.data)
                elif message.type == WSMsgType.TEXT:
                    if len(message.data) > 1024:
                        raise ValueError("Command is too long")
                    command = json.loads(message.data)
                    if not isinstance(command, dict):
                        raise ValueError("Expected a JSON command object")
                    if command.get("type") == "tone":
                        bridge.request_tone()
                    elif command.get("type") == "stop":
                        bridge.reason = "user_stop"
                        break
                    elif command.get("type") == "ping":
                        await ws.send_json({"type": "pong"})
                    else:
                        raise ValueError("Unknown command")
                elif message.type in (WSMsgType.ERROR, WSMsgType.CLOSE, WSMsgType.CLOSED):
                    break
        except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
            if ws.prepared and not ws.closed:
                with suppress(Exception):
                    await asyncio.wait_for(ws.send_json({"type": "error", "message": str(exc)}), timeout=0.5)
        finally:
            if sender:
                sender.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await sender
            if bridge:
                await asyncio.to_thread(bridge.stop, bridge.reason if bridge.stop_event.is_set() or bridge.reason == "user_stop" else "connection_closed")
                self.last_session = {
                    "reason": bridge.reason,
                    "error": bridge.error,
                    "stats": bridge.stats(reset_peaks=False),
                }
                if not bridge.alive:
                    self.bridge = None
            if not ws.closed:
                with suppress(Exception):
                    await ws.close()
            self.websocket = None
            self.lock.release()
        return ws

    async def cleanup(self, app):
        if self.lease_task:
            self.lease_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.lease_task
        await asyncio.to_thread(self.phone_monitor.stop)
        if self.bridge:
            self.bridge.stop_event.set()
        if self.websocket:
            await self.websocket.close()
        if self.bridge:
            await asyncio.to_thread(self.bridge.stop, "server_shutdown")
        with suppress(Exception):
            await asyncio.to_thread(self.mic_lease.restore)
        await self.retell_relay.close()


def create_app(audio_factory=NativeAudio, phone_monitor=None):
    server = BridgeServer(audio_factory, phone_monitor)
    app = web.Application(middlewares=[local_only], client_max_size=MAX_FRAME_BYTES)
    app.router.add_get("/health", server.health)
    app.router.add_get("/phone-state", server.phone_state)
    app.router.add_get("/connection", server.connection)
    app.router.add_post("/connection", server.connection)
    app.router.add_get("/network-status", server.network_status)
    app.router.add_route("*", "/retell/{tail:.*}", server.relay_retell)
    app.router.add_get("/audio-routes", server.audio_routes)
    app.router.add_post("/audio-route", server.route_command)
    app.router.add_get("/audio", server.audio)
    for route in STATIC_FILES:
        app.router.add_get(route, server.static)
    app.on_startup.append(server.startup)
    app.on_cleanup.append(server.cleanup)
    return app


if __name__ == "__main__":
    web.run_app(create_app(), host="127.0.0.1", port=8766, access_log=None, print=lambda text: print(text, flush=True))
