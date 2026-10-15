"""Local call records. SQLite lives beside private settings, never in the checkout."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sqlite3
import threading
import uuid


TRANSCRIPT_LIMIT = 120000
FIELD_LIMITS = {'summary': 2000, 'next_step': 1000, 'name': 200, 'phone': 100,
                'telegram': 200, 'notes': 5000, 'follow_up': 1000}
EDITABLE = {'name', 'phone', 'telegram', 'notes', 'follow_up', 'status'}
ANALYSIS_FIELDS = {'summary', 'interest', 'name', 'phone', 'telegram', 'next_step'}
STATUSES = {'new', 'callback', 'done', 'declined'}
INTERESTS = {'interested', 'not_interested', 'unclear'}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def _text(value, maximum, field):
    if not isinstance(value, str) or len(value) > maximum or '\x00' in value:
        raise ValueError('Некорректное поле CRM: ' + field)
    return value.strip()


class CrmStore:
    def __init__(self, path=None):
        self.path = (Path(path) if path is not None else
                     Path(os.environ['LOCALAPPDATA']) / 'RetellPhoneBridge' / 'crm.sqlite3')
        self._initialized = False
        self._init_lock = threading.RLock()

    def initialize(self):
        """Create the schema on first use; constructing the store has no disk effects."""
        with self._init_lock:
            if self._initialized:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=10)
            try:
                connection.execute('PRAGMA journal_mode=WAL')
                connection.executescript('''
                    CREATE TABLE IF NOT EXISTS calls (
                        id TEXT PRIMARY KEY,
                        mode TEXT NOT NULL CHECK(mode IN ('agents', 'custom')),
                        started_at TEXT NOT NULL,
                        ended_at TEXT,
                        state TEXT NOT NULL DEFAULT 'active'
                            CHECK(state IN ('active', 'finished', 'interrupted')),
                        duration_seconds REAL NOT NULL DEFAULT 0,
                        end_reason TEXT NOT NULL DEFAULT '',
                        conversation_id TEXT NOT NULL DEFAULT '',
                        analysis_status TEXT NOT NULL DEFAULT 'active'
                            CHECK(analysis_status IN ('active', 'pending', 'ready', 'error', 'empty')),
                        analysis_error TEXT NOT NULL DEFAULT '',
                        summary TEXT NOT NULL DEFAULT '',
                        interest TEXT NOT NULL DEFAULT 'unclear'
                            CHECK(interest IN ('interested', 'not_interested', 'unclear')),
                        name TEXT NOT NULL DEFAULT '',
                        phone TEXT NOT NULL DEFAULT '',
                        telegram TEXT NOT NULL DEFAULT '',
                        next_step TEXT NOT NULL DEFAULT '',
                        notes TEXT NOT NULL DEFAULT '',
                        follow_up TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'new'
                            CHECK(status IN ('new', 'callback', 'done', 'declined')),
                        edited_fields TEXT NOT NULL DEFAULT '[]',
                        transcript_chars INTEGER NOT NULL DEFAULT 0,
                        transcript_truncated INTEGER NOT NULL DEFAULT 0,
                        transcript_incomplete INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE TABLE IF NOT EXISTS turns (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        call_id TEXT NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
                        role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                        text TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS calls_started ON calls(started_at DESC);
                    CREATE INDEX IF NOT EXISTS turns_call ON turns(call_id, sequence);
                ''')
                # Preserve records created before capture-loss tracking was added.
                columns = {row[1] for row in connection.execute('PRAGMA table_info(calls)')}
                if 'transcript_incomplete' not in columns:
                    connection.execute('ALTER TABLE calls ADD COLUMN transcript_incomplete INTEGER NOT NULL DEFAULT 0')
                connection.commit()
            finally:
                connection.close()
            self._initialized = True

    @contextmanager
    def _connection(self, write=False):
        self.initialize()
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        try:
            if write:
                connection.execute('BEGIN IMMEDIATE')
            yield connection
            if write:
                connection.commit()
        except BaseException:
            if write:
                connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _row(row):
        if row is None:
            return None
        value = dict(row)
        value.pop('edited_fields', None)
        value['transcript_truncated'] = bool(value['transcript_truncated'])
        value['transcript_incomplete'] = bool(value['transcript_incomplete'])
        return value

    @staticmethod
    def _existing(connection, call_id):
        row = connection.execute('SELECT * FROM calls WHERE id=?', (call_id,)).fetchone()
        if row is None:
            raise KeyError('Разговор CRM не найден.')
        return row

    @classmethod
    def _detail(cls, connection, call_id):
        row = connection.execute('SELECT * FROM calls WHERE id=?', (call_id,)).fetchone()
        value = cls._row(row)
        if value is not None:
            value['transcript'] = [dict(turn) for turn in connection.execute(
                'SELECT role,text FROM turns WHERE call_id=? ORDER BY sequence', (call_id,))]
        return value

    def create_call(self, mode):
        if mode not in ('agents', 'custom'):
            raise ValueError('CRM доступна для разговоров ElevenLabs.')
        call_id = str(uuid.uuid4())
        with self._connection(write=True) as connection:
            connection.execute('INSERT INTO calls(id,mode,started_at) VALUES(?,?,?)',
                               (call_id, mode, _now()))
        return call_id

    def append_turn(self, call_id, role, text):
        if role == 'agent':
            role = 'assistant'
        if role not in ('user', 'assistant') or not isinstance(text, str) or '\x00' in text:
            raise ValueError('Некорректная реплика CRM.')
        text = text.strip()
        if not text:
            return False
        with self._connection(write=True) as connection:
            row = self._existing(connection, call_id)
            if row['state'] != 'active':
                return False
            remaining = max(0, TRANSCRIPT_LIMIT - row['transcript_chars'])
            truncated = len(text) > remaining
            text = text[:remaining]
            if text:
                connection.execute('INSERT INTO turns(call_id,role,text) VALUES(?,?,?)',
                                   (call_id, role, text))
            connection.execute('''UPDATE calls SET transcript_chars=transcript_chars+?,
                transcript_truncated=MAX(transcript_truncated,?) WHERE id=?''',
                               (len(text), int(truncated), call_id))
            return bool(text)

    def correct_last_agent_turn(self, call_id, original_text, text):
        if (not isinstance(original_text, str) or not isinstance(text, str)
                or '\x00' in original_text or '\x00' in text):
            raise ValueError('Некорректное исправление реплики CRM.')
        original_text, text = original_text.strip(), text.strip()
        with self._connection(write=True) as connection:
            row = self._existing(connection, call_id)
            if row['state'] != 'active':
                return False
            turn = connection.execute('''SELECT sequence,text FROM turns
                WHERE call_id=? AND role='assistant' AND text=? ORDER BY sequence DESC LIMIT 1''',
                                      (call_id, original_text)).fetchone()
            if turn is None:
                return False
            old_length = len(turn['text'])
            remaining = max(0, TRANSCRIPT_LIMIT - row['transcript_chars'] + old_length)
            truncated = len(text) > remaining
            text = text[:remaining]
            if text:
                connection.execute('UPDATE turns SET text=? WHERE sequence=?', (text, turn['sequence']))
            else:
                connection.execute('DELETE FROM turns WHERE sequence=?', (turn['sequence'],))
            connection.execute('''UPDATE calls SET transcript_chars=transcript_chars+?,
                transcript_truncated=MAX(transcript_truncated,?) WHERE id=?''',
                               (len(text) - old_length, int(truncated), call_id))
            return True

    def set_conversation_id(self, call_id, conversation_id):
        conversation_id = _text(conversation_id, 240, 'conversation_id')
        with self._connection(write=True) as connection:
            self._existing(connection, call_id)
            connection.execute('UPDATE calls SET conversation_id=? WHERE id=?', (conversation_id, call_id))

    def mark_transcript_incomplete(self, call_id):
        """Persist a capture-loss warning independently of analysis/retry outcomes."""
        with self._connection(write=True) as connection:
            self._existing(connection, call_id)
            connection.execute('UPDATE calls SET transcript_incomplete=1 WHERE id=?', (call_id,))

    def finish_call(self, call_id, end_reason, duration_seconds):
        end_reason = _text(end_reason, 160, 'end_reason')
        if (isinstance(duration_seconds, bool) or not isinstance(duration_seconds, (int, float))
                or not math.isfinite(duration_seconds) or duration_seconds < 0):
            raise ValueError('Некорректная длительность разговора CRM.')
        with self._connection(write=True) as connection:
            row = self._existing(connection, call_id)
            if row['state'] == 'active':
                analysis = 'pending' if row['transcript_chars'] else 'empty'
                connection.execute('''UPDATE calls SET state='finished',ended_at=?,end_reason=?,
                    duration_seconds=?,analysis_status=? WHERE id=?''',
                                   (_now(), end_reason, float(duration_seconds), analysis, call_id))
            return self._detail(connection, call_id)

    def list_calls(self, limit=50, offset=0):
        if (type(limit) is not int or not 1 <= limit <= 200
                or type(offset) is not int or offset < 0):
            raise ValueError('Некорректная страница CRM.')
        with self._connection() as connection:
            total = connection.execute('SELECT COUNT(*) FROM calls').fetchone()[0]
            rows = connection.execute('SELECT * FROM calls ORDER BY started_at DESC,rowid DESC LIMIT ? OFFSET ?',
                                      (limit, offset))
            return {'items': [self._row(row) for row in rows], 'total': total}

    def get_call(self, call_id):
        with self._connection() as connection:
            return self._detail(connection, call_id)

    def update_call(self, call_id, changes):
        if not isinstance(changes, dict) or not changes or set(changes) - EDITABLE:
            raise ValueError('Неизвестные поля CRM.')
        values = {}
        for key, value in changes.items():
            value = _text(value, FIELD_LIMITS.get(key, 20), key)
            if key == 'status' and value not in STATUSES:
                raise ValueError('Неизвестный статус CRM.')
            values[key] = value
        with self._connection(write=True) as connection:
            row = self._existing(connection, call_id)
            edited = set(json.loads(row['edited_fields'])) | set(values)
            columns = ','.join(key + '=?' for key in values)  # Keys are whitelisted above.
            connection.execute('UPDATE calls SET ' + columns + ',edited_fields=? WHERE id=?',
                               (*values.values(), json.dumps(sorted(edited)), call_id))
            return self._detail(connection, call_id)

    def save_analysis(self, call_id, result):
        if not isinstance(result, dict) or set(result) != ANALYSIS_FIELDS:
            raise ValueError('Неполный или некорректный результат анализа CRM.')
        values = {key: _text(value, FIELD_LIMITS.get(key, 30), key) for key, value in result.items()}
        if values['interest'] not in INTERESTS:
            raise ValueError('Неизвестный результат интереса CRM.')
        with self._connection(write=True) as connection:
            row = self._existing(connection, call_id)
            if row['state'] == 'active' or not row['transcript_chars']:
                raise ValueError('Для анализа CRM нужен завершённый разговор с репликами.')
            edited = set(json.loads(row['edited_fields']))
            values = {key: value for key, value in values.items() if key not in edited}
            columns = ','.join(key + '=?' for key in values)  # Validated analysis fields only.
            connection.execute('UPDATE calls SET ' + columns + ",analysis_status='ready',analysis_error='' WHERE id=?",
                               (*values.values(), call_id))
            return self._detail(connection, call_id)

    def mark_analysis_error(self, call_id, reason):
        reason = _text(reason, 500, 'analysis_error')
        with self._connection(write=True) as connection:
            row = self._existing(connection, call_id)
            status = ('active' if row['state'] == 'active' else
                      'error' if row['transcript_chars'] else 'empty')
            connection.execute('UPDATE calls SET analysis_status=?,analysis_error=? WHERE id=?',
                               (status, reason, call_id))

    def list_pending(self, limit=20):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('Некорректный размер очереди CRM.')
        with self._connection() as connection:
            ids = [row[0] for row in connection.execute('''SELECT id FROM calls
                WHERE analysis_status='pending' ORDER BY started_at LIMIT ?''', (limit,))]
            return [self._detail(connection, call_id) for call_id in ids]

    def pending_ids(self):
        """Enumerate every pending record without loading potentially long transcripts."""
        with self._connection() as connection:
            return [row[0] for row in connection.execute('''SELECT id FROM calls
                WHERE analysis_status='pending' ORDER BY started_at,rowid''')]

    def recover_interrupted(self):
        now = _now()
        with self._connection(write=True) as connection:
            rows = list(connection.execute("SELECT id,started_at,transcript_chars FROM calls WHERE state='active'"))
            for row in rows:
                duration = max(0, (datetime.fromisoformat(now) - datetime.fromisoformat(row['started_at'])).total_seconds())
                connection.execute('''UPDATE calls SET state='interrupted',ended_at=?,duration_seconds=?,
                    end_reason='server_interrupted',analysis_status=? WHERE id=?''',
                                   (now, duration, 'pending' if row['transcript_chars'] else 'empty', row['id']))
            return len(rows)
