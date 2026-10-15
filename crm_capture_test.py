"""Offline transcript capture checks: no provider calls or audio devices."""
import asyncio
import json
import time
import unittest
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiohttp

from eleven_dialog import DialogError, ElevenDialog


class FakeSocket:
    def __init__(self, events):
        self.events = events
        self.send_json = AsyncMock()
        self.close = AsyncMock()

    async def __aiter__(self):
        for event in self.events:
            yield SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(event))


class FakeResponse:
    def __init__(self, chunks=(), status=200, after_audio=None):
        self.status = status
        self.chunks, self.after_audio = chunks, after_audio
        self.content = self

    async def __aenter__(self): return self
    async def __aexit__(self, *_): pass

    async def iter_chunked(self, _):
        for chunk in self.chunks:
            yield chunk
        if self.after_audio is not None:
            self.after_audio.set()
            await asyncio.Future()


class CaptureTests(unittest.IsolatedAsyncioTestCase):
    def dialog(self, mode='agents', observer=None, response=None):
        self.events, self.activity = [], []
        bridge = SimpleNamespace(alive=True, output_queue=SimpleNamespace(clear=lambda: None),
                                 push_audio=lambda _: None)
        async def emit(event): self.activity.append(event)
        config = {'prompt': 'Private instruction, never a transcript', 'greeting': 'Здравствуйте!',
                  'voiceId': 'test', 'ttsModel': 'test', 'elevenKey': 'test-secret'}
        http = SimpleNamespace(post=Mock(return_value=response))
        dialog = ElevenDialog(mode, config, bridge, http, None, emit,
                              self.events.append if observer is None else observer)
        dialog.route = lambda: {}
        dialog.turn_started = time.monotonic()
        return dialog

    async def test_hosted_metadata_transcript_and_interruption_correction(self):
        dialog = self.dialog()
        dialog.remote = FakeSocket([
            {'type': 'conversation_initiation_metadata', 'conversation_initiation_metadata_event': {
                'conversation_id': 'conv_test', 'user_input_audio_format': 'pcm_16000',
                'agent_output_audio_format': 'pcm_16000'}},
            {'type': 'user_transcript', 'user_transcription_event': {'user_transcript': '  Меня зовут Иван. '}},
            {'type': 'agent_response', 'agent_response_event': {'agent_response': 'Приятно познакомиться. Что нужно?'}},
            {'type': 'agent_response_correction', 'agent_response_correction_event': {
                'original_agent_response': 'Приятно познакомиться. Что нужно?',
                'corrected_agent_response': 'Приятно познакомиться.'}},
        ])
        try:
            await dialog.receive()
            self.assertEqual([event['type'] for event in self.events],
                             ['conversation_id', 'transcript', 'transcript', 'transcript_correction'])
            self.assertEqual(self.events[0]['conversation_id'], 'conv_test')
            self.assertEqual(self.events[1]['text'], 'Меня зовут Иван.')
            self.assertEqual(self.events[2]['source'], 'generated')
            self.assertEqual(self.events[3]['text'], 'Приятно познакомиться.')
            self.assertNotIn('Private instruction', json.dumps(self.events))
            self.assertNotIn('test-secret', json.dumps(self.events))
            self.assertFalse(any('text' in event for event in self.activity), 'UI activity logs stay free of transcripts')
        finally: await dialog.close()

    async def test_custom_commits_recorded_once_and_repeated_real_turns_preserved(self):
        dialog = self.dialog('custom')
        dialog.start_answer = Mock()
        dialog.remote = FakeSocket([
            {'message_type': 'partial_transcript', 'text': 'Да'},
            {'message_type': 'committed_transcript', 'text': ' Да '},
            {'message_type': 'committed_transcript_with_timestamps', 'text': 'Да', 'words': []},
            {'message_type': 'committed_transcript', 'text': 'Да'},
            {'message_type': 'committed_transcript', 'text': '   '},
        ])
        try:
            await dialog.receive()
            self.assertEqual([event['text'] for event in self.events], ['Да', 'Да'])
            self.assertTrue(all(event['role'] == 'user' for event in self.events))
            self.assertEqual(dialog.start_answer.call_count, 2)
        finally: await dialog.close()

    async def test_capture_failure_does_not_stop_audio_path(self):
        def full_queue(_): raise asyncio.QueueFull()
        dialog = self.dialog(observer=full_queue)
        dialog.remote = FakeSocket([
            {'type': 'user_transcript', 'user_transcription_event': {'user_transcript': 'Здравствуйте.'}},
            {'type': 'ping', 'ping_event': {'event_id': 12}},
        ])
        try:
            await dialog.receive()
            self.assertEqual(dialog.observer_errors, 1)
            self.assertEqual(self.activity, [{'type': 'activity', 'speaker': 'user'}])
            dialog.remote.send_json.assert_awaited_once_with({'type': 'pong', 'event_id': 12})
        finally: await dialog.close()

    async def test_greeting_recorded_only_once_when_audio_starts(self):
        response = FakeResponse([b'\0' * 1920, b'\0' * 1920])
        dialog = self.dialog('custom', response=response)
        try:
            await dialog.respond(dialog.config['greeting'], True, dialog.player.generation)
            self.assertEqual(self.events, [{'type': 'transcript', 'role': 'agent',
                                           'text': 'Здравствуйте!', 'source': 'generated'}])
        finally: await dialog.close()

    async def test_tts_error_or_empty_audio_does_not_claim_agent_spoke(self):
        for response in (FakeResponse(status=402), FakeResponse()):
            dialog = self.dialog('custom', response=response)
            try:
                with suppress(DialogError): await dialog.tts('Недоставленная фраза.', dialog.player.generation)
                self.assertEqual(self.events, [])
            finally: await dialog.close()

    async def test_interrupt_does_not_capture_phrases_waiting_for_synthesis(self):
        audio_started = asyncio.Event()
        dialog = self.dialog('custom', response=FakeResponse([b'\0' * 1920], after_audio=audio_started))
        async def sentences(_):
            yield 'Первая фраза.'
            yield 'Вторая, ещё не озвученная фраза.'
        dialog.llm_sentences = sentences
        dialog.answer = asyncio.create_task(dialog.respond('Вопрос?', False, dialog.player.generation))
        try:
            await asyncio.wait_for(audio_started.wait(), 1)
            dialog.cancel_answer()
            with suppress(asyncio.CancelledError): await dialog.answer
            self.assertEqual([event['text'] for event in self.events], ['Первая фраза.'])
            self.assertTrue(dialog.answer.cancelled())
        finally: await dialog.close()

    async def test_empty_correction_is_retained_to_remove_unheard_response(self):
        dialog = self.dialog()
        dialog.remote = FakeSocket([{'type': 'agent_response_correction', 'agent_response_correction_event': {
            'original_agent_response': 'Здравствуйте.', 'corrected_agent_response': ''}}])
        try:
            await dialog.receive()
            self.assertEqual(self.events[0]['text'], '')
            self.assertEqual(self.events[0]['type'], 'transcript_correction')
        finally: await dialog.close()


if __name__ == '__main__': unittest.main(verbosity=2)
