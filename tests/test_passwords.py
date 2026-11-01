import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from werkzeug.security import check_password_hash, generate_password_hash
import app as module
import generate_temporary_passwords as migration


class Database:
    def __init__(self):
        self.connection = sqlite3.connect(':memory:')
        self.connection.row_factory = lambda cursor, row: dict(zip((c[0] for c in cursor.description), row))
        self.connection.execute('''CREATE TABLE profiles (
            id TEXT PRIMARY KEY, email TEXT UNIQUE, first_name TEXT, age INTEGER,
            bio TEXT, photo_paths TEXT, password_hash TEXT,
            password_change_required BOOLEAN DEFAULT false, auth_version INTEGER DEFAULT 1,
            login_failures INTEGER DEFAULT 0, login_locked_until REAL DEFAULT 0)''')
        self.execute("INSERT INTO profiles(id,email,first_name,age,bio,photo_paths) VALUES ('1','alice@example.com','Alice',25,'Hello','[]')")

    def execute(self, query, params=()):
        params = tuple(json.dumps(v) if isinstance(v, list) else v for v in params)
        return self.connection.execute(query.replace('public.', '').replace('%s', '?').replace(' FOR UPDATE', ''), params)

    def __enter__(self):
        return self

    def __exit__(self, kind, *_):
        self.connection.rollback() if kind else self.connection.commit()


class PasswordTests(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.addCleanup(self.db.connection.close)
        for item in (patch.object(module, 'get_db', return_value=self.db),
                     patch.object(migration, 'get_db', return_value=self.db),
                     patch.object(module, 'as_json', side_effect=dict),
                     patch.object(module, 'upload_photo', return_value='photo')):
            item.start()
            self.addCleanup(item.stop)
        self.client = module.app.test_client()

    def row(self):
        return self.db.execute("SELECT * FROM profiles WHERE id='1'").fetchone()

    def login(self, password=None, **extra):
        return self.client.post('/api/login', json={'email': 'alice@example.com', 'password': password, **extra})

    def test_legacy_session_and_passwordless_login_rejected(self):
        with self.client.session_transaction() as session:
            session['email'] = 'alice@example.com'
        self.assertIsNone(self.client.get('/api/me').get_json())
        self.assertEqual(self.login().status_code, 401)
        self.assertEqual(self.login('some password').status_code, 401)
        self.assertEqual(self.client.get('/api/profiles').status_code, 401)

    def test_migration_and_required_replacement(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            output = Path(directory) / 'passwords.json'
            self.assertEqual(migration.generate(output), 1)
            password = json.loads(output.read_text(encoding='utf-8'))[0]['mot_de_passe_temporaire']
            self.assertTrue(check_password_hash(self.row()['password_hash'], password))
            self.assertNotEqual(self.row()['password_hash'], password)
            self.assertEqual(migration.generate(Path(directory) / 'second.json'), 0)
            with self.assertRaises(FileExistsError):
                migration.generate(output)
        self.assertEqual(self.login(password).get_json(), {'password_change_required': True})
        self.assertIsNone(self.client.get('/api/me').get_json())
        self.assertEqual(self.login(password, new_password=password).status_code, 400)
        self.assertEqual(self.login(password, new_password='short').status_code, 400)
        result = self.login(password, new_password='My personal password!')
        self.assertEqual(result.status_code, 200)
        self.assertNotIn('password_hash', result.get_json())
        self.assertEqual(self.client.get('/api/me').status_code, 200)
        self.client.post('/api/logout')
        self.assertEqual(self.login(password).status_code, 401)
        self.assertEqual(self.login('My personal password!').status_code, 200)

    def test_failed_attempts_are_limited(self):
        self.db.execute("UPDATE profiles SET password_hash=%s WHERE id='1'", (generate_password_hash('correct password'),))
        for _ in range(5):
            self.assertEqual(self.login('wrong password').status_code, 401)
        self.assertEqual(self.login('correct password').status_code, 429)
        self.db.execute("UPDATE profiles SET login_locked_until=0 WHERE id='1'")
        self.assertEqual(self.login('correct password').status_code, 200)

    def test_signup_requires_and_hashes_password(self):
        payload = {'email': 'bob@example.com', 'first_name': 'Bob', 'age': 25,
                   'bio': 'Hello', 'photo_paths': ['data:image/jpeg;base64,test']}
        self.assertEqual(self.client.post('/api/signup', json=payload).status_code, 400)
        payload['password'] = 'My new password!'
        response = self.client.post('/api/signup', json=payload)
        self.assertEqual(response.status_code, 200)
        row = self.db.execute("SELECT * FROM profiles WHERE email='bob@example.com'").fetchone()
        self.assertTrue(check_password_hash(row['password_hash'], payload['password']))
        self.assertNotIn('password_hash', response.get_json())
        self.assertEqual(self.client.get('/api/me').get_json()['email'], 'bob@example.com')

    def change(self, current='correct password', new='My changed password!', confirm='My changed password!'):
        return self.client.put('/api/password', json={
            'current_password': current, 'new_password': new, 'confirm_password': confirm})

    def authenticate(self):
        self.db.execute("UPDATE profiles SET password_hash=%s WHERE id='1'", (generate_password_hash('correct password'),))
        self.assertEqual(self.login('correct password').status_code, 200)

    def test_change_password_invalidates_other_sessions(self):
        self.authenticate()
        other_client = module.app.test_client()
        other_client.post('/api/login', json={'email': 'alice@example.com', 'password': 'correct password'})
        self.assertEqual(self.change().status_code, 200)
        self.assertTrue(check_password_hash(self.row()['password_hash'], 'My changed password!'))
        self.assertIsNotNone(self.client.get('/api/me').get_json())
        self.assertIsNone(other_client.get('/api/me').get_json())
        self.assertEqual(self.login('correct password').status_code, 401)
        self.assertEqual(self.login('My changed password!').status_code, 200)

    def test_change_requires_session_current_password_and_matching_confirmation(self):
        self.assertEqual(self.change().status_code, 401)
        self.authenticate()
        original_hash = self.row()['password_hash']
        self.assertEqual(self.change(current='wrong password').status_code, 403)
        self.assertEqual(self.change(confirm='Something else!').status_code, 400)
        self.assertEqual(self.change(new='short', confirm='short').status_code, 400)
        self.assertEqual(self.change(new='correct password', confirm='correct password').status_code, 400)
        self.assertEqual(self.client.put('/api/password', json=[]).status_code, 400)
        self.assertEqual(self.row()['password_hash'], original_hash)

    def test_change_password_attempts_are_limited(self):
        self.authenticate()
        for _ in range(5):
            self.assertEqual(self.change(current='wrong password').status_code, 403)
        self.assertEqual(self.change().status_code, 429)


if __name__ == '__main__':
    unittest.main()
