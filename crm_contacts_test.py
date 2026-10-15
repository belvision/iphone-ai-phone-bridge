"""Offline address-book normalization, import and conservative identity matching."""
from pathlib import Path
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

from crm_contacts import ContactsStore, normalize_phone, parse_vcard, MAX_VCARD_BYTES
from crm_store import CrmStore


class ContactsTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.crm = CrmStore(Path(self.folder.name) / 'crm.sqlite3')
        self.store = ContactsStore(self.crm)

    def test_conservative_normalization_does_not_guess_country_or_last_digits(self):
        for phone in ('+375 (29) 123-45-67', '375291234567', '80291234567', '0291234567',
                      '291234567', '00375291234567', 'tel:+375291234567'):
            self.assertEqual(normalize_phone(phone), '+375291234567')
        self.assertEqual(normalize_phone('00 1 (415) 555-2671'), '+14155552671')
        for phone in ('1234567', '14155552671', '+37529123', '+375029123456',
                      '+375291234567;ext=99', 'номер +375291234567', '++375291234567', 375291234567):
            self.assertEqual(normalize_phone(phone), '')

    def test_import_repeated_multiple_phones_and_same_name_distinct_people(self):
        self.assertFalse(self.crm.path.exists())
        items = [{'name': 'Иван', 'phones': ['291234567', '+375291234567', '331234567']},
                 {'name': 'Иван', 'phones': ['291234568']}, {'name': 'Без номера', 'phones': ['12345']}]
        self.assertEqual(self.store.import_contacts(items), {'added': 2, 'updated': 0, 'skipped': 1})
        self.assertEqual(self.store.import_contacts(items), {'added': 0, 'updated': 0, 'skipped': 3})
        self.assertEqual(self.store.search()['total'], 2)
        first = self.store.match('291234567')['contact']
        self.assertEqual(first['phones'], ['+375291234567', '+375331234567'])
        self.assertEqual(self.store.import_contacts([{'name': 'Иван', 'phones': ['291234567', '441234567']}]),
                         {'added': 0, 'updated': 1, 'skipped': 0})
        reopened = ContactsStore(CrmStore(self.crm.path))
        self.assertEqual(reopened.match('441234567')['contact']['id'], first['id'])

    def test_shared_numbers_are_ambiguous_and_never_arbitrarily_merged(self):
        items = [{'name': 'Иван', 'phones': ['291234567']}, {'name': 'Мария', 'phones': ['291234567']}]
        self.assertEqual(self.store.import_contacts(items)['added'], 2)
        self.assertEqual(self.store.match('291234567'), {'state': 'ambiguous', 'contact': None})
        self.assertEqual(self.store.import_contacts(items), {'added': 0, 'updated': 0, 'skipped': 2})
        call_id = self.crm.create_call('agents')
        record = self.store.match_call(call_id, '291234567', 'Иван')
        self.assertEqual(record['contact_id'], '')
        self.assertEqual(self.store.search()['total'], 2)

    def test_match_immutable_identity_keeps_confirmed_callback_phone_separate(self):
        self.store.import_contacts([{'name': 'Иван', 'phones': ['291234567']}])
        call_id = self.crm.create_call('agents')
        self.crm.update_call(call_id, {'name': 'Иван Петров', 'phone': '+375331234567'})
        first = self.store.match_call(call_id, '291234567', 'Входящий Иван')
        second = self.store.match_call(call_id, '441234567', 'Другой человек')
        self.assertEqual(first['contact_id'], second['contact_id'])
        self.assertEqual(second['call_number'], '+375291234567')
        self.assertEqual(second['call_display_name'], 'Входящий Иван')
        self.assertEqual(second['phone'], '+375331234567')
        self.assertEqual(second['name'], 'Иван Петров')
        self.assertEqual(second['contact_name'], 'Иван')
        self.assertEqual(self.store.search()['total'], 1)

    def test_later_shared_number_invalidates_auto_link_but_manual_choice_is_preserved(self):
        self.store.import_contacts([{'name': 'Иван', 'phones': ['291234567']}])
        call_id = self.crm.create_call('agents')
        first = self.store.match_call(call_id, '291234567', 'Иван')
        self.assertTrue(first['contact_id'])
        self.store.import_contacts([{'name': 'Мария', 'phones': ['291234567']}])
        self.assertEqual(self.store.enrich(self.crm.get_call(call_id))['contact_id'], '')
        chosen = self.store.link_call(call_id, first['contact_id'])
        self.assertEqual(chosen['contact_name'], 'Иван')
        self.assertEqual(self.store.match_call(call_id, '291234567', 'Иван')['contact_id'], first['contact_id'])

    def test_observed_phone_creates_contact_only_with_valid_number_and_name(self):
        for phone, name in [('', 'Незнакомый'), ('Скрытый номер', 'Незнакомый'), ('291234567', '')]:
            record = self.store.match_call(self.crm.create_call('agents'), phone, name)
            self.assertEqual(record['contact_id'], '')
        self.assertEqual(self.store.search()['total'], 0)
        call_id = self.crm.create_call('custom')
        first = self.store.match_call(call_id, '291234567', 'Пётр')
        self.assertEqual(first['contact_name'], 'Пётр')
        self.assertEqual(self.store.match_call(call_id, '291234567', 'Пётр')['contact_id'], first['contact_id'])
        self.assertEqual(self.store.search()['total'], 1)
        with self.assertRaises(KeyError):
            self.store.link_call('missing', first['contact_id'])
        with self.assertRaises(KeyError):
            self.store.link_call(call_id, 'missing')

    def test_russian_search_literal_sql_wildcards_and_pagination(self):
        self.store.import_contacts([{'name': "ПЁТР'; DROP TABLE contacts;--", 'phones': ['291234567']},
                                    {'name': 'Имя 100%', 'phones': ['291234568']},
                                    {'name': 'Имя 1000', 'phones': ['291234569']}])
        self.assertEqual(self.store.search('пётр')['total'], 1)
        self.assertEqual(self.store.search('100%')['total'], 1)
        self.assertEqual(self.store.search('1234568')['total'], 1)
        self.assertEqual(self.store.search(limit=1, offset=1)['total'], 3)
        self.assertEqual(len(self.store.search(limit=1, offset=1)['items']), 1)
        self.assertEqual(self.store.match('1234568')['state'], 'none')

    def test_concurrent_repeated_import_does_not_duplicate_contact(self):
        self.store.initialize()
        item = [{'name': 'Иван', 'phones': ['291234567']}]
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: ContactsStore(CrmStore(self.crm.path)).import_contacts(item), range(2)))
        self.assertEqual(sum(result['added'] for result in results), 1)
        self.assertEqual(self.store.search()['total'], 1)

    def test_vcard_folding_escapes_structured_names_uris_and_unrelated_data(self):
        text = ('BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Иван\\, Петров\r\n'
                'TEL;TYPE=CELL:+375 29 123-\r\n 45-67\r\nEMAIL:ignored@example.org\r\n'
                'NOTE:Do not import\r\nEND:VCARD\r\nBEGIN:VCARD\nVERSION:4.0\n'
                'N:Иванова;Мария;Сергеевна;;\nitem1.TEL;VALUE=uri:tel:+375331234567\nEND:VCARD\n')
        items = parse_vcard(text)
        self.assertEqual(items, [{'name': 'Иван, Петров', 'phones': ['+375 29 123-45-67']},
                                 {'name': 'Мария Сергеевна Иванова', 'phones': ['tel:+375331234567']}])
        self.assertEqual(self.store.import_contacts(items, 'vcard')['added'], 2)
        with self.assertRaises(ValueError):
            parse_vcard('BEGIN:VCARD\nVERSION:3.0\nFN:Оборванный файл')
        with self.assertRaises(ValueError):
            parse_vcard('x' * (MAX_VCARD_BYTES + 1))
        for invalid in ('', 'not a vCard', 'BEGIN:VCARD\nVERSION:2.1\nFN:Unsupported\nEND:VCARD'):
            with self.assertRaises(ValueError):
                parse_vcard(invalid)


if __name__ == '__main__':
    unittest.main(verbosity=2)
