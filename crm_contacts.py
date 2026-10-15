"""Local address book and conservative call matching; no remote account access."""
from contextlib import contextmanager
from datetime import datetime, timezone
import re
import sqlite3
import threading
import uuid


MAX_CONTACTS = 25000
MAX_PHONES = 100000
MAX_VCARD_BYTES = 16 * 1024 * 1024
SOURCES = {'google', 'iphone', 'vcard', 'manual'}


def normalize_phone(value):
    """Return an unambiguous international number, or an empty string."""
    if not isinstance(value, str) or len(value) > 100:
        return ''
    value = value.strip()
    if value.lower().startswith('tel:'):
        value = value[4:]
    if not re.fullmatch(r'[+0-9\s().-]+', value):
        return ''
    value = re.sub(r'[\s().-]', '', value)
    if value.startswith('00'):
        value = '+' + value[2:]
    if value.startswith('+'):
        digits = value[1:]
        if not re.fullmatch(r'[1-9][0-9]{7,14}', digits):
            return ''
        if digits.startswith('375') and not re.fullmatch(r'375[1-9][0-9]{8}', digits):
            return ''
        return value
    if re.fullmatch(r'375[1-9][0-9]{8}', value):
        return '+' + value
    if re.fullmatch(r'80[1-9][0-9]{8}', value):
        return '+375' + value[2:]
    if re.fullmatch(r'0[1-9][0-9]{8}', value):
        return '+375' + value[1:]
    if re.fullmatch(r'(?:29|25|33|44)[0-9]{7}', value):
        return '+375' + value
    return ''


def _text(value, maximum, field):
    if not isinstance(value, str) or len(value) > maximum or '\x00' in value:
        raise ValueError('Некорректное поле контакта: ' + field)
    return value.strip()


def _name_key(value):
    return ' '.join(value.casefold().split())


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def _unescape(value):
    return re.sub(r'\\([\\,;nN])', lambda match: '\n' if match[1] in 'nN' else match[1], value)


def _structured_name(value):
    parts, current, escaped = [], [], False
    for character in value:
        if escaped:
            current.extend(('\\', character))
            escaped = False
        elif character == '\\':
            escaped = True
        elif character == ';':
            parts.append(''.join(current))
            current = []
        else:
            current.append(character)
    if escaped:
        current.append('\\')
    parts.append(''.join(current))
    parts += [''] * max(0, 5 - len(parts))
    return ' '.join(_unescape(parts[index]).strip() for index in (3, 1, 2, 0, 4)
                    if _unescape(parts[index]).strip())


def parse_vcard(text):
    """Read UTF-8 vCard 3/4 FN/N/TEL; other properties never enter the database."""
    if (not isinstance(text, str) or len(text) > MAX_VCARD_BYTES or '\x00' in text
            or len(text.encode('utf-8')) > MAX_VCARD_BYTES):
        raise ValueError('Файл vCard должен быть текстом UTF-8 размером не более 16 МиБ.')
    # Folded lines continue with one whitespace character; do not strip their content.
    lines, fragments = [], []
    for line in text.lstrip('\ufeff').splitlines():
        if line.startswith((' ', '\t')) and fragments:
            fragments.append(line[1:])
        else:
            if fragments:
                lines.append(''.join(fragments))
            fragments = [line]
    if fragments:
        lines.append(''.join(fragments))
    items, current, cards, phones = [], None, 0, 0
    for line in lines:
        header, separator, value = line.partition(':')
        if not separator:
            continue
        property_name = header.split(';', 1)[0].rsplit('.', 1)[-1].upper()
        if property_name == 'BEGIN' and value.upper() == 'VCARD':
            if current is not None:
                raise ValueError('Некорректная структура файла vCard.')
            cards += 1
            if cards > MAX_CONTACTS:
                raise ValueError('В одном импорте допускается не более 25000 контактов.')
            current = {'version': '', 'fn': '', 'n': '', 'phones': []}
        elif property_name == 'END' and value.upper() == 'VCARD':
            if current is not None and current['version'] in ('3.0', '4.0'):
                name = (current['fn'] or current['n']).strip()
                items.append({'name': name[:200], 'phones': current['phones']})
            current = None
        elif current is not None:
            if property_name == 'VERSION':
                current['version'] = value.strip()
            elif property_name in ('FN', 'N'):
                if 'ENCODING=' not in header.upper():
                    current[property_name.lower()] = (_unescape(value) if property_name == 'FN'
                                                       else _structured_name(value))
            elif property_name == 'TEL':
                phones += 1
                if phones > MAX_PHONES:
                    raise ValueError('В одном импорте допускается не более 100000 телефонных номеров.')
                # Keep unsupported formats for import's skipped accounting, without truncating numbers.
                current['phones'].append(_unescape(value).strip())
    if current is not None:
        raise ValueError('Файл vCard обрывается до конца контакта.')
    if not items:
        raise ValueError('В файле не найдены контакты vCard 3.0 или 4.0 в кодировке UTF-8.')
    return items


class ContactsStore:
    def __init__(self, crm_store):
        self.crm_store = crm_store
        self.path = crm_store.path
        self._initialized = False
        self._init_lock = threading.RLock()

    def initialize(self):
        with self._init_lock:
            if self._initialized:
                return
            self.crm_store.initialize()
            connection = sqlite3.connect(self.path, timeout=10)
            try:
                connection.executescript('''
                    CREATE TABLE IF NOT EXISTS contacts (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL DEFAULT '',
                        name_key TEXT NOT NULL DEFAULT '',
                        source TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS contact_phones (
                        contact_id TEXT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
                        phone TEXT NOT NULL,
                        UNIQUE(contact_id,phone)
                    );
                    CREATE INDEX IF NOT EXISTS contact_number ON contact_phones(phone);
                    CREATE INDEX IF NOT EXISTS contact_name ON contacts(name_key);
                    CREATE TABLE IF NOT EXISTS call_contacts (
                        call_id TEXT PRIMARY KEY REFERENCES calls(id) ON DELETE CASCADE,
                        contact_id TEXT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
                        link_kind TEXT NOT NULL DEFAULT 'manual'
                    );
                    CREATE TABLE IF NOT EXISTS call_identity (
                        call_id TEXT PRIMARY KEY REFERENCES calls(id) ON DELETE CASCADE,
                        phone_raw TEXT NOT NULL DEFAULT '',
                        phone_normalized TEXT NOT NULL DEFAULT '',
                        display_name TEXT NOT NULL DEFAULT ''
                    );
                ''')
                columns = {row[1] for row in connection.execute('PRAGMA table_info(call_contacts)')}
                if 'link_kind' not in columns:
                    connection.execute("ALTER TABLE call_contacts ADD COLUMN link_kind TEXT NOT NULL DEFAULT 'manual'")
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
            connection.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _contacts(connection, rows):
        result = [dict(row) for row in rows]
        if not result:
            return result
        by_id = {row['id']: row for row in result}
        for row in result:
            row.pop('name_key', None)
            row['phones'] = []
        placeholders = ','.join('?' for _ in result)
        for row in connection.execute('SELECT contact_id,phone FROM contact_phones WHERE contact_id IN (' +
                                      placeholders + ') ORDER BY rowid', tuple(by_id)):
            by_id[row['contact_id']]['phones'].append(row['phone'])
        return result

    @classmethod
    def _contact(cls, connection, contact_id):
        rows = connection.execute('SELECT * FROM contacts WHERE id=?', (contact_id,)).fetchall()
        values = cls._contacts(connection, rows)
        return values[0] if values else None

    @classmethod
    def _match(cls, connection, phone):
        phone = normalize_phone(phone)
        if not phone:
            return {'state': 'none', 'contact': None}
        rows = connection.execute('SELECT contact_id FROM contact_phones WHERE phone=? LIMIT 2', (phone,)).fetchall()
        if len(rows) > 1:
            return {'state': 'ambiguous', 'contact': None}
        return ({'state': 'matched', 'contact': cls._contact(connection, rows[0][0])} if rows else
                {'state': 'none', 'contact': None})

    @staticmethod
    def _new_contact(connection, name, phones, source):
        contact_id, now = str(uuid.uuid4()), _now()
        connection.execute('INSERT INTO contacts(id,name,name_key,source,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                           (contact_id, name, _name_key(name), source, now, now))
        connection.executemany('INSERT INTO contact_phones(contact_id,phone) VALUES(?,?)',
                               [(contact_id, phone) for phone in phones])
        return contact_id

    def import_contacts(self, items, source='google'):
        if not isinstance(source, str) or source not in SOURCES or not isinstance(items, list) or len(items) > MAX_CONTACTS:
            raise ValueError('Некорректный источник или размер списка контактов.')
        prepared, total_phones = [], 0
        for item in items:
            if not isinstance(item, dict) or set(item) != {'name', 'phones'} or not isinstance(item['phones'], list):
                raise ValueError('Контакт должен содержать имя и список телефонов.')
            name = _text(item['name'], 200, 'name')
            total_phones += len(item['phones'])
            if total_phones > MAX_PHONES:
                raise ValueError('В одном импорте допускается не более 100000 телефонных номеров.')
            phones = list(dict.fromkeys(phone for value in item['phones'] if (phone := normalize_phone(value))))
            prepared.append((name, phones))
        counts = {'added': 0, 'updated': 0, 'skipped': 0}
        with self._connection(write=True) as connection:
            # Load indexes once; importing a large address book uses one transaction and linear scans.
            names = {row['id']: row['name'] for row in connection.execute('SELECT id,name FROM contacts')}
            by_phone = {}
            for row in connection.execute('SELECT contact_id,phone FROM contact_phones'):
                by_phone.setdefault(row['phone'], set()).add(row['contact_id'])
            for name, phones in prepared:
                if not phones:
                    counts['skipped'] += 1
                    continue
                candidates = set().union(*(by_phone.get(phone, set()) for phone in phones))
                if len(candidates) > 1:
                    counts['skipped'] += 1
                    continue
                contact_id = next(iter(candidates), None)
                existing_name = names.get(contact_id, '')
                if contact_id and existing_name and name and _name_key(existing_name) != _name_key(name):
                    # A shared household/company number cannot prove two different names are one person.
                    contact_id = None
                if contact_id is None:
                    contact_id = self._new_contact(connection, name, phones, source)
                    names[contact_id] = name
                    for phone in phones:
                        by_phone.setdefault(phone, set()).add(contact_id)
                    counts['added'] += 1
                else:
                    additions = [phone for phone in phones if contact_id not in by_phone.get(phone, set())]
                    rename = bool(name and not existing_name)
                    if not additions and not rename:
                        counts['skipped'] += 1
                        continue
                    for phone in additions:
                        connection.execute('INSERT INTO contact_phones(contact_id,phone) VALUES(?,?)', (contact_id, phone))
                        by_phone.setdefault(phone, set()).add(contact_id)
                    if rename:
                        names[contact_id] = name
                        connection.execute('UPDATE contacts SET name=?,name_key=?,updated_at=? WHERE id=?',
                                           (name, _name_key(name), _now(), contact_id))
                    else:
                        connection.execute('UPDATE contacts SET updated_at=? WHERE id=?', (_now(), contact_id))
                    counts['updated'] += 1
        return counts

    def search(self, query='', limit=50, offset=0):
        query = _text(query, 200, 'search')
        if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or offset < 0:
            raise ValueError('Некорректная страница контактов.')
        name = _name_key(query).replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        digits = re.sub(r'[^0-9]', '', query) if re.fullmatch(r'[+0-9\s().-]+', query) else ''
        clause = " WHERE c.name_key LIKE ? ESCAPE '\\'"
        parameters = ['%' + name + '%']
        if digits:
            clause += ' OR EXISTS (SELECT 1 FROM contact_phones p WHERE p.contact_id=c.id AND p.phone LIKE ?)'
            parameters.append('%' + digits + '%')
        with self._connection() as connection:
            total = connection.execute('SELECT COUNT(*) FROM contacts c' + clause, parameters).fetchone()[0]
            rows = connection.execute('SELECT c.* FROM contacts c' + clause + ' ORDER BY c.name_key,c.id LIMIT ? OFFSET ?',
                                      (*parameters, limit, offset)).fetchall()
            return {'items': self._contacts(connection, rows), 'total': total}

    def match(self, phone):
        with self._connection() as connection:
            return self._match(connection, phone)

    @staticmethod
    def _require_call(connection, call_id):
        if connection.execute('SELECT 1 FROM calls WHERE id=?', (call_id,)).fetchone() is None:
            raise KeyError('Разговор CRM не найден.')

    @staticmethod
    def _identity(connection, call_id, phone, name):
        connection.execute('''INSERT OR IGNORE INTO call_identity(call_id,phone_raw,phone_normalized,display_name)
            VALUES(?,?,?,?)''', (call_id, phone, normalize_phone(phone), name))

    @classmethod
    def _enrich(cls, connection, record):
        if record is None:
            return None
        result = dict(record)
        result.update(contact_id='', contact_name='', contact_phones=[], call_number='', call_display_name='')
        identity = connection.execute('SELECT * FROM call_identity WHERE call_id=?', (record['id'],)).fetchone()
        if identity:
            result['call_number'] = identity['phone_normalized'] or identity['phone_raw']
            result['call_display_name'] = identity['display_name']
        link = connection.execute('SELECT contact_id,link_kind FROM call_contacts WHERE call_id=?', (record['id'],)).fetchone()
        if link and link['link_kind'] == 'auto':
            match = cls._match(connection, identity['phone_normalized'] if identity else '')
            if match['state'] != 'matched' or match['contact']['id'] != link['contact_id']:
                link = None
        if link:
            contact = cls._contact(connection, link[0])
            if contact:
                result.update(contact_id=contact['id'], contact_name=contact['name'], contact_phones=contact['phones'])
        return result

    def enrich(self, record):
        with self._connection() as connection:
            return self._enrich(connection, record)

    def attach_identity(self, call_id, phone, name=''):
        phone, name = _text(phone, 100, 'phone'), _text(name, 200, 'name')
        with self._connection(write=True) as connection:
            self._require_call(connection, call_id)
            self._identity(connection, call_id, phone, name)
        return self.enrich(self.crm_store.get_call(call_id))

    def link_call(self, call_id, contact_id):
        with self._connection(write=True) as connection:
            self._require_call(connection, call_id)
            if self._contact(connection, contact_id) is None:
                raise KeyError('Контакт не найден.')
            connection.execute('''INSERT INTO call_contacts(call_id,contact_id,link_kind) VALUES(?,?,'manual')
                ON CONFLICT(call_id) DO UPDATE SET contact_id=excluded.contact_id,link_kind='manual' ''', (call_id, contact_id))
        return self.enrich(self.crm_store.get_call(call_id))

    def match_call(self, call_id, phone, name=''):
        phone, name = _text(phone, 100, 'phone'), _text(name, 200, 'name')
        with self._connection(write=True) as connection:
            self._require_call(connection, call_id)
            self._identity(connection, call_id, phone, name)
            # Once attached, the observed identity never changes even if late UI metadata belongs to another call.
            identity = connection.execute('SELECT * FROM call_identity WHERE call_id=?', (call_id,)).fetchone()
            normalized, observed_name = identity['phone_normalized'], identity['display_name']
            existing_link = connection.execute('SELECT 1 FROM call_contacts WHERE call_id=?', (call_id,)).fetchone()
            if not existing_link:
                match = self._match(connection, normalized)
                contact_id = match['contact']['id'] if match['state'] == 'matched' else ''
                if match['state'] == 'none' and normalized and observed_name:
                    contact_id = self._new_contact(connection, observed_name, [normalized], 'iphone')
                if contact_id:
                    connection.execute("INSERT INTO call_contacts(call_id,contact_id,link_kind) VALUES(?,?,'auto')", (call_id, contact_id))
        return self.enrich(self.crm_store.get_call(call_id))
