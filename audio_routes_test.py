"""Offline checks: audio conversion, microphone rollback and route HTTP guards."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import numpy as np
from aiohttp.test_utils import TestClient, TestServer
import audio_routes as routes
from native_audio import NativeAudio, SampleQueue
from native_server import create_app
from crm_store import CrmStore


class RouteTests(unittest.TestCase):
    def test_virtual_pcm_is_not_decimated_and_channels_match(self):
        bridge = NativeAudio(output_mode="virtual-mic")
        samples = np.linspace(-0.5, 0.5, 960, dtype=np.float32)
        bridge.push_audio(samples.tobytes())
        output = bytearray(960 * 2 * 4)
        bridge._output_callback(output, 960, None, False)
        stereo = np.frombuffer(output, dtype=np.float32).reshape(-1, 2)
        np.testing.assert_array_equal(stereo[:, 0], samples)
        np.testing.assert_array_equal(stereo[:, 1], samples)
        self.assertEqual(bridge.stats()["outputSampleRate"], 48000)
        self.assertEqual(bridge.stats()["droppedOutputSamples"], 0)

    def test_existing_phone_pcm_and_bounded_queue(self):
        bridge = NativeAudio()
        bridge.push_audio(np.zeros(960, dtype=np.float32).tobytes())
        self.assertEqual(bridge.output_queue.snapshot()[0], 320)
        output = bytearray(640)
        bridge._output_callback(output, 320, None, False)
        self.assertEqual(len(output), 640)
        queue = SampleQueue(4, np.float32)
        queue.push(np.arange(8, dtype=np.float32))
        np.testing.assert_array_equal(queue.take(4)[0], [4, 5, 6, 7])
        self.assertEqual(queue.dropped, 4)

    def test_known_devices_only(self):
        sd = SimpleNamespace(query_hostapis=lambda: [{"name": "Windows WASAPI"}],
                             query_devices=lambda: [{"name": "Speakers", "hostapi": 0, "max_output_channels": 2}])
        with self.assertRaises(RuntimeError): routes.output_device("virtual-mic", sd)
        with self.assertRaises(ValueError): routes.output_device("arbitrary-id", sd)

    def test_restore_preserves_user_changes_and_recovers_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            backup = Path(directory) / "restore.json"
            before = {r: "{0.0.1.00000000}.{old-" + r.name + "}" for r in routes.ROLES}
            current = dict(before)
            target = "{0.0.1.00000000}.{vb-cable}"
            enum = SimpleNamespace(GetDefaultAudioEndpoint=lambda _, role: SimpleNamespace(GetId=lambda: current[routes.ERole(role)]))
            def set_default(dev_id, roles):
                for role in roles: current[role] = dev_id
            with patch.object(routes, "RESTORE_FILE", backup), \
                 patch.object(routes.comtypes, "CoInitialize"), patch.object(routes.comtypes, "CoUninitialize"), \
                 patch.object(routes.AudioUtilities, "GetDeviceEnumerator", return_value=enum), \
                 patch.object(routes.AudioUtilities, "GetSpeakers", return_value=SimpleNamespace(FriendlyName="Speakers")), \
                 patch.object(routes.AudioUtilities, "SetDefaultDevice", side_effect=set_default), \
                 patch.object(routes, "cable_output_device", return_value=SimpleNamespace(id=target)):
                lease = routes.MicrophoneLease()
                lease.prepare("virtual-mic")
                self.assertTrue(lease.ready())
                self.assertTrue(backup.is_file())
                # Simulate server termination; the new instance recovers its backup.
                resumed = routes.MicrophoneLease()
                current[routes.ROLES[0]] = "{0.0.1.00000000}.{user-selected}"
                resumed.restore()
                self.assertEqual(current[routes.ROLES[0]], "{0.0.1.00000000}.{user-selected}")
                for role in routes.ROLES[1:]: self.assertEqual(current[role], before[role])
                self.assertFalse(backup.exists())
                self.assertFalse(resumed.ready())


class HTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_origin_and_unprepared_virtual_audio_guards(self):
        class Monitor:
            def start(self): pass
            def stop(self): pass
            def snapshot(self): return {"callPhase": "idle"}
        class Lease:
            def restore(self): pass
            def expired(self): return False
            def heartbeat(self): pass
            def ready(self): return False
            def prepare(self, mode): raise RuntimeError("VB-CABLE not installed")
            def status(self): return {"virtualMicInstalled": False, "virtualMicPrepared": False}
        def forbidden_audio_factory(**kwargs):
            raise AssertionError("Must not open audio on an unprepared route")
        with tempfile.TemporaryDirectory() as directory, \
             patch("native_server.MicrophoneLease", Lease), \
             patch("crm_service.CrmStore", return_value=CrmStore(Path(directory) / "crm.sqlite3")):
            client = TestClient(TestServer(create_app(audio_factory=forbidden_audio_factory, phone_monitor=Monitor())))
            await client.start_server()
            try:
                headers = {"Host": "localhost:8766", "Origin": "http://localhost:8766"}
                response = await client.get('/audio-routes', headers=headers)
                self.assertFalse((await response.json())["virtualMicInstalled"])
                response = await client.post('/audio-route', headers={"Host": "localhost:8766", "Origin": "https://unrelated.example"}, json={"action": "prepare", "mode": "virtual-mic"})
                self.assertEqual(response.status, 403)
                response = await client.post('/audio-route', headers=headers, json={"action": "prepare", "mode": "arbitrary-id"})
                self.assertEqual(response.status, 400)
                response = await client.get('/audio?mode=virtual-mic', headers=headers)
                self.assertEqual(response.status, 409)
                response = await client.get('/audio?mode=arbitrary-id', headers=headers)
                self.assertEqual(response.status, 400)
            finally:
                await client.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
