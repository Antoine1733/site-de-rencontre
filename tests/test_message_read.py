import unittest
from unittest.mock import patch

import app as module
from test_like_reconsideration import MemoryDatabase

MATCH = '00000000-0000-0000-0000-000000000001'
RECEIVED = '00000000-0000-0000-0000-000000000002'
SENT = '00000000-0000-0000-0000-000000000003'
NEW = '00000000-0000-0000-0000-000000000004'


class MessageReadTests(unittest.TestCase):
    def setUp(self):
        self.db = MemoryDatabase()
        self.addCleanup(self.db.connection.close)
        self.db.connection.executescript('''
            ALTER TABLE matches ADD COLUMN created_at TEXT;
            CREATE TABLE messages (
                id TEXT PRIMARY KEY, match_id TEXT, sender_id TEXT,
                body TEXT, created_at TEXT, read_at TEXT
            );
        ''')
        self.db.execute('INSERT INTO matches(id,user_a,user_b) VALUES (%s,%s,%s)', (MATCH, 'alice', 'bob'))
        for message, sender in [(RECEIVED, 'bob'), (SENT, 'alice')]:
            self.db.execute('INSERT INTO messages(id,match_id,sender_id,body) VALUES (%s,%s,%s,%s)', (message, MATCH, sender, 'Hello'))
        self.actor = 'alice'
        for mock in [patch.object(module, 'get_db', return_value=self.db),
                     patch.object(module, 'require_user', side_effect=lambda: {'id': self.actor}),
                     patch.object(module, 'as_json_many', side_effect=lambda rows: rows),
                     patch.object(module, 'as_json', side_effect=lambda row: row)]:
            mock.start()
            self.addCleanup(mock.stop)
        self.client = module.app.test_client()

    def unread(self):
        response = self.client.get('/api/matches')
        self.assertEqual(response.status_code, 200)
        return response.get_json()[0]['unread']

    def read(self, ids):
        return self.client.post(f'/api/matches/{MATCH}/read', json={'message_ids': ids})

    def test_read_is_persistent_and_only_marks_received_messages(self):
        self.assertTrue(self.unread())
        self.assertEqual(self.client.get(f'/api/matches/{MATCH}/messages').status_code, 200)
        self.assertTrue(self.unread())  # Fetching alone does not acknowledge reading.
        self.assertEqual(self.read([RECEIVED, SENT]).status_code, 200)
        self.assertFalse(self.unread())
        self.assertFalse(self.unread())
        self.assertIsNone(self.db.execute('SELECT read_at FROM messages WHERE id=%s', (SENT,)).fetchone()['read_at'])
        self.actor = 'bob'
        self.assertTrue(self.unread())

    def test_message_arriving_after_fetch_stays_unread(self):
        self.db.execute('INSERT INTO messages(id,match_id,sender_id,body) VALUES (%s,%s,%s,%s)', (NEW, MATCH, 'bob', 'New'))
        self.assertEqual(self.read([RECEIVED]).status_code, 200)
        self.assertTrue(self.unread())
        self.assertEqual(self.read([NEW]).status_code, 200)
        self.assertFalse(self.unread())

    def test_non_member_cannot_mark_read(self):
        self.actor = 'outsider'
        self.assertEqual(self.read([RECEIVED]).status_code, 403)
        self.actor = 'alice'
        self.assertTrue(self.unread())

    def test_invalid_ids_rejected(self):
        self.assertEqual(self.read(['invalid']).status_code, 400)
        self.assertTrue(self.unread())
