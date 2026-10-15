"""No network: explicit agent end-call intent, final audio drain and barge-in."""
import asyncio
import base64
import json
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import aiohttp
import numpy as np

from eleven_dialog import ElevenDialog, PcmPlayer


class OutputQueue:
    def __init__(self):
        self.queued = 0

    def clear(self):
        self.queued = 0

    def snapshot(self):
        return self.queued, 0


def make_bridge():
    packets = []
    queue = OutputQueue()
    return SimpleNamespace(alive=True, output_queue=queue, packets=packets,
                           push_audio=lambda packet: packets.append((time.monotonic(), packet)))


END_EVENT = {'type': 'agent_tool_response', 'agent_tool_response': {
    'tool_name': 'end_call', 'tool_type': 'system', 'tool_call_id': 'end_call_test', 'is_error': False}}


def audio_event(value=1000, samples=1600, event_id=1):
    return {'type': 'audio', 'audio_event': {'event_id': event_id,
            'audio_base_64': base64.b64encode(np.full(samples, value, dtype='<i2').tobytes()).decode('ascii')}}


class FakeRemote:
    def __init__(self, events, close_code=1000, stay_open=False):
        self.events = events
        self.close_code = None
        self.final_code = close_code
        self.stay_open = stay_open
        self.closed = False

    async def messages(self):
        for delay, event in self.events:
            await asyncio.sleep(delay)
            yield SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(event))
        if self.stay_open:
            await asyncio.Future()
        self.close_code = self.final_code

    def __aiter__(self):
        return self.messages()

    async def close(self):
        self.closed = True

    async def send_json(self, message):
        pass


class HangupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.patchers = [patch('eleven_dialog.END_CALL_QUIET_SECONDS', .06),
                         patch('eleven_dialog.END_CALL_DRAIN_SECONDS', 1),
                         patch('eleven_dialog.PHONE_OUTPUT_TAIL_SECONDS', .04)]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    async def make_dialog(self, remote=None):
        async def emit(message):
            pass
        dialog = ElevenDialog('agents', {'prompt': 'test', 'greeting': 'До свидания.',
                                        'voiceId': '', 'agentId': 'fake'}, make_bridge(), None, None, emit)
        dialog.remote = remote
        return dialog

    async def test_only_successful_system_end_call_is_accepted(self):
        dialog = await self.make_dialog()
        try:
            valid = END_EVENT['agent_tool_response']
            invalid = [dict(valid, is_error=True), dict(valid, is_error=0), dict(valid, tool_type='client'),
                       dict(valid, tool_name='skip_turn'), dict(valid, status='error'),
                       dict(valid, status='blocked'), dict(valid, status='skipped'),
                       dict(valid, is_blocked=True), dict(valid, is_called=False),
                       dict(valid, is_called=0), {'tool_name': 'end_call'}]
            for event in invalid:
                self.assertFalse(dialog.request_end_call(event), event)
                self.assertFalse(dialog.end_call_requested)
            self.assertTrue(dialog.request_end_call(dict(valid, status='success', is_called=True, is_blocked=False)))
            task = dialog._end_call_task
            self.assertTrue(dialog.request_end_call(valid))
            self.assertIs(dialog._end_call_task, task, 'duplicate success cannot schedule a second end')
        finally:
            await dialog.close()

    async def test_end_event_then_late_audio_is_fully_played_before_completion(self):
        remote = FakeRemote([(0, audio_event(samples=1600)), (0, END_EVENT),
                             (.04, audio_event(value=2000, samples=1600, event_id=2))])
        dialog = await self.make_dialog(remote)
        try:
            await dialog.receive()
            self.assertTrue(dialog.end_call_requested)
            self.assertFalse(dialog.end_call_audio_drained)
            await dialog._end_call_task
            self.assertTrue(dialog.end_call_audio_drained)
            self.assertEqual(len(dialog.bridge.packets), 10)
            self.assertGreaterEqual(time.monotonic() - dialog.bridge.packets[-1][0], .04)
        finally:
            await dialog.close()

    async def test_normal_close_without_end_event_and_error_close_never_request_hangup(self):
        plain = await self.make_dialog(FakeRemote([(0, audio_event())]))
        failed = await self.make_dialog(FakeRemote([(0, END_EVENT)], close_code=1006))
        try:
            await plain.receive()
            self.assertFalse(plain.end_call_requested)
            self.assertFalse(plain.end_call_audio_drained)
            await failed.receive()
            await failed._end_call_task
            self.assertTrue(failed.end_call_requested)
            self.assertFalse(failed.end_call_audio_drained)
        finally:
            await plain.close()
            await failed.close()

    async def test_interruption_cancels_pending_hangup_and_old_audio(self):
        remote = FakeRemote([(0, audio_event(samples=16000)), (0, END_EVENT),
                             (.02, {'type': 'interruption', 'interruption_event': {'event_id': 1}}),
                             (0, audio_event(value=3000, samples=320, event_id=2))])
        dialog = await self.make_dialog(remote)
        try:
            await dialog.receive()
            await asyncio.sleep(.12)
            self.assertFalse(dialog.end_call_requested)
            self.assertFalse(dialog.end_call_audio_drained)
            self.assertLess(len(dialog.bridge.packets), 10, 'queued old farewell must be discarded')
            self.assertGreater(np.frombuffer(dialog.bridge.packets[-1][1], dtype='<f4').max(), .08)
        finally:
            await dialog.close()

    async def test_drain_waits_for_native_queue_and_has_a_deadline(self):
        bridge = make_bridge()
        player = PcmPlayer(bridge)
        try:
            await player.push(np.full(1600, 1000, dtype='<i2').tobytes(), 16000, 0)
            bridge.output_queue.queued = 960
            self.assertFalse(await player.drain(.15), 'stuck native queue must time out')
            bridge.output_queue.queued = 0
            self.assertTrue(await player.drain(.3))
            await player.push(np.full(16000, 1000, dtype='<i2').tobytes(), 16000, 0)
            draining = asyncio.create_task(player.drain(.5))
            await asyncio.sleep(.02)
            player.interrupt()
            self.assertFalse(await draining, 'an interrupted generation must not confirm farewell playback')
        finally:
            await player.close()

    async def test_end_event_can_finish_without_waiting_forever_for_server_close(self):
        remote = FakeRemote([(0, END_EVENT)], stay_open=True)
        dialog = await self.make_dialog(remote)
        receiver = asyncio.create_task(dialog.receive())
        try:
            await asyncio.wait_for(asyncio.shield(dialog.done), .7)
            self.assertTrue(dialog.end_call_audio_drained)
            self.assertFalse(receiver.done())
        finally:
            receiver.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await receiver
            await dialog.close()

    async def test_run_keeps_native_audio_alive_until_normal_end_is_drained(self):
        remote = FakeRemote([(0, audio_event(samples=3200)), (0, END_EVENT)])
        dialog = await self.make_dialog()
        async def signed_url(*args, **kwargs):
            return {'signed_url': 'wss://api.elevenlabs.io/v1/convai/conversation?test=1'}
        async def connect(*args, **kwargs):
            return remote
        dialog.json_get = signed_url
        dialog.route = lambda: {}
        dialog.http = SimpleNamespace(ws_connect=connect)
        await asyncio.wait_for(dialog.run(), 1)
        self.assertTrue(dialog.end_call_requested)
        self.assertTrue(dialog.end_call_audio_drained)
        self.assertEqual(len(dialog.bridge.packets), 10)
        self.assertTrue(remote.closed)

    async def test_end_drain_keeps_caller_input_for_interruption_detection(self):
        dialog = await self.make_dialog()
        dialog.sender = asyncio.create_task(asyncio.sleep(60))
        try:
            dialog.request_end_call(END_EVENT['agent_tool_response'])
            dialog.feed(bytes(3840))
            self.assertEqual(dialog.input.qsize(), 1)
        finally:
            await dialog.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
