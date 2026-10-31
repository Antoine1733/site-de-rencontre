"""Exercise the real routes against an isolated, in-memory SQL database."""
import sqlite3
import unittest
from unittest.mock import patch

import app as module


class MemoryDatabase:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = lambda cursor, row: dict(
            zip((column[0] for column in cursor.description), row)
        )
        self.connection.executescript("""
            CREATE TABLE profiles (
                id TEXT PRIMARY KEY, first_name TEXT, age INTEGER,
                bio TEXT, photo_paths TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE swipes (
                actor_id TEXT, target_id TEXT, liked BOOLEAN,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(actor_id, target_id)
            );
            CREATE TABLE matches (
                id TEXT DEFAULT 'test-match', user_a TEXT, user_b TEXT,
                UNIQUE(user_a, user_b)
            );
            INSERT INTO profiles(id,first_name,age,bio,photo_paths)
                VALUES ('alice','Alice',25,'Hello','[]'),
                       ('bob','Bob',26,'Hello','[]');
        """)

    def execute(self, query, parameters=()):
        # Adapt only PostgreSQL namespace, placeholders and UUID pair ordering.
        query = query.replace("public.", "").replace("::uuid", "")
        query = query.replace("%s", "?").replace("LEAST(", "MIN(")
        query = query.replace("GREATEST(", "MAX(")
        return self.connection.execute(query, parameters)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.connection.commit()


class LikeReconsiderationTests(unittest.TestCase):
    def setUp(self):
        self.db = MemoryDatabase()
        self.addCleanup(self.db.connection.close)
        self.actor = "alice"
        self.client = module.app.test_client()
        patches = [
            patch.object(module, "get_db", return_value=self.db),
            patch.object(module, "require_user", side_effect=lambda: {
                "id": self.actor, "first_name": self.actor.title()
            }),
            patch.object(module, "as_json_many", side_effect=lambda rows: rows),
            patch.object(module, "send_push"),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def swipe(self, target, liked):
        response = self.client.post('/api/swipes', json={
            'target_id': target, 'liked': liked
        })
        self.assertEqual(response.status_code, 200)
        return response.get_json()['match']

    def ids(self, endpoint):
        response = self.client.get(endpoint)
        self.assertEqual(response.status_code, 200)
        return [row['id'] for row in response.get_json()]

    def test_passed_profile_stays_hidden_when_it_likes_me_later(self):
        self.assertIsNone(self.swipe('bob', False))
        self.actor = 'bob'
        self.assertIn('alice', self.ids('/api/profiles'))
        self.assertIsNone(self.swipe('alice', True))
        self.actor = 'alice'
        self.assertEqual(self.ids('/api/likes'), [])
        self.assertNotIn('bob', self.ids('/api/profiles'))

    def test_passing_incoming_like_removes_it_persistently(self):
        self.actor = 'bob'
        self.swipe('alice', True)
        self.actor = 'alice'
        self.assertEqual(self.ids('/api/likes'), ['bob'])
        self.assertIsNone(self.swipe('bob', False))
        for _ in range(2):
            self.assertEqual(self.ids('/api/likes'), [])
            self.assertNotIn('bob', self.ids('/api/profiles'))
        self.assertIsNone(self.swipe('bob', False))
        self.assertEqual(self.ids('/api/likes'), [])
        self.assertEqual(self.db.execute('SELECT * FROM matches').fetchall(), [])

    def test_incoming_like_without_prior_swipe_can_be_accepted(self):
        self.actor = 'bob'
        self.swipe('alice', True)
        self.actor = 'alice'
        self.assertEqual(self.ids('/api/likes'), ['bob'])
        match = self.swipe('bob', True)
        self.assertIsNotNone(match)
        self.assertEqual(self.ids('/api/likes'), [])
        self.assertEqual(self.swipe('bob', True), match)
        self.assertEqual(len(self.db.execute('SELECT * FROM matches').fetchall()), 1)

    def test_pass_alone_is_not_an_incoming_like(self):
        self.actor = 'bob'
        self.swipe('alice', False)
        self.actor = 'alice'
        self.assertEqual(self.ids('/api/likes'), [])


if __name__ == '__main__':
    unittest.main()
