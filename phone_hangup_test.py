"""Guarded hangup tests with fake command processes; never end a real call."""
import asyncio
import json
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

from phone_state import PhoneStateMonitor

TOKEN = 'a' * 64
OTHER = 'b' * 64
CONNECTED = {'available': True, 'active': True, 'callPhase': 'connected', 'callCount': 1,
             'callSequence': 7, 'audioOnComputer': True, 'phoneCallToken': TOKEN}


class HangupTests(unittest.IsolatedAsyncioTestCase):
    def monitor(self, **changes):
        monitor = PhoneStateMonitor()
        monitor._state = monitor._parse_state(dict(CONNECTED, **changes))
        monitor._last_update = time.monotonic()
        monitor._end_command = AsyncMock(return_value={'status': 'succeeded'})
        return monitor

    async def test_wrong_new_stale_multiple_ended_and_transferred_calls_never_run_command(self):
        cases = [({'callSequence': 8, 'phoneCallToken': OTHER}, 'wrong-call'),
                 ({'phoneCallToken': OTHER}, 'wrong-call'),
                 ({'callCount': 2}, 'multiple-calls'),
                 ({'callPhase': 'held', 'active': False}, 'not-connected'),
                 ({'callPhase': 'idle', 'active': False, 'callCount': 0}, 'not-connected'),
                 ({'audioOnComputer': False}, 'not-on-computer'),
                 ({'available': False}, 'unavailable')]
        for changes, status in cases:
            monitor = self.monitor(**changes)
            self.assertEqual(await monitor.end_call(7, TOKEN), {'ok': False, 'status': status})
            monitor._end_command.assert_not_awaited()
        monitor = self.monitor()
        monitor._last_update -= 3
        self.assertEqual((await monitor.end_call(7, TOKEN))['status'], 'stale')
        monitor._end_command.assert_not_awaited()

    async def test_invalid_identifiers_never_run_command(self):
        monitor = self.monitor()
        for sequence, token in ((True, TOKEN), (0, TOKEN), ('7', TOKEN), (7, ''), (7, TOKEN + 'x')):
            self.assertEqual(await monitor.end_call(sequence, token), {'ok': False, 'status': 'invalid-target'})
        monitor._end_command.assert_not_awaited()

    async def test_succeeded_action_requires_fresh_end_confirmation(self):
        monitor = self.monitor()
        async def end(_):
            await asyncio.sleep(0)
            monitor._state = monitor._parse_state(dict(CONNECTED, callPhase='idle', active=False, callCount=0))
            monitor._last_update = time.monotonic()
            return {'status': 'succeeded'}
        monitor._end_command.side_effect = end
        self.assertEqual(await monitor.end_call(7, TOKEN), {'ok': True, 'status': 'ended'})
        monitor._end_command.assert_awaited_once_with(TOKEN)

    async def test_failed_or_timed_out_action_is_never_retried(self):
        for failure in ('timed-out', 'command-failed', 'connection-lost'):
            monitor = self.monitor()
            monitor._end_command.return_value = {'status': failure}
            self.assertEqual(await monitor.end_call(7, TOKEN), {'ok': False, 'status': failure})
            self.assertEqual((await monitor.end_call(7, TOKEN))['status'], 'already-requested')
            monitor._end_command.assert_awaited_once()

    async def test_concurrent_duplicate_is_rejected_before_first_action_finishes(self):
        monitor = self.monitor()
        entered, release = asyncio.Event(), asyncio.Event()
        async def pending(_):
            entered.set()
            await release.wait()
            return {'status': 'timed-out'}
        monitor._end_command.side_effect = pending
        first = asyncio.create_task(monitor.end_call(7, TOKEN))
        try:
            await entered.wait()
            self.assertEqual((await monitor.end_call(7, TOKEN))['status'], 'already-requested')
            release.set()
            await first
            monitor._end_command.assert_awaited_once()
        finally:
            release.set()
            await first

    async def test_success_without_monitor_confirmation_times_out_without_retry(self):
        monitor = self.monitor()
        started = time.monotonic()
        self.assertEqual(await monitor.end_call(7, TOKEN), {'ok': False, 'status': 'confirmation-timeout'})
        self.assertLess(time.monotonic() - started, 4)
        monitor._end_command.assert_awaited_once()

    async def test_control_token_is_excluded_from_public_and_contact_snapshots(self):
        monitor = self.monitor(phoneNumber='+375290000000', displayName='Тест')
        self.assertNotIn('phoneCallToken', monitor.snapshot())
        self.assertNotIn('phoneCallToken', monitor.snapshot(include_contact=True))
        self.assertEqual(monitor.snapshot(include_control=True)['phoneCallToken'], TOKEN)
        self.assertNotIn('phoneNumber', monitor.snapshot(include_control=True))
        monitor._last_update -= 3
        self.assertNotIn('phoneCallToken', monitor.snapshot(include_contact=True, include_control=True))

    async def test_command_token_travels_only_on_stdin_and_results_are_sanitized(self):
        monitor = PhoneStateMonitor()
        process = Mock(returncode=0)
        process.communicate = AsyncMock(return_value=(json.dumps({'operation': 'end-call', 'status': 'wrong-call',
                                                                'extra': 'private value'}).encode(), b''))
        create = AsyncMock(return_value=process)
        with patch('phone_state.asyncio.create_subprocess_exec', create):
            result = await monitor._end_command(TOKEN)
        self.assertEqual(result, {'status': 'wrong-call'})
        self.assertNotIn(TOKEN, ' '.join(create.call_args.args))
        process.communicate.assert_awaited_once_with((TOKEN + '\n').encode())

    async def test_command_timeout_stops_helper_and_does_not_return_raw_output(self):
        monitor = PhoneStateMonitor()
        process = Mock(returncode=None)
        process.communicate = AsyncMock(side_effect=TimeoutError())
        process.wait = AsyncMock(return_value=-9)
        with patch('phone_state.asyncio.create_subprocess_exec', AsyncMock(return_value=process)):
            result = await monitor._end_command(TOKEN)
        self.assertEqual(result, {'status': 'timed-out'})
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()


if __name__ == '__main__': unittest.main(verbosity=2)
