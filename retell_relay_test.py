"""Offline relay scope, proxy encryption and no-call-before-answer checks."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from aiohttp.test_utils import TestClient, TestServer
from native_server import create_app
from retell_relay import RetellRelay, allowed_request, save_proxy, load_proxy


class ProxyTests(unittest.TestCase):
    def test_scope_and_encryption(self):
        self.assertTrue(allowed_request('POST', '/v3/create-web-call'))
        self.assertTrue(allowed_request('POST', '/webrtc-proxy/call_fake/v1/webrtc/sessions'))
        self.assertTrue(allowed_request('PATCH', '/webrtc-proxy/call_fake/v1/webrtc/sessions/uuid-fake'))
        for path in ['/v2/list-agents', '//unrelated.example', '/webrtc-proxy/../anything', '/v3/create-web-call/other']:
            self.assertFalse(allowed_request('POST', path))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'network.dpapi'
            value = {'address': 'http://proxy.example:8080', 'username': 'offline_user', 'password': 'offline_password', 'country': 'US'}
            save_proxy(value, path)
            self.assertNotIn(value['password'].encode(), path.read_bytes())
            self.assertEqual(load_proxy(path), value)


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_preserves_allowed_domain_and_fixed_destination(self):
        observed = {}
        class Content:
            async def iter_chunked(self, size):
                yield b'{"error":'
                yield b'"offline"}'
        class Response:
            status = 401
            headers = {'Content-Type': 'application/json'}
            content = Content()
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
        class Session:
            def request(self, method, url, **kwargs):
                observed.update(method=method, url=url, **kwargs)
                return Response()
        relay = RetellRelay()
        relay.proxy = {'address': 'http://proxy.example:8080', 'username': 'offline', 'password': 'fake'}
        relay.session = Session()
        async def read(): return b'{}'
        request = SimpleNamespace(method='POST', match_info={'tail': 'v3/create-web-call'}, headers={'Origin': 'http://localhost:8766', 'Authorization': 'Bearer public_key_offline'}, read=read)
        response = await relay.handle(request)
        self.assertEqual(response.status, 401)
        self.assertEqual(json.loads(response.body), {'error': 'offline'}, 'all upstream chunks must be forwarded')
        self.assertEqual(observed['url'], 'https://api.retellai.com/v3/create-web-call')
        self.assertEqual(observed['headers']['Origin'], 'http://localhost:8766')
        self.assertNotIn('Proxy-Authorization', observed['headers'])
        self.assertFalse(observed['allow_redirects'])

    async def test_http_origin_and_answer_guard(self):
        class Monitor:
            def start(self): pass
            def stop(self): pass
            def snapshot(self): return {'available': True, 'active': False, 'callPhase': 'dialing', 'ageMs': 10, 'audioOnComputer': True}
        class Lease:
            def restore(self): pass
            def expired(self): return False
        with patch('retell_relay.load_proxy', return_value=None), patch('native_server.MicrophoneLease', Lease):
            client = TestClient(TestServer(create_app(phone_monitor=Monitor())))
            await client.start_server()
            try:
                local = {'Host': 'localhost:8766', 'Origin': 'http://localhost:8766'}
                response = await client.post('/retell/v3/create-web-call', headers=local, json={})
                self.assertEqual(response.status, 409, 'no paid call while dialing')
                response = await client.post('/retell/v2/list-agents', headers=local, json={})
                self.assertEqual(response.status, 404)
                response = await client.post('/retell/v3/create-web-call', headers={'Host': 'localhost:8766', 'Origin': 'https://unrelated.example'}, json={})
                self.assertEqual(response.status, 403)
            finally:
                await client.close()


if __name__ == '__main__': unittest.main(verbosity=2)
