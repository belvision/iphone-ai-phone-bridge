"""Offline current-call metadata/privacy checks; no real contacts or calls."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from phone_state import PhoneStateMonitor


CONNECTED = {'available': True, 'active': True, 'callPhase': 'connected', 'callCount': 1,
             'callSequence': 7, 'audioOnComputer': True, 'callDirection': 'outgoing',
             'phoneNumber': '+375 29 000-00-00', 'displayName': 'Тестовый контакт',
             'CallId': 'private-call-identifier', 'LineId': 'private-line-identifier'}


class PhoneContactTests(unittest.TestCase):
    def set_state(self, monitor, values, observed=100):
        monitor._state = monitor._parse_state(values)
        monitor._last_update = observed

    def test_default_snapshot_never_exposes_contacts_and_private_snapshot_is_copy(self):
        monitor = PhoneStateMonitor()
        self.set_state(monitor, CONNECTED)
        with patch('phone_state.time.monotonic', return_value=100.1):
            public = monitor.snapshot()
            private = monitor.snapshot(include_contact=True)
        self.assertNotIn('phoneNumber', public)
        self.assertNotIn('displayName', public)
        self.assertNotIn('Тестовый контакт', json.dumps(public, ensure_ascii=False))
        self.assertEqual(private['phoneNumber'], CONNECTED['phoneNumber'])
        self.assertEqual(private['displayName'], CONNECTED['displayName'])
        self.assertNotIn('CallId', private)
        self.assertNotIn('LineId', private)
        private['displayName'] = 'Changed in a consumer'
        self.assertEqual(monitor._state['displayName'], CONNECTED['displayName'])

    def test_stale_missing_and_future_observation_never_return_contact(self):
        monitor = PhoneStateMonitor()
        for observed in (90, 0, 101):
            self.set_state(monitor, CONNECTED, observed)
            with patch('phone_state.time.monotonic', return_value=100):
                result = monitor.snapshot(include_contact=True)
            self.assertNotIn('phoneNumber', result)
            self.assertNotIn('displayName', result)
        self.set_state(monitor, CONNECTED, 90)
        with patch('phone_state.time.monotonic', return_value=100):
            self.assertFalse(monitor.snapshot()['active'])

    def test_ambiguous_unavailable_and_nonconnected_state_discard_contacts(self):
        monitor = PhoneStateMonitor()
        variants = [dict(CONNECTED, callCount=0), dict(CONNECTED, callCount=2),
                    dict(CONNECTED, available=False), dict(CONNECTED, active=False)]
        variants += [dict(CONNECTED, callPhase=phase) for phase in
                     ('idle', 'dialing', 'incoming', 'held', 'ended', 'lost', 'multiple', 'unknown')]
        with patch('phone_state.time.monotonic', return_value=100):
            for state in variants:
                self.set_state(monitor, state)
                result = monitor.snapshot(include_contact=True)
                self.assertNotIn('phoneNumber', result, state)
                self.assertNotIn('displayName', result, state)
                self.assertNotIn('phoneNumber', monitor._state, 'ineligible contact is not retained in memory')

    def test_contact_values_are_bounded_sanitized_and_not_coerced_from_objects(self):
        state = PhoneStateMonitor._parse_state(dict(CONNECTED, phoneNumber=' +375\r\n00\x00 ',
                                                   displayName=' Имя\t\x7f ' + 'я' * 500))
        self.assertEqual(state['phoneNumber'], '+37500')
        self.assertEqual(len(state['displayName']), 200)
        self.assertNotIn('\t', state['displayName'])
        self.assertNotIn('\x7f', state['displayName'])
        state = PhoneStateMonitor._parse_state(dict(CONNECTED, phoneNumber={'secret': 'not-a-number'},
                                                   displayName=['not-a-name']))
        self.assertEqual(state['phoneNumber'], '')
        self.assertEqual(state['displayName'], '')

    def test_sidecar_reader_clears_previous_contact_when_next_call_has_no_metadata(self):
        monitor = PhoneStateMonitor()
        seen = []
        next_call = dict(CONNECTED, callSequence=8)
        del next_call['phoneNumber']
        del next_call['displayName']
        class Pipe:
            def __iter__(self):
                for state in (CONNECTED, next_call, dict(next_call, callPhase='idle', active=False, callCount=0)):
                    yield json.dumps(state) + '\n'
                    seen.append((monitor.snapshot(), monitor.snapshot(include_contact=True)))
            def close(self): pass
        process = SimpleNamespace(stdout=Pipe())
        monitor._process = process
        with patch('phone_state.time.monotonic', return_value=100): monitor._read(process)
        self.assertEqual(seen[0][1]['displayName'], CONNECTED['displayName'])
        self.assertEqual(seen[1][1]['displayName'], '')
        self.assertEqual(seen[1][1]['phoneNumber'], '')
        self.assertNotIn('displayName', seen[2][1])
        self.assertTrue(all('phoneNumber' not in public and 'displayName' not in public for public, _ in seen))
        self.assertNotIn('displayName', monitor.snapshot(include_contact=True), 'exited process clears contact state')


if __name__ == '__main__': unittest.main(verbosity=2)
