"""Minimal Agents and custom STT -> streaming LLM -> streaming TTS sessions.

PCM is paced in real time; barge-in invalidates queued audio and cancels
in-flight response generation. An optional, synchronous observer queues CRM
events without database or network work on the audio path.
"""
import asyncio
import base64
from contextlib import suppress
import json
import re
import time
from urllib.parse import urlsplit
import aiohttp
import numpy as np
from native_audio import StreamingDecimator, WEB_RATE, PACKET_FRAMES


class DialogError(Exception): pass

END_CALL_QUIET_SECONDS = .35
END_CALL_DRAIN_SECONDS = 25
PHONE_OUTPUT_TAIL_SECONDS = .25


class PcmPlayer:
    def __init__(self, bridge):
        self.bridge = bridge
        self.queue = asyncio.Queue(maxsize=1000)
        self.generation = 0
        self.pending = np.empty(0, np.float32)
        self.tail = 0.0
        self.task = asyncio.create_task(self.run())

    def interrupt(self):
        self.generation += 1
        self.pending = np.empty(0, np.float32)
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()
        self.bridge.output_queue.clear()
        self.tail = 0.0
        return self.generation

    @property
    def speaking(self):
        return not self.queue.empty() or time.monotonic() < self.tail

    async def push(self, pcm, rate, generation, *, backpressure=True):
        if generation != self.generation: return
        if rate not in (8000, 16000, 22050, 24000, 44100, 48000) or len(pcm) % 2:
            raise DialogError('ElevenLabs вернул несовместимый PCM-формат.')
        samples = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768
        if rate != WEB_RATE and samples.size:
            # Fixed-rate PCM chunks; telephone bandwidth is below the Nyquist
            # limit of all supported formats. Never decode compressed bytes as PCM.
            samples = np.interp(np.arange(round(samples.size * WEB_RATE / rate)) * rate / WEB_RATE,
                                np.arange(samples.size), samples).astype(np.float32)
        self.pending = np.concatenate((self.pending, samples))
        while self.pending.size >= PACKET_FRAMES:
            packet = self.pending[:PACKET_FRAMES].copy().astype('<f4').tobytes()
            self.pending = self.pending[PACKET_FRAMES:]
            if backpressure: await self.queue.put((generation, packet))
            else:
                try: self.queue.put_nowait((generation, packet))
                except asyncio.QueueFull: raise DialogError('Слишком большая очередь голоса агента; тест остановлен.')
            if generation != self.generation: return

    async def flush(self, generation):
        if generation == self.generation and self.pending.size:
            packet = np.pad(self.pending, (0, PACKET_FRAMES - self.pending.size)).astype('<f4').tobytes()
            self.pending = np.empty(0, np.float32)
            await self.queue.put((generation, packet))

    async def run(self):
        next_at = time.monotonic()
        while True:
            generation, packet = await self.queue.get()
            try:
                # Keep an absolute clock: resetting it after every Windows timer
                # overshoot would gradually slow 48 kHz audio to ~32 kHz.
                if time.monotonic() - next_at > .1: next_at = time.monotonic()
                await asyncio.sleep(max(0, next_at - time.monotonic()))
                if generation == self.generation and self.bridge.alive:
                    self.bridge.push_audio(packet)
                    self.tail = time.monotonic() + .06
                next_at += .02
            finally:
                self.queue.task_done()

    async def drain(self, timeout=END_CALL_DRAIN_SECONDS):
        """Wait for PCM and the native output queue; a device tail is best effort."""
        generation, deadline = self.generation, time.monotonic() + timeout
        try:
            await asyncio.wait_for(self.flush(generation), max(.001, deadline - time.monotonic()))
            await asyncio.wait_for(self.queue.join(), max(.001, deadline - time.monotonic()))
        except TimeoutError:
            return False
        empty_since = None
        while time.monotonic() < deadline:
            if generation != self.generation or not self.bridge.alive or self.task.done():
                return False
            queued, _ = self.bridge.output_queue.snapshot()
            if self.queue.empty() and not queued and time.monotonic() >= self.tail:
                if empty_since is None:
                    empty_since = time.monotonic()
                if time.monotonic() - empty_since >= PHONE_OUTPUT_TAIL_SECONDS:
                    return True
            else:
                empty_since = None
            await asyncio.sleep(.02)
        return False

    async def close(self):
        self.interrupt()
        self.task.cancel()
        with suppress(asyncio.CancelledError): await self.task


class ElevenDialog:
    def __init__(self, mode, config, bridge, http, proxy, emit, observer=None):
        self.mode, self.config, self.bridge = mode, config, bridge
        self.http, self.proxy, self.emit = http, proxy, emit
        self.input = asyncio.Queue(maxsize=15)
        self.resampler = StreamingDecimator()
        self.player = PcmPlayer(bridge)
        self.remote = None
        self.sender = None
        self.answer = None
        self.closed = False
        self.done = asyncio.get_running_loop().create_future()
        self.history = [{'role': 'system', 'content': config['prompt']}]
        self.turn_started = 0.0
        self.first_text = None
        self.first_audio = None
        self.interrupted_event = -1
        self.observer = observer
        self.observer_errors = 0
        self.end_call_requested = False
        self.end_call_audio_drained = False
        self._end_call_task = None
        self._last_audio_at = 0.0
        self._receive_finished = False
        self._receive_normal_close = False

    def observe(self, event):
        """The observer must only enqueue; a CRM failure must not drop audio."""
        if self.observer is None: return
        try: self.observer(event)
        except Exception:
            # Route finalization can report incomplete capture. Do not log
            # event contents or observer errors, which may contain caller data.
            self.observer_errors += 1

    def transcript(self, role, text, *, source):
        if isinstance(text, str) and text.strip():
            self.observe({'type': 'transcript', 'role': role, 'text': text.strip(), 'source': source})

    def feed(self, packet):
        if self.closed: return
        # Keep caller audio flowing for barge-in while the farewell drains.
        # A server that has already closed no longer consumes that input.
        if self.end_call_requested and self.sender is not None and self.sender.done(): return
        if self.sender is None and self.input.full():
            self.input.get_nowait()  # Keep only recent caller audio while connecting.
        try: self.input.put_nowait(packet)
        except asyncio.QueueFull:
            if not self.done.done(): self.done.set_exception(DialogError('Сервис не успевает принимать звук. Остановите тест.'))

    def route(self):
        if not self.proxy: raise DialogError('Для ElevenLabs нужен настроенный прокси США.')
        return {'proxy': self.proxy['address'], 'proxy_headers': {
            'Proxy-Authorization': aiohttp.encode_basic_auth(self.proxy['username'], self.proxy['password'])}}

    async def json_get(self, path, **params):
        async with self.http.get('https://api.elevenlabs.io' + path, params=params,
                headers={'xi-api-key': self.config['elevenKey']}, allow_redirects=False, **self.route()) as response:
            if response.status != 200: raise DialogError(f'ElevenLabs: HTTP {response.status}. Проверьте ключ, права и кредиты.')
            return await response.json()

    async def send_audio(self):
        pending = bytearray()
        speech_samples = silence_samples = 0
        speaking = False
        commit = False
        while True:
            packet = await self.input.get()
            samples = self.resampler.process(np.frombuffer(packet, dtype='<f4'))
            pcm = np.rint(np.clip(samples, -1, 1) * 32767).astype('<i2').tobytes()
            if self.mode == 'custom':
                rms = float(np.sqrt(np.mean(samples * samples))) if samples.size else 0
                if rms > .004:
                    speech_samples += samples.size
                    silence_samples = 0
                    if not speaking and speech_samples >= 1600:
                        speaking = True
                        if self.answer and not self.answer.done() or self.player.speaking:
                            self.cancel_answer()
                            await self.emit({'type':'interrupted'})
                else:
                    silence_samples += samples.size
                    if speaking and silence_samples >= 16 * self.config.get('replyPauseMs',350):
                        commit = True; speaking = False; speech_samples = 0
                    elif not speaking: speech_samples = 0
            pending.extend(pcm)
            if len(pending) < 3200 and not commit: continue  # Flush a completed turn immediately.
            audio = base64.b64encode(pending).decode('ascii')
            pending.clear()
            if self.mode == 'agents': payload = {'user_audio_chunk': audio}
            else:
                payload = {'message_type': 'input_audio_chunk', 'audio_base_64': audio, 'sample_rate': 16000}
                if commit: payload['commit'] = True; commit = False
            await self.remote.send_json(payload)

    async def run(self):
        try:
            if self.mode == 'agents':
                data = await self.json_get('/v1/convai/conversation/get-signed-url', agent_id=self.config['agentId'])
                url = data.get('signed_url', '')
                parsed = urlsplit(url)
                if parsed.scheme != 'wss' or parsed.hostname != 'api.elevenlabs.io' or parsed.path != '/v1/convai/conversation':
                    raise DialogError('ElevenLabs вернул некорректный адрес подключения.')
                self.remote = await self.http.ws_connect(url, heartbeat=15, max_msg_size=4*1024*1024, **self.route())
                override = {'agent': {'language':'ru', 'first_message':self.config['greeting'],
                                      'prompt':{'prompt':self.config['prompt']}}}
                if self.config['voiceId']:
                    override['tts'] = {'voice_id':self.config['voiceId']}
                await self.remote.send_json({'type': 'conversation_initiation_client_data',
                                             'conversation_config_override': override})
            else:
                self.remote = await self.http.ws_connect('wss://api.elevenlabs.io/v1/speech-to-text/realtime',
                    params={'model_id': 'scribe_v2_realtime', 'audio_format': 'pcm_16000', 'language_code': 'ru',
                            'commit_strategy': 'manual'},
                    headers={'xi-api-key': self.config['elevenKey']}, heartbeat=15,
                    max_msg_size=1024*1024, **self.route())
            receiver = asyncio.create_task(self.receive())
            try:
                done, _ = await asyncio.wait((receiver, self.done), return_when=asyncio.FIRST_COMPLETED)
                for item in done: await item
                if receiver in done and self.end_call_requested and self._end_call_task:
                    await self._end_call_task
            finally:
                receiver.cancel()
                with suppress(asyncio.CancelledError): await receiver
        except (aiohttp.ClientError, TimeoutError):
            raise DialogError('Нет соединения с ElevenLabs. Проверьте прокси, ключ и кредиты.') from None
        finally: await self.close()

    async def ready(self):
        if self.sender: return
        self.sender = asyncio.create_task(self.send_audio())
        def sender_done(task):
            if not task.cancelled() and task.exception() and not self.done.done() and not self.end_call_requested:
                self.done.set_exception(DialogError('Передача голоса в ElevenLabs прервалась.'))
        self.sender.add_done_callback(sender_done)
        await self.emit({'type': 'live'})
        if self.mode == 'custom': self.start_answer(self.config['greeting'], greeting=True)

    async def receive(self):
        try:
            await self._receive_messages()
        finally:
            self._receive_finished = True
            self._receive_normal_close = getattr(self.remote, 'close_code', None) == 1000

    def request_end_call(self, event):
        """Only a successful authenticated server system-tool result is an intent to hang up."""
        if (self.mode != 'agents' or not isinstance(event, dict) or event.get('tool_name') != 'end_call'
                or event.get('tool_type') != 'system' or event.get('is_error') is not False
                or ('is_blocked' in event and event['is_blocked'] is not False)
                or ('is_called' in event and event['is_called'] is not True)
                or ('status' in event and event['status'] != 'success')):
            return False
        if self.end_call_requested:
            return True
        self.end_call_requested = True
        self.end_call_audio_drained = False
        self._last_audio_at = time.monotonic()
        self._end_call_task = asyncio.create_task(self._finish_agent_end())
        return True

    async def _finish_agent_end(self):
        deadline = time.monotonic() + END_CALL_DRAIN_SECONDS
        generation = self.player.generation
        while self.end_call_requested and time.monotonic() < deadline:
            if generation != self.player.generation or not self.bridge.alive:
                break
            if self._receive_finished and not self._receive_normal_close:
                break  # A network failure must not become a successful local hang-up.
            if not self._receive_finished and time.monotonic() - self._last_audio_at < END_CALL_QUIET_SECONDS:
                await asyncio.sleep(.02)
                continue
            if not await self.player.drain(max(.001, deadline - time.monotonic())):
                break
            # The receiver stays live during drain. A late audio chunk restarts settling.
            if time.monotonic() - self._last_audio_at < END_CALL_QUIET_SECONDS:
                continue
            if self._receive_finished and not self._receive_normal_close:
                break
            if self.end_call_requested and generation == self.player.generation:
                self.end_call_audio_drained = True
            break
        if not self.done.done():
            self.done.set_result(None)

    async def _receive_messages(self):
        rate = 16000
        async for frame in self.remote:
            if frame.type != aiohttp.WSMsgType.TEXT:
                if frame.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR): break
                continue
            message = json.loads(frame.data)
            kind = message.get('type') if self.mode == 'agents' else message.get('message_type')
            if kind == 'conversation_initiation_metadata':
                meta = message['conversation_initiation_metadata_event']
                conversation_id = meta.get('conversation_id')
                if isinstance(conversation_id, str) and conversation_id:
                    self.observe({'type': 'conversation_id', 'conversation_id': conversation_id})
                if meta.get('user_input_audio_format') != 'pcm_16000':
                    raise DialogError('У агента ElevenLabs выберите вход PCM 16000 Гц.')
                match = re.fullmatch(r'pcm_(8000|16000|22050|24000|44100|48000)', meta.get('agent_output_audio_format', ''))
                if not match: raise DialogError('У агента ElevenLabs выберите выход PCM.')
                rate = int(match[1]); await self.ready()
            elif kind == 'session_started': await self.ready()
            elif kind == 'ping': await self.remote.send_json({'type': 'pong', 'event_id': message['ping_event']['event_id']})
            elif kind == 'interruption':
                self.end_call_requested = False
                self.end_call_audio_drained = False
                if self._end_call_task:
                    self._end_call_task.cancel()
                    self._end_call_task = None
                self.interrupted_event = max(self.interrupted_event, message.get('interruption_event', {}).get('event_id', -1))
                self.player.interrupt()
            elif kind == 'audio':
                event = message['audio_event']
                if event.get('event_id', 0) <= self.interrupted_event: continue
                self._last_audio_at = time.monotonic()
                await self.player.push(base64.b64decode(event['audio_base_64'], validate=True), rate,
                                       self.player.generation, backpressure=False)
                await self.player.flush(self.player.generation)
            elif kind == 'partial_transcript' and message.get('text', '').strip():
                if self.answer and not self.answer.done() or self.player.speaking:
                    self.cancel_answer()
                    await self.emit({'type': 'interrupted'})
            elif kind == 'committed_transcript' and message.get('text', '').strip():
                # Scribe may also send timestamp/edited events for this same
                # segment. Only the stable committed event creates a CRM turn.
                self.transcript('user', message['text'], source='transcribed')
                self.start_answer(message['text'].strip())
            elif kind in ('user_transcript', 'agent_response'):
                if kind == 'user_transcript':
                    event = message.get('user_transcription_event') or message.get('user_transcript_event') or {}
                    self.transcript('user', event.get('user_transcript'), source='transcribed')
                else:
                    event = message.get('agent_response_event') or {}
                    self.transcript('agent', event.get('agent_response'), source='generated')
                # Activity logs still contain no conversation text.
                await self.emit({'type': 'activity', 'speaker': 'user' if kind == 'user_transcript' else 'agent'})
            elif kind == 'agent_response_correction':
                event = message.get('agent_response_correction_event') or {}
                original, corrected = event.get('original_agent_response'), event.get('corrected_agent_response')
                if isinstance(original, str) and isinstance(corrected, str):
                    self.observe({'type': 'transcript_correction', 'role': 'agent',
                                  'original_text': original.strip(), 'text': corrected.strip(),
                                  'source': 'generated'})
            elif kind == 'agent_tool_response':
                self.request_end_call(message.get('agent_tool_response'))
            elif kind and (kind.endswith('_error') or kind in ('error', 'rate_limited', 'quota_exceeded')):
                raise DialogError('ElevenLabs: ' + str(kind)[:80] + '. Проверьте тариф и права API.')
            elif kind == 'client_tool_call':
                tool = message.get('client_tool_call', {})
                await self.remote.send_json({'type': 'client_tool_result', 'tool_call_id': tool.get('tool_call_id'),
                                             'is_error': True, 'result': 'Client tools are not available in this phone bridge.'})

    def cancel_answer(self):
        if self.answer and not self.answer.done(): self.answer.cancel()
        self.player.interrupt()

    def start_answer(self, text, greeting=False):
        self.cancel_answer()
        self.turn_started = time.monotonic()
        self.first_text = self.first_audio = None
        self.answer = asyncio.create_task(self.respond(text, greeting, self.player.generation))
        def ended(task):
            if not task.cancelled() and task.exception() and not self.done.done():
                problem = task.exception()
                message = str(problem) if isinstance(problem, DialogError) else 'Ошибка ответа/голоса (' + type(problem).__name__ + '). Проверьте API и прокси.'
                self.done.set_exception(DialogError(message))
        self.answer.add_done_callback(ended)

    async def llm_sentences(self, text):
        self.history.append({'role': 'user', 'content': text[:4000]})
        self.history = [self.history[0]] + self.history[1:][-20:]
        payload = {'model': self.config['llmModel'], 'messages': self.history, 'stream': True, 'max_tokens': 220}
        # DeepSeek is intentionally direct. Never send this API key to ElevenLabs
        # or copy the proxy's credentials into an upstream Authorization header.
        if urlsplit(self.config['llmUrl']).hostname == 'api.deepseek.com': payload['thinking'] = {'type': 'disabled'}
        headers = {'Authorization': 'Bearer ' + self.config['llmKey']}
        route = {} if urlsplit(self.config['llmUrl']).hostname == 'api.deepseek.com' else self.route()
        buffer, full = '', ''
        async with self.http.post(self.config['llmUrl'], headers=headers, json=payload, allow_redirects=False, **route) as response:
            if response.status != 200: raise DialogError(f'Модель ответов: HTTP {response.status}.')
            async for raw in response.content:
                if not raw.startswith(b'data:'): continue
                data = raw[5:].strip()
                if data == b'[DONE]': break
                chunk = (json.loads(data).get('choices') or [{}])[0].get('delta', {}).get('content') or ''
                if not chunk: continue
                if self.first_text is None: self.first_text = time.monotonic() - self.turn_started
                buffer += chunk; full += chunk
                # Speak a completed sentence; don't wait for the entire answer.
                while True:
                    match = re.search(r'[.!?](?:\s|$)', buffer)
                    if not match or match.end() < 12: break
                    phrase, buffer = buffer[:match.end()].strip(), buffer[match.end():]
                    yield phrase
                if len(buffer) > 130:
                    split = buffer.rfind(' ', 40, 130)
                    if split > 0:
                        yield buffer[:split]; buffer = buffer[split:]
        if buffer.strip(): yield buffer.strip()
        if full.strip(): self.history.append({'role': 'assistant', 'content': full[:2000]})

    async def tts(self, text, generation):
        path = f"https://api.elevenlabs.io/v1/text-to-speech/{self.config['voiceId']}/stream"
        async with self.http.post(path, params={'output_format': 'pcm_24000'},
                headers={'xi-api-key': self.config['elevenKey']},
                json={'text': text, 'model_id': self.config['ttsModel'], 'language_code': 'ru'},
                allow_redirects=False, **self.route()) as response:
            if response.status != 200: raise DialogError(f'Озвучка ElevenLabs: HTTP {response.status}.')
            tail = b''
            observed = False
            async for chunk in response.content.iter_chunked(1920):
                if generation != self.player.generation: return
                chunk = tail + chunk; usable = len(chunk) // 2 * 2
                tail = chunk[usable:]
                if not usable: continue
                if not observed:
                    # The complete phrase was generated and audio has started,
                    # but interruption can prevent part of it reaching the caller.
                    self.transcript('agent', text, source='generated')
                    observed = True
                if self.first_audio is None:
                    self.first_audio = time.monotonic() - self.turn_started
                    await self.emit({'type': 'latency', 'textSeconds': self.first_text, 'audioSeconds': self.first_audio})
                await self.player.push(chunk[:usable], 24000, generation)
            if tail: raise DialogError('Неполный аудиопакет ElevenLabs.')
            await self.player.flush(generation)

    async def respond(self, text, greeting, generation):
        if greeting:
            self.history.append({'role': 'assistant', 'content': text})
            await self.tts(text, generation)
            return
        sentences = asyncio.Queue(maxsize=4)
        async def produce():
            try:
                async for sentence in self.llm_sentences(text): await sentences.put(sentence)
            except asyncio.CancelledError: raise
            except Exception as error:
                await sentences.put(error)
            await sentences.put(None)
        producer = asyncio.create_task(produce())
        try:
            while True:
                sentence = await sentences.get()
                if sentence is None: break
                if isinstance(sentence, Exception): raise sentence
                await self.tts(sentence, generation)
            await producer
        finally:
            producer.cancel()
            with suppress(asyncio.CancelledError): await producer

    async def close(self):
        if self.closed: return
        self.closed = True
        if self._end_call_task:
            self._end_call_task.cancel()
            with suppress(asyncio.CancelledError): await self._end_call_task
        if self.answer: self.answer.cancel()
        if self.sender: self.sender.cancel()
        for task in (self.answer, self.sender):
            if task:
                with suppress(asyncio.CancelledError, Exception): await task
        if self.remote: await self.remote.close()
        if not self.done.done(): self.done.cancel()
        elif not self.done.cancelled(): self.done.exception()
        await self.player.close()
        self.history.clear()
