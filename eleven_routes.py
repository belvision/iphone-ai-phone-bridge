"""Local guarded endpoints. Retell remains the default and a separate adapter."""
import asyncio
from contextlib import suppress
import aiohttp
from aiohttp import web, WSMsgType
from eleven_store import ElevenStore, public_config, validate, DEFAULTS
from eleven_dialog import ElevenDialog, DialogError

ORIGINS = {'http://localhost:8766', 'http://127.0.0.1:8766'}


def check_origin(request):
    if request.headers.get('Origin') not in ORIGINS:
        raise web.HTTPForbidden(text='Only the local panel may use ElevenLabs')


class ElevenService:
    def __init__(self, server):
        self.server = server
        self.store = ElevenStore()
        self.http = None
        self.dialog = None

    async def start(self):
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, connect=12, sock_read=40))

    async def close(self):
        if self.dialog: await self.dialog.close()
        if self.http: await self.http.close()

    async def settings(self, request):
        if request.method == 'POST':
            check_origin(request)
            if self.server.bridge is not None or self.dialog:
                raise web.HTTPConflict(text='Stop the assistant before changing its settings')
            if request.content_length is None or request.content_length > 24000:
                raise web.HTTPBadRequest()
            try:
                changes = await request.json()
                if not isinstance(changes, dict): raise ValueError()
                sync = changes.pop('syncAgent', False)
                if not isinstance(sync, bool): raise ValueError()
                if set(changes)-set(DEFAULTS): raise ValueError()
                if sync:
                    current = await asyncio.to_thread(self.store.read)
                    current.update({k:v for k,v in changes.items() if k not in ('elevenKey','llmKey') or v})
                    candidate = validate(current)
                    if not public_config(candidate)['agentsReady']: raise ValueError()
                    await asyncio.wait_for(self.sync_agent(candidate),15)
                result = await asyncio.to_thread(self.store.save, changes)
            except DialogError as error:
                return web.json_response({'error':str(error)},status=502)
            except (aiohttp.ClientError, TimeoutError):
                return web.json_response({'error':'Не удалось обновить агента через прокси США. Настройки не сохранены.'},status=502)
            except (ValueError, OSError):
                return web.json_response({'error': 'Проверьте настройки ElevenLabs и адрес модели.'}, status=400)
        else:
            origin = request.headers.get('Origin')
            if origin is not None and origin not in ORIGINS or request.headers.get('Sec-Fetch-Site') == 'cross-site':
                raise web.HTTPForbidden()
            try: result = public_config(await asyncio.to_thread(self.store.read))
            except (ValueError, OSError):
                return web.json_response({'error': 'Не удалось прочитать настройки ElevenLabs.'}, status=500)
        return web.json_response(result)

    async def sync_agent(self, config):
        proxy = self.server.retell_relay.proxy
        if not proxy: raise DialogError('Для ElevenLabs нужен настроенный прокси США.')
        route = {'proxy':proxy['address'],'proxy_headers':{'Proxy-Authorization':aiohttp.encode_basic_auth(proxy['username'],proxy['password'])}}
        headers = {'xi-api-key':config['elevenKey']}
        url = 'https://api.elevenlabs.io/v1/convai/agents/' + config['agentId']
        async with self.http.get(url,headers=headers,allow_redirects=False,**route) as response:
            if response.status != 200: raise DialogError(f'Не удалось прочитать агента ElevenLabs: HTTP {response.status}.')
            data = await response.json()
        conversation = data['conversation_config']
        agent = conversation['agent']
        agent['language'] = 'ru'
        agent['first_message'] = config['greeting']
        agent['disable_first_message_interruptions'] = False
        agent['prompt']['prompt'] = config['prompt']
        conversation['turn']['mode'] = 'turn'
        conversation['turn']['turn_eagerness'] = 'eager'
        conversation['turn']['speculative_turn'] = True
        events = conversation['conversation'].setdefault('client_events',[])
        if 'interruption' not in events: events.append('interruption')
        platform = data['platform_settings']
        # The private, authenticated bridge supplies the owner's current prompt
        # on every call, including when the dashboard uses versioned drafts.
        override = platform['overrides']['conversation_config_override']['agent']
        override['language'] = True
        override['first_message'] = True
        override['prompt']['prompt'] = True
        async with self.http.patch(url,headers=headers,json={'conversation_config':conversation,
                'platform_settings':platform},allow_redirects=False,**route) as response:
            if response.status != 200: raise DialogError(f'Не удалось сохранить агента ElevenLabs: HTTP {response.status}.')

    def answered(self):
        state = self.server.phone_monitor.snapshot()
        return (state.get('available') and state.get('active') and state.get('callPhase') == 'connected'
                and state.get('audioOnComputer') and 0 <= state.get('ageMs', 9999) <= 2000)

    async def conversation(self, request):
        check_origin(request)
        mode = request.query.get('mode')
        if mode not in ('agents', 'custom'): raise web.HTTPBadRequest()
        bridge = self.server.bridge
        if not self.answered() or not bridge or not bridge.alive:
            raise web.HTTPConflict(text='An answered phone call and audio bridge are required')
        if self.dialog: raise web.HTTPConflict(text='One ElevenLabs session is already running')
        config = await asyncio.to_thread(self.store.read)
        if not public_config(config)['agentsReady' if mode == 'agents' else 'customReady']:
            raise web.HTTPConflict(text='Configure ElevenLabs before calling')
        sequence = self.server.phone_monitor.snapshot()['callSequence']
        socket = web.WebSocketResponse(heartbeat=10, receive_timeout=20, max_msg_size=1024)
        await socket.prepare(request)
        async def emit(message):
            if not socket.closed:
                await asyncio.wait_for(socket.send_json(message), 1)
        dialog = ElevenDialog(mode, config, bridge, self.http, self.server.retell_relay.proxy, emit)
        self.dialog = dialog
        async def run():
            try:
                await asyncio.wait_for(self.run_dialog(dialog, sequence), timeout=None)
                await emit({'type': 'ended'})
            except asyncio.CancelledError: raise
            except DialogError as exc: await emit({'type': 'error', 'message': str(exc)})
            except Exception: await emit({'type': 'error', 'message': 'Соединение ElevenLabs прервалось. Проверьте API и прокси.'})
            finally:
                await dialog.close()
                await socket.close()
        task = asyncio.create_task(run())
        try:
            async for message in socket:
                if message.type == WSMsgType.TEXT:
                    if message.data == 'stop': break
                    if message.data != 'ping': break
                    await socket.send_str('pong')
                elif message.type in (WSMsgType.ERROR, WSMsgType.CLOSE, WSMsgType.CLOSED): break
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError, Exception): await task
            await dialog.close()
            if self.dialog is dialog: self.dialog = None
        return socket

    async def run_dialog(self, dialog, sequence):
        # Independent phone guard also stops cloud sessions when browser polling
        # stalls or an old phone call is replaced by a new one.
        task = asyncio.create_task(dialog.run())
        try:
            while not task.done():
                await asyncio.sleep(.25)
                state = self.server.phone_monitor.snapshot()
                if not self.answered() or state.get('callSequence') != sequence or not dialog.bridge.alive:
                    break
            if task.done(): await task
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError): await task
