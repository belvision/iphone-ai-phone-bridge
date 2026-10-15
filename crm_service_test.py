"""Offline CRM service/HTTP integration with temporary SQLite and fake providers."""
import asyncio
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
from aiohttp.test_utils import TestClient, TestServer

from crm_service import CrmService, analyze_call
from crm_store import CrmStore
from eleven_store import DEFAULTS, public_config
from native_server import create_app


RESULT = {'summary': 'Клиент хочет автоматизировать учёт.', 'interest': 'interested',
          'name': 'Иван', 'phone': '', 'telegram': '', 'next_step': 'Сергей уточнит задачу.'}
CONFIG = dict(DEFAULTS, elevenKey='fake-eleven-secret', llmKey='fake-llm-secret',
              llmUrl='https://api.deepseek.com/chat/completions', llmModel='test',
              agentId='agent_test', voiceId='test')
LOCAL = {'Host': 'localhost:8766', 'Origin': 'http://localhost:8766'}
VCARD = ('BEGIN:VCARD\r\nVERSION:3.0\r\nUID:offline-contact-1\r\n'
         'FN:Контакт из телефона\r\nTEL;TYPE=CELL:+375 29 000-00-00\r\nEND:VCARD\r\n')


async def settle_jobs(service):
    while service.jobs:
        await asyncio.gather(*list(service.jobs.values()))
        await asyncio.sleep(0)


async def wait_until(predicate, timeout=2):
    async def wait():
        while not predicate(): await asyncio.sleep(.01)
    await asyncio.wait_for(wait(), timeout)


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.store = CrmStore(Path(self.folder.name) / 'crm.sqlite3')
        eleven = SimpleNamespace(store=SimpleNamespace(read=lambda: dict(CONFIG)), http=None,
                                 server=SimpleNamespace(retell_relay=SimpleNamespace(proxy=None)))
        self.service = CrmService(eleven, self.store)
        await self.service.start()

    async def asyncTearDown(self):
        await self.service.close()
        self.folder.cleanup()

    async def test_capture_correct_finish_and_analysis_are_one_record(self):
        analyze = AsyncMock(return_value=dict(RESULT))
        with patch('crm_service.analyze_call', analyze):
            capture = await self.service.begin('agents')
            capture.observe({'type': 'conversation_id', 'conversation_id': 'conv_offline'})
            capture.observe({'type': 'transcript', 'role': 'user', 'text': 'Я Иван, хочу автоматизировать учёт.'})
            capture.observe({'type': 'transcript', 'role': 'agent', 'text': 'Сергей уточнит задачу. Ещё вопрос.'})
            capture.observe({'type': 'transcript_correction', 'role': 'agent',
                             'original_text': 'Сергей уточнит задачу. Ещё вопрос.', 'text': 'Сергей уточнит задачу.'})
            await capture.finish('ended')
            await capture.finish('ended')
            capture.observe({'type': 'transcript', 'role': 'user', 'text': 'Позднее событие не из звонка.'})
            await settle_jobs(self.service)
        self.assertEqual(self.store.list_calls()['total'], 1)
        record = self.store.get_call(capture.call_id)
        self.assertEqual(record['state'], 'finished')
        self.assertEqual(record['analysis_status'], 'ready')
        self.assertEqual(record['conversation_id'], 'conv_offline')
        self.assertEqual(record['summary'], RESULT['summary'])
        self.assertEqual([turn['text'] for turn in record['transcript']],
                         ['Я Иван, хочу автоматизировать учёт.', 'Сергей уточнит задачу.'])
        analyze.assert_awaited_once()
        self.assertEqual(analyze.await_args.args[-1]['state'], 'finished', 'analysis runs after finalization')

    async def test_network_error_preserves_transcript_and_redacts_transport_details(self):
        failure = aiohttp.ClientConnectionError('https://fake-user:fake-secret@example.test failed')
        with patch('crm_service.analyze_call', AsyncMock(side_effect=failure)):
            capture = await self.service.begin('custom')
            capture.observe({'type': 'transcript', 'role': 'user', 'text': 'Позвоните завтра.'})
            await capture.finish('ended')
            await settle_jobs(self.service)
        record = self.store.get_call(capture.call_id)
        self.assertEqual(record['analysis_status'], 'error')
        self.assertEqual(record['transcript'], [{'role': 'user', 'text': 'Позвоните завтра.'}])
        self.assertNotIn('fake-secret', json.dumps(record))
        self.assertNotIn('example.test', record['analysis_error'])

    async def test_empty_call_and_failed_observer_do_not_start_analysis(self):
        analyze = AsyncMock(return_value=dict(RESULT))
        with patch('crm_service.analyze_call', analyze):
            empty = await self.service.begin('agents')
            await empty.finish('phone_ended_before_start')
            incomplete = await self.service.begin('custom')
            incomplete.observe({'type': 'transcript', 'role': 'user', 'text': 'Обрыв записи.'})
            await incomplete.finish('ended', observer_errors=1)
            await settle_jobs(self.service)
        self.assertEqual(self.store.get_call(empty.call_id)['analysis_status'], 'empty')
        self.assertEqual(self.store.get_call(incomplete.call_id)['analysis_status'], 'error')
        self.assertIs(self.store.get_call(incomplete.call_id)['transcript_incomplete'], True)
        analyze.assert_not_awaited()

    async def test_phone_identity_matches_existing_contact_without_overwriting_analysis(self):
        self.service.contacts.import_contacts([{'name': 'Имя в адресной книге', 'phones': ['+375290000000']}], 'google')
        contact = self.service.contacts.search()['items'][0]
        analyze = AsyncMock(return_value=dict(RESULT))
        with patch('crm_service.analyze_call', analyze):
            capture = await self.service.begin('agents', {'phoneNumber': '80 (29) 000-00-00', 'displayName': 'Имя iPhone'})
            capture.observe({'type': 'transcript', 'role': 'user', 'text': 'Меня зовут Иван.'})
            await capture.finish('ended')
            await settle_jobs(self.service)
        record = self.service.contacts.enrich(self.store.get_call(capture.call_id))
        self.assertEqual(record['name'], 'Иван', 'confirmed transcript name stays a separate field')
        self.assertEqual(record['contact_id'], contact['id'])
        self.assertEqual(record['contact_name'], 'Имя в адресной книге')
        self.assertEqual(record['call_number'], '+375290000000')
        self.assertEqual(record['call_display_name'], 'Имя iPhone')
        self.service.contacts.match_call(capture.call_id, '+375299999999', 'Поздние данные другого звонка')
        unchanged = self.service.contacts.enrich(self.store.get_call(capture.call_id))
        self.assertEqual(unchanged['call_number'], '+375290000000')
        self.assertEqual(unchanged['call_display_name'], 'Имя iPhone')
        self.assertEqual(unchanged['contact_id'], contact['id'])


class FakeSettings:
    def __init__(self): self.config = dict(CONFIG)
    def read(self): return dict(self.config)
    def save(self, changes):
        self.config.update(changes)
        return public_config(self.config)


class FakeMonitor:
    def start(self): pass
    def stop(self): pass
    def snapshot(self, include_contact=False, include_control=False):
        state = {'available': True, 'active': True, 'callPhase': 'connected', 'callCount': 1,
                 'audioOnComputer': True, 'ageMs': 0, 'callSequence': 1}
        if include_contact:
            state.update(phoneNumber='+375290000000', displayName='Имя текущего абонента')
        return state


class FakeLease:
    def restore(self): pass
    def expired(self): return False


class AnalyzerTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_json_is_read_across_network_chunks(self):
        body = json.dumps({'choices': [{'finish_reason': 'stop', 'message': {
            'content': json.dumps(RESULT, ensure_ascii=False)}}]}, ensure_ascii=False).encode('utf-8')
        class Response:
            status = 200
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass
            @property
            def content(self): return self
            async def iter_chunked(self, _):
                # Deliberately split both JSON tokens and multi-byte Russian letters.
                for offset in range(0, len(body), 3):
                    await asyncio.sleep(0)
                    yield body[offset:offset + 3]
        http = SimpleNamespace(post=Mock(return_value=Response()))
        record = {'state': 'finished', 'transcript_incomplete': True,
                  'caller_phone': '+375290000000', 'caller_name': 'CONTACT_METADATA_MUST_STAY_LOCAL',
                  'call_number': '+375290000000', 'call_display_name': 'CONTACT_METADATA_MUST_STAY_LOCAL',
                  'contact_id': 'local-only-contact', 'contact_name': 'ADDRESSBOOK_MUST_STAY_LOCAL',
                  'name': 'SAVED_NAME_MUST_STAY_LOCAL', 'phone': '+375299999999',
                  'transcript': [{'role': 'user', 'text': 'Хочу автоматизировать учёт.'}]}
        self.assertEqual(await analyze_call(http, CONFIG, None, record), RESULT)
        request = http.post.call_args.kwargs
        self.assertFalse(request['allow_redirects'])
        self.assertNotIn('proxy', request, 'DeepSeek stays direct')
        self.assertNotIn('fake-llm-secret', json.dumps(request['json']))
        self.assertNotIn('fake-eleven-secret', json.dumps(request['json']))
        user_payload = json.loads(request['json']['messages'][1]['content'])
        self.assertEqual(set(user_payload), {'incomplete', 'transcript'})
        self.assertIs(user_payload['incomplete'], True)
        for local_value in ('CONTACT_METADATA_MUST_STAY_LOCAL', 'ADDRESSBOOK_MUST_STAY_LOCAL',
                            'SAVED_NAME_MUST_STAY_LOCAL', '+375290000000', '+375299999999'):
            self.assertNotIn(local_value, json.dumps(request['json']))


class HttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.store = CrmStore(Path(self.folder.name) / 'crm.sqlite3')
        self.settings = FakeSettings()
        self.patches = ExitStack()
        self.patches.enter_context(patch('crm_service.CrmStore', return_value=self.store))
        self.patches.enter_context(patch('eleven_routes.ElevenStore', return_value=self.settings))
        self.patches.enter_context(patch('native_server.MicrophoneLease', FakeLease))
        self.patches.enter_context(patch('native_server.RetellRelay.start', AsyncMock()))
        self.patches.enter_context(patch('crm_service.analyze_call', AsyncMock(return_value=dict(RESULT))))
        app = create_app(phone_monitor=FakeMonitor())
        self.server = next(route.handler.__self__ for route in app.router.routes()
                           if route.resource.canonical == '/health')
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.patches.close()
        self.folder.cleanup()

    async def test_crm_origin_guards_edits_and_secret_free_responses(self):
        call_id = self.store.create_call('agents')
        self.store.append_turn(call_id, 'user', 'Нужна автоматизация.')
        self.store.finish_call(call_id, 'ended', 2)
        detail = '/crm/calls/' + call_id
        bad = dict(LOCAL, Origin='https://unrelated.example')
        for method, url, headers in [
                ('GET', '/crm/calls', bad), ('GET', detail, bad),
                ('GET', detail, {'Host': 'localhost:8766', 'Sec-Fetch-Site': 'cross-site'}),
                ('GET', detail, {'Host': 'unrelated.example'}),
                ('POST', detail, {'Host': 'localhost:8766'}), ('POST', detail, bad),
                ('POST', detail + '/analyze', bad)]:
            response = await self.client.request(method, url, headers=headers, json={'notes': 'no'})
            self.assertEqual(response.status, 403, (method, url, headers))
        response = await self.client.post(detail, headers=LOCAL,
                                          json={'name': 'Иван', 'notes': 'Проверено вручную', 'status': 'callback'})
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())['status'], 'callback')
        response = await self.client.post(detail, headers=LOCAL, json={'summary': 'Нельзя подменить итог'})
        self.assertEqual(response.status, 400)
        for url in ('/crm/calls', detail):
            response = await self.client.get(url, headers={'Host': 'localhost:8766'})
            self.assertEqual(response.status, 200)
            data = await response.text()
            self.assertNotIn('fake-eleven-secret', data)
            self.assertNotIn('fake-llm-secret', data)
            self.assertNotIn('elevenKey', data)
            self.assertNotIn('llmKey', data)
        self.assertEqual(self.store.get_call(call_id)['notes'], 'Проверено вручную')

    async def test_contacts_import_search_and_call_binding_are_local_and_guarded(self):
        call_id = self.store.create_call('agents')
        self.store.append_turn(call_id, 'user', 'Нужна автоматизация.')
        self.store.finish_call(call_id, 'ended', 2)
        detail = '/crm/calls/' + call_id
        bad = dict(LOCAL, Origin='https://unrelated.example')
        for method, path, headers in [
                ('GET', '/crm/contacts', bad),
                ('GET', '/crm/contacts', {'Host': 'localhost:8766', 'Sec-Fetch-Site': 'cross-site'}),
                ('POST', '/crm/contacts/import', bad),
                ('POST', '/crm/contacts/import', {'Host': 'localhost:8766'}),
                ('POST', detail + '/contact', bad),
                ('POST', detail + '/contact', {'Host': 'localhost:8766'})]:
            response = await self.client.request(method, path, headers=headers, data=VCARD.encode())
            self.assertEqual(response.status, 403)
        response = await self.client.post('/crm/contacts/import?source=iphone',
            headers=dict(LOCAL, **{'Content-Type': 'text/vcard'}), data=VCARD.encode('utf-8-sig'))
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual((await response.json())['added'], 1)
        response = await self.client.get('/crm/contacts', headers=LOCAL, params={'q': 'Контакт'})
        self.assertEqual(response.status, 200)
        contacts = await response.json()
        self.assertEqual(contacts['total'], 1)
        contact = contacts['items'][0]
        self.assertEqual(contact['name'], 'Контакт из телефона')
        self.assertTrue(contact['phones'])
        before = self.store.get_call(call_id)
        response = await self.client.post(detail + '/contact', headers=LOCAL, json={'contact_id': contact['id']})
        self.assertEqual(response.status, 200, await response.text())
        response = await self.client.get(detail, headers=LOCAL)
        enriched = await response.json()
        self.assertIn(contact['id'], json.dumps(enriched))
        self.assertIn('Контакт из телефона', json.dumps(enriched, ensure_ascii=False))
        after = self.store.get_call(call_id)
        for field in ('name', 'phone', 'transcript'):
            self.assertEqual(after[field], before[field], 'address-book identity must not overwrite transcript/analysis fields')
        for secret in ('fake-eleven-secret', 'fake-llm-secret'):
            self.assertNotIn(secret, json.dumps(enriched))
        response = await self.client.post(detail + '/contact', headers=LOCAL,
                                          json={'contact_id': contact['id'], 'name': 'Unexpected edit'})
        self.assertEqual(response.status, 400)
        response = await self.client.post('/crm/calls/missing/contact', headers=LOCAL, json={'contact_id': contact['id']})
        self.assertEqual(response.status, 404)
        response = await self.client.post(detail + '/contact', headers=LOCAL, json={'contact_id': 'missing-contact'})
        self.assertEqual(response.status, 404)

    async def test_contacts_reject_invalid_import_and_preserve_existing_directory(self):
        response = await self.client.post('/crm/contacts/import?source=google',
            headers=dict(LOCAL, **{'Content-Type': 'text/vcard'}), data=VCARD.encode())
        self.assertEqual(response.status, 200)
        for path, body in [('/crm/contacts/import?source=unsupported', VCARD.encode()),
                           ('/crm/contacts/import', b'\xff\xfe\0broken'),
                           ('/crm/contacts/import', b'not a vCard')]:
            response = await self.client.post(path, headers=LOCAL, data=body)
            self.assertEqual(response.status, 400)
        response = await self.client.get('/crm/contacts?limit=101', headers=LOCAL)
        self.assertEqual(response.status, 400)
        response = await self.client.get('/crm/contacts', headers=LOCAL)
        self.assertEqual((await response.json())['total'], 1)

    async def test_overlapping_session_is_reserved_before_config_read_finishes(self):
        entered, release = threading.Event(), threading.Event()
        def slow_read():
            entered.set()
            if not release.wait(2): raise RuntimeError('test config read was not released')
            return dict(CONFIG)
        self.settings.read = slow_read
        self.server.bridge = SimpleNamespace(alive=True, stop_event=threading.Event(), stop=lambda _: None)
        dialogs = []
        class FakeDialog:
            def __init__(self, mode, config, bridge, http, proxy, emit, observer=None):
                self.bridge, self.emit, self.observer = bridge, emit, observer
                self.observer_errors = 0
                self.closed = asyncio.Event()
                dialogs.append(self)
            async def run(self):
                self.observer({'type': 'transcript', 'role': 'user', 'text': 'Один тестовый звонок.'})
                await self.emit({'type': 'live'})
                await self.closed.wait()
            async def close(self): self.closed.set()
        with patch('eleven_routes.ElevenDialog', FakeDialog):
            first = asyncio.ensure_future(self.client.ws_connect('/eleven/conversation?mode=agents', headers=LOCAL))
            socket = None
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                response = await self.client.get('/eleven/conversation?mode=agents', headers=LOCAL)
                self.assertEqual(response.status, 409)
                response = await self.client.post('/eleven/settings', headers=LOCAL, json={'voiceId': 'other'})
                self.assertEqual(response.status, 409)
                self.assertEqual(dialogs, [], 'reservation must reject overlap before a dialog exists')
                release.set()
                socket = await asyncio.wait_for(first, 2)
                self.assertEqual((await socket.receive_json())['type'], 'live')
                self.assertEqual(len(dialogs), 1)
                self.assertEqual(self.store.list_calls()['total'], 1)
                await socket.send_str('stop')
                await socket.close()
                await wait_until(lambda: not self.server.eleven.operation_lock.locked())
                if self.server.eleven.crm.finalizers:
                    await asyncio.gather(*list(self.server.eleven.crm.finalizers))
                await settle_jobs(self.server.eleven.crm)
                call = self.store.list_calls()['items'][0]
                self.assertEqual(call['analysis_status'], 'ready')
                response = await self.client.get('/crm/calls/' + call['id'], headers=LOCAL)
                enriched = await response.json()
                self.assertEqual(enriched['call_number'], '+375290000000')
                self.assertEqual(enriched['call_display_name'], 'Имя текущего абонента')
                self.assertEqual(enriched['contact_name'], 'Имя текущего абонента')
            finally:
                release.set()
                if socket: await socket.close()
                if not first.done(): first.cancel()
                await asyncio.gather(first, return_exceptions=True)

    async def test_hangup_during_config_read_prevents_dialog_and_crm_chargeable_session(self):
        entered, release = threading.Event(), threading.Event()
        def slow_read():
            entered.set()
            if not release.wait(2): raise RuntimeError('test config read was not released')
            return dict(CONFIG)
        self.settings.read = slow_read
        self.server.bridge = SimpleNamespace(alive=True, stop_event=threading.Event(), stop=lambda _: None)
        with patch('eleven_routes.ElevenDialog') as dialog:
            first = asyncio.ensure_future(self.client.get('/eleven/conversation?mode=agents', headers=LOCAL))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                self.server.phone_monitor.snapshot = lambda **_: {'available': True, 'active': False,
                                                              'callPhase': 'idle', 'ageMs': 0}
                release.set()
                response = await asyncio.wait_for(first, 2)
                self.assertEqual(response.status, 409)
                self.assertEqual(self.store.list_calls()['total'], 0)
                dialog.assert_not_called()
                self.assertFalse(self.server.eleven.operation_lock.locked())
            finally:
                release.set()
                if not first.done(): first.cancel()
                await asyncio.gather(first, return_exceptions=True)

    async def test_replacement_call_during_config_read_cannot_inherit_old_request(self):
        entered, release = threading.Event(), threading.Event()
        def slow_read():
            entered.set()
            if not release.wait(2): raise RuntimeError('test config read was not released')
            return dict(CONFIG)
        self.settings.read = slow_read
        self.server.bridge = SimpleNamespace(alive=True, stop_event=threading.Event(), stop=lambda _: None)
        with patch('eleven_routes.ElevenDialog') as dialog:
            first = asyncio.ensure_future(self.client.get('/eleven/conversation?mode=agents', headers=LOCAL))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                self.server.phone_monitor.snapshot = lambda **_: {'available': True, 'active': True,
                    'callPhase': 'connected', 'audioOnComputer': True, 'ageMs': 0, 'callSequence': 2}
                release.set()
                response = await asyncio.wait_for(first, 2)
                self.assertEqual(response.status, 409)
                dialog.assert_not_called()
                self.assertEqual(self.store.list_calls()['total'], 0)
            finally:
                release.set()
                if not first.done(): first.cancel()
                await asyncio.gather(first, return_exceptions=True)


if __name__ == '__main__': unittest.main(verbosity=2)
