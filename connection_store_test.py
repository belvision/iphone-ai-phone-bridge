"""Credential persistence and local-only HTTP tests, using fake public keys."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from aiohttp.test_utils import TestClient, TestServer
from connection_store import ConnectionStore
from native_server import create_app

FAKE = {"key": "public_key_offline_test_only", "agentId": "agent_offline", "audioMode": "virtual-mic"}


class StoreTests(unittest.TestCase):
    def test_encrypted_reload_and_reject_private_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "connection.dpapi"
            store = ConnectionStore(path)
            self.assertIsNone(store.read())
            store.save(FAKE)
            self.assertNotIn(FAKE["key"].encode(), path.read_bytes())
            self.assertEqual(ConnectionStore(path).read(), FAKE)
            with self.assertRaises(ValueError): store.save({**FAKE, "key": "key_private_must_not_be_saved"})
            self.assertEqual(store.read(), FAKE, "invalid input must not replace saved connection")
            path.write_bytes(b"corrupted ciphertext")
            with self.assertRaises(OSError): store.read()


class HTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_connection_and_origin_guards(self):
        class Monitor:
            def start(self): pass
            def stop(self): pass
            def snapshot(self): return {"callPhase": "idle"}
        class Lease:
            def restore(self): pass
            def expired(self): return False
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "connection.dpapi"
            with patch("native_server.ConnectionStore", lambda: ConnectionStore(path)), patch("native_server.MicrophoneLease", Lease):
                client = TestClient(TestServer(create_app(phone_monitor=Monitor())))
                await client.start_server()
                try:
                    local = {"Host": "localhost:8766", "Origin": "http://localhost:8766"}
                    evil = {"Host": "localhost:8766", "Origin": "https://unrelated.example"}
                    for verb in (client.get, client.post):
                        result = await verb('/connection', headers=evil)
                        self.assertEqual(result.status, 403)
                    result = await client.post('/connection', headers={"Host": "localhost:8766"}, json=FAKE)
                    self.assertEqual(result.status, 403)
                    result = await client.post('/connection', headers=local, json=FAKE)
                    self.assertEqual(await result.json(), {"saved": True})
                    result = await client.get('/connection', headers={"Host": "localhost:8766"})
                    self.assertEqual((await result.json())["key"], FAKE["key"])
                finally:
                    await client.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
