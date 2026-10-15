"""Offline credential, interruption, audio pacing and no-idle-call checks."""
import asyncio
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import numpy as np
from aiohttp.test_utils import TestClient, TestServer
from eleven_store import ElevenStore, public_config
from eleven_dialog import PcmPlayer, ElevenDialog
from native_server import create_app


class StoreTests(unittest.TestCase):
    def test_secrets_encrypted_and_never_returned(self):
        with tempfile.TemporaryDirectory() as folder:
            store = ElevenStore(Path(folder)/'config.dpapi')
            result = store.save({'elevenKey':'fake-eleven-secret','llmKey':'fake-model-secret',
                                 'llmUrl':'https://api.deepseek.com/chat/completions','llmModel':'test','voiceId':'test'})
            self.assertTrue(result['customReady'])
            self.assertNotIn('elevenKey', result)
            self.assertNotIn('llmKey', result)
            self.assertNotIn(b'fake-eleven-secret', store.path.read_bytes())
            self.assertNotIn(b'fake-model-secret', store.path.read_bytes())
            store.save({'elevenKey':'','llmKey':'','agentId':'agent_test'})
            self.assertEqual(store.read()['elevenKey'],'fake-eleven-secret')
            with self.assertRaises(ValueError): store.save({'llmUrl':'https://stolen:key@example.org/chat/completions'})
            self.assertTrue(public_config(store.read())['agentsReady'])


class AudioTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_time_pacing_and_interruption_invalidates_old_audio(self):
        packets=[]
        bridge=SimpleNamespace(alive=True,output_queue=SimpleNamespace(clear=lambda:None),
                               push_audio=lambda p:packets.append((time.monotonic(),np.frombuffer(p,dtype='<f4').copy())))
        player=PcmPlayer(bridge)
        try:
            await player.push(np.full(1600,10000,dtype='<i2').tobytes(),16000,0)
            await asyncio.sleep(.15)
            self.assertEqual(len(packets),5)
            self.assertAlmostEqual(packets[-1][0]-packets[0][0],.08,delta=.04)
            generation=player.interrupt()
            previous=len(packets)
            await player.push(np.full(320,20000,dtype='<i2').tobytes(),16000,0)
            await player.push(np.full(320,-10000,dtype='<i2').tobytes(),16000,generation)
            await asyncio.sleep(.06)
            self.assertEqual(len(packets),previous+1)
            self.assertTrue(np.all(packets[-1][1]<0),'only the new generation may play after an interruption')
        finally:await player.close()

    async def test_startup_keeps_recent_audio_instead_of_failing_while_connecting(self):
        bridge=SimpleNamespace(alive=True,output_queue=SimpleNamespace(clear=lambda:None))
        async def emit(_):pass
        dialog=ElevenDialog('custom',{'prompt':'test'},bridge,None,None,emit)
        try:
            for _ in range(100):dialog.feed(bytes(3840))
            self.assertEqual(dialog.input.qsize(),15)
            self.assertFalse(dialog.done.done())
        finally:await dialog.close()

    async def test_caller_interrupts_immediately_and_short_pause_does_not_commit(self):
        events=[]
        sent=[]
        bridge=SimpleNamespace(alive=True,output_queue=SimpleNamespace(clear=lambda:None))
        async def emit(event):events.append(event)
        async def send_json(message):sent.append(message)
        dialog=ElevenDialog('custom',{'prompt':'test','replyPauseMs':350},bridge,None,None,emit)
        dialog.remote=SimpleNamespace(send_json=send_json)
        dialog.sender=asyncio.create_task(dialog.send_audio())
        old_answer=asyncio.create_task(asyncio.sleep(60))
        dialog.answer=old_answer
        async def feed(count,level):
            for _ in range(count):
                dialog.feed(np.full(960,level,dtype='<f4').tobytes())
                await asyncio.sleep(0)
        try:
            await feed(7,.1)
            await asyncio.sleep(0)
            self.assertTrue(old_answer.cancelled(),'100 ms of caller speech cancels generation')
            self.assertTrue(any(event['type']=='interrupted' for event in events))
            await feed(10,0)
            self.assertFalse(any(message.get('commit') for message in sent),'200 ms pause must not end a turn')
            await feed(5,.1)
            await feed(22,0)
            self.assertEqual(sum(bool(message.get('commit')) for message in sent),1,'only a complete pause ends the caller turn')
        finally:
            dialog.remote=None
            await dialog.close()


class GuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_call_and_cross_origin_never_start_cloud_audio(self):
        class Monitor:
            def start(self):pass
            def stop(self):pass
            def snapshot(self):return {'available':True,'active':False,'callPhase':'idle','ageMs':0}
        class Lease:
            def restore(self):pass
            def expired(self):return False
        with tempfile.TemporaryDirectory() as folder:
            store=ElevenStore(Path(folder)/'config.dpapi')
            with patch('eleven_routes.ElevenStore',return_value=store),patch('native_server.MicrophoneLease',Lease):
                client=TestClient(TestServer(create_app(phone_monitor=Monitor())))
                await client.start_server()
                try:
                    local={'Host':'localhost:8766','Origin':'http://localhost:8766'}
                    bad={'Host':'localhost:8766','Origin':'https://unrelated.example'}
                    result=await client.post('/eleven/settings',headers=bad,json={'elevenKey':'fake'})
                    self.assertEqual(result.status,403)
                    result=await client.post('/eleven/settings',headers=local,json={'elevenKey':'fake-private'})
                    self.assertEqual(result.status,200)
                    result=await client.get('/eleven/settings',headers=local)
                    self.assertNotIn('fake-private',await result.text())
                    result=await client.get('/eleven/conversation?mode=custom',headers=local)
                    self.assertEqual(result.status,409)
                    result=await client.get('/eleven/conversation?mode=agents',headers=bad)
                    self.assertEqual(result.status,403)
                finally:await client.close()


if __name__=='__main__':unittest.main(verbosity=2)
