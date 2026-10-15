"""Offline SQLite durability, lifecycle and edit-preservation regression tests."""
from pathlib import Path
import sqlite3
import tempfile
import unittest

from crm_store import CrmStore, TRANSCRIPT_LIMIT


class CrmStoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'nested' / 'crm.sqlite3'
        self.store = CrmStore(self.path)

    def analysis(self, **changes):
        result = {'summary': 'Нужна автоматизация заказов.', 'interest': 'interested',
                  'name': 'Иван', 'phone': '+375290000000', 'telegram': '@ivan',
                  'next_step': 'Сергей уточнит требования.'}
        result.update(changes)
        return result

    def ended_call(self):
        call_id = self.store.create_call('agents')
        self.store.append_turn(call_id, 'user', 'Нужна автоматизация заказов.')
        self.store.append_turn(call_id, 'agent', 'Что занимает больше всего времени?')
        self.store.finish_call(call_id, 'completed', 12.5)
        return call_id

    def test_lazy_initialization_persistence_order_and_pagination(self):
        self.assertFalse(self.path.exists())
        call_id = self.ended_call()
        self.store.set_conversation_id(call_id, 'conv_test')
        reopened = CrmStore(self.path)
        record = reopened.get_call(call_id)
        self.assertEqual(record['mode'], 'agents')
        self.assertEqual(record['conversation_id'], 'conv_test')
        self.assertEqual(record['duration_seconds'], 12.5)
        self.assertTrue(record['started_at'].endswith('Z'))
        self.assertEqual([turn['role'] for turn in record['transcript']], ['user', 'assistant'])
        second = reopened.create_call('custom')
        page = reopened.list_calls(limit=1)
        self.assertEqual(page['total'], 2)
        self.assertEqual(page['items'][0]['id'], second)
        self.assertNotIn('transcript', page['items'][0])
        self.assertEqual(reopened.list_calls(limit=1, offset=1)['items'][0]['id'], call_id)

    def test_duplicate_finish_and_late_turn_do_not_reopen_or_overwrite(self):
        call_id = self.ended_call()
        self.store.save_analysis(call_id, self.analysis())
        first = self.store.get_call(call_id)
        second = self.store.finish_call(call_id, 'different_reason', 999)
        self.assertEqual(first, second)
        self.assertFalse(self.store.append_turn(call_id, 'user', 'Поздняя реплика'))
        self.assertEqual(self.store.list_pending(), [])

    def test_manual_contact_edits_and_intentional_blanks_survive_reanalysis(self):
        call_id = self.ended_call()
        self.store.save_analysis(call_id, self.analysis())
        self.store.update_call(call_id, {'name': 'Пётр', 'phone': '', 'telegram': '@petr',
                                         'notes': 'Проверить вечером.', 'follow_up': 'Завтра', 'status': 'callback'})
        record = CrmStore(self.path).save_analysis(call_id, self.analysis(summary='Обновлённый вывод.'))
        self.assertEqual(record['name'], 'Пётр')
        self.assertEqual(record['phone'], '')
        self.assertEqual(record['telegram'], '@petr')
        self.assertEqual(record['status'], 'callback')
        self.assertEqual(record['notes'], 'Проверить вечером.')
        self.assertEqual(record['follow_up'], 'Завтра')
        self.assertEqual(record['summary'], 'Обновлённый вывод.')

    def test_sql_strings_are_data_and_unknown_columns_are_rejected(self):
        call_id = self.ended_call()
        payload = "'; DROP TABLE calls;--"
        self.store.update_call(call_id, {'name': payload})
        self.assertEqual(self.store.get_call(call_id)['name'], payload)
        self.assertIsNone(self.store.get_call(payload))
        self.assertEqual(self.store.list_calls()['total'], 1)
        for changes in ({'state': 'finished'}, {'name=?; DROP TABLE calls;--': 'bad'},
                        {'status': 'invented'}, {'phone': '0' * 101}, {'notes': None}, {}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    self.store.update_call(call_id, changes)
        with self.assertRaises(ValueError):
            self.store.save_analysis(call_id, self.analysis(secret='do not store'))
        with self.assertRaises(ValueError):
            self.store.save_analysis(call_id, self.analysis(summary='x' * 2001))
        with self.assertRaises(ValueError):
            self.store.save_analysis(call_id, self.analysis(interest='guessed'))
        with self.assertRaises(ValueError):
            self.store.create_call('retell')

    def test_recovery_marks_partial_calls_interrupted_and_empty_calls_empty(self):
        partial = self.store.create_call('agents')
        self.store.append_turn(partial, 'user', 'Перезвоните, пожалуйста.')
        self.store.mark_analysis_error(partial, 'Часть реплик не сохранена.')
        self.assertEqual(self.store.get_call(partial)['analysis_status'], 'active')
        empty = self.store.create_call('custom')
        completed = self.ended_call()
        reopened = CrmStore(self.path)
        self.assertEqual(reopened.recover_interrupted(), 2)
        self.assertEqual(reopened.recover_interrupted(), 0)
        record = reopened.get_call(partial)
        self.assertEqual(record['state'], 'interrupted')
        self.assertEqual(record['end_reason'], 'server_interrupted')
        self.assertEqual(record['analysis_status'], 'pending')
        self.assertEqual(record['analysis_error'], 'Часть реплик не сохранена.')
        self.assertEqual(reopened.get_call(empty)['analysis_status'], 'empty')
        self.assertEqual(reopened.get_call(completed)['state'], 'finished')
        self.assertEqual({row['id'] for row in reopened.list_pending()}, {partial, completed})
        reopened.mark_analysis_error(partial, 'Анализ временно недоступен.')
        self.assertEqual([row['id'] for row in reopened.list_pending()], [completed])
        reopened.save_analysis(partial, self.analysis())
        self.assertEqual(reopened.get_call(partial)['analysis_error'], '')

    def test_transcript_bound_and_correction_of_only_latest_matching_agent(self):
        call_id = self.store.create_call('agents')
        for role, text in [('agent', 'Здравствуйте.'), ('user', 'Не слышно.'), ('agent', 'Здравствуйте.')]:
            self.store.append_turn(call_id, role, text)
        self.assertTrue(self.store.correct_last_agent_turn(call_id, 'Здравствуйте.', 'Добрый день.'))
        self.assertEqual([turn['text'] for turn in self.store.get_call(call_id)['transcript']],
                         ['Здравствуйте.', 'Не слышно.', 'Добрый день.'])
        self.assertTrue(self.store.correct_last_agent_turn(call_id, 'Добрый день.', ''))
        self.assertEqual(len(self.store.get_call(call_id)['transcript']), 2)
        self.store.append_turn(call_id, 'user', 'я' * TRANSCRIPT_LIMIT)
        record = self.store.get_call(call_id)
        self.assertEqual(sum(len(turn['text']) for turn in record['transcript']), TRANSCRIPT_LIMIT)
        self.assertTrue(record['transcript_truncated'])
        self.assertFalse(self.store.append_turn(call_id, 'user', 'Ещё слова.'))
        self.assertFalse(self.store.correct_last_agent_turn(call_id, 'Несуществующая реплика', 'Привет'))
        self.store.finish_call(call_id, 'completed', 1)
        self.assertFalse(self.store.correct_last_agent_turn(call_id, 'Здравствуйте.', 'Изменение после анализа'))

    def test_empty_call_and_invalid_duration_never_create_fabricated_analysis(self):
        call_id = self.store.create_call('custom')
        for duration in (float('nan'), float('inf'), -1, True, '3'):
            with self.assertRaises(ValueError):
                self.store.finish_call(call_id, 'completed', duration)
        with self.assertRaises(ValueError):
            self.store.save_analysis(call_id, self.analysis())
        self.store.finish_call(call_id, 'no_audio', 0)
        self.assertEqual(self.store.get_call(call_id)['analysis_status'], 'empty')
        with self.assertRaises(ValueError):
            self.store.save_analysis(call_id, self.analysis())
        self.store.mark_analysis_error(call_id, 'Нет распознанной речи.')
        self.assertEqual(self.store.get_call(call_id)['analysis_status'], 'empty')

    def test_incomplete_capture_warning_survives_reopen_retry_and_analysis(self):
        call_id = self.ended_call()
        self.assertFalse(self.store.get_call(call_id)['transcript_incomplete'])
        self.store.mark_transcript_incomplete(call_id)
        self.store.mark_transcript_incomplete(call_id)
        reopened = CrmStore(self.path)
        reopened.mark_analysis_error(call_id, 'Анализ временно недоступен.')
        reopened.save_analysis(call_id, self.analysis())
        record = reopened.get_call(call_id)
        self.assertEqual(record['analysis_status'], 'ready')
        self.assertTrue(record['transcript_incomplete'])
        self.assertFalse(record['transcript_truncated'])
        self.assertTrue(reopened.list_calls()['items'][0]['transcript_incomplete'])

    def test_pending_ids_include_all_pending_without_error_or_active(self):
        pending = [self.ended_call() for _ in range(23)]
        failed = self.ended_call()
        self.store.mark_analysis_error(failed, 'Анализ временно недоступен.')
        self.store.create_call('agents')
        self.assertEqual(self.store.pending_ids(), pending)
        self.assertEqual(len(self.store.list_pending()), 20)

    def test_old_database_gets_capture_loss_column_without_losing_records(self):
        call_id = self.ended_call()
        connection = sqlite3.connect(self.path)
        try:
            connection.execute('ALTER TABLE calls DROP COLUMN transcript_incomplete')
            connection.commit()
        finally:
            connection.close()
        reopened = CrmStore(self.path)
        self.assertFalse(reopened.get_call(call_id)['transcript_incomplete'])
        self.assertEqual(len(reopened.get_call(call_id)['transcript']), 2)
        reopened.mark_transcript_incomplete(call_id)
        self.assertTrue(reopened.get_call(call_id)['transcript_incomplete'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
