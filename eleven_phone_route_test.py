"""No real phone or cloud calls: verify tool/phone completion orchestration."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from eleven_routes import ElevenService


class EndPhoneRouteTests(unittest.IsolatedAsyncioTestCase):
    def service(self):
        service = object.__new__(ElevenService)
        service.server = SimpleNamespace(phone_monitor=SimpleNamespace(
            end_call=AsyncMock(return_value={'ok': True, 'status': 'ended'})))
        return service

    async def test_missing_command_or_incomplete_audio_never_hangs_up(self):
        cases = [dict(mode='custom', end_call_requested=True, end_call_audio_drained=True),
                 dict(mode='agents', end_call_requested=False, end_call_audio_drained=True),
                 dict(mode='agents', end_call_requested=True, end_call_audio_drained=False)]
        for fields in cases:
            service = self.service()
            result = await service.end_phone_after_agent(SimpleNamespace(**fields), 4, 'a'*64)
            self.assertFalse(result['ok'])
            service.server.phone_monitor.end_call.assert_not_awaited()

    async def test_verified_command_targets_original_call_and_keeps_safe_result(self):
        service = self.service()
        dialog = SimpleNamespace(mode='agents', end_call_requested=True, end_call_audio_drained=True)
        self.assertFalse((await service.end_phone_after_agent(dialog, 4, ''))['ok'])
        service.server.phone_monitor.end_call.assert_not_awaited()
        self.assertEqual(await service.end_phone_after_agent(dialog, 4, 'a'*64), {'ok': True, 'status': 'ended'})
        service.server.phone_monitor.end_call.assert_awaited_once_with(4, 'a'*64)

    async def test_transport_error_redacted_and_cancellation_not_swallowed(self):
        service = self.service()
        dialog = SimpleNamespace(mode='agents', end_call_requested=True, end_call_audio_drained=True)
        service.server.phone_monitor.end_call.side_effect = RuntimeError('private-process-details')
        self.assertEqual(await service.end_phone_after_agent(dialog, 4, 'a'*64),
                         {'ok': False, 'status': 'command-failed'})
        service.server.phone_monitor.end_call.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await service.end_phone_after_agent(dialog, 4, 'a'*64)


if __name__ == '__main__': unittest.main()
