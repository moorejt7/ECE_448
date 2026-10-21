import io
import json
import os
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
import app as server


class AppTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.directory.cleanup)
        server.app.config.update(TESTING=True, DATABASE=str(Path(self.directory.name) / 'test.sqlite3'))
        self.client = server.app.test_client()
        self.register('alice')
        self.pet_id = self.add_pet('Fido')

    def token(self, client=None):
        client = client or self.client
        with client.session_transaction() as state:
            return state.get('csrf_token', '')

    def register(self, username, client=None):
        client = client or self.client
        client.get('/register')
        return client.post('/register', data={'username': username, 'password': 'password123',
            'confirm_password': 'password123', 'csrf_token': self.token(client)}, follow_redirects=True)

    def logout(self):
        return self.client.post('/logout', data={'csrf_token': self.token()}, follow_redirects=True)

    def add_pet(self, name, client=None):
        client = client or self.client
        response = client.post('/pets', data={'name': name, 'csrf_token': self.token(client)})
        self.assertEqual(response.status_code, 302)
        return int(response.location.split('pet_id=')[1])

    def upload(self, data=None, pet_id=None, client=None):
        client = client or self.client
        if data is None:
            buffer = io.BytesIO()
            Image.new('RGB', (10, 10)).save(buffer, format='PNG')
            data = buffer.getvalue()
        return client.post('/', data={'image': (io.BytesIO(data), 'photo.png'),
            'pet_id': self.pet_id if pet_id is None else pet_id,
            'csrf_token': self.token(client)}, follow_redirects=True)

    def test_home(self):
        self.assertEqual(self.client.get('/').status_code, 200)

    def test_missing_file(self):
        self.assertEqual(self.client.post('/', data={'csrf_token': self.token()}).status_code, 400)

    def test_invalid_file(self):
        self.assertEqual(self.upload(b'not an image').status_code, 400)

    @patch.dict(os.environ, {'OPENAI_API_KEY': ''})
    def test_blank_key(self):
        response = self.upload()
        self.assertEqual(response.status_code, 503)
        self.assertIn(b'Add OPENAI_API_KEY', response.data)

    @patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'})
    @patch('app.OpenAI')
    def test_success_and_html_escaping(self, mock_openai):
        create = mock_openai.return_value.__enter__.return_value.responses.create
        create.return_value = SimpleNamespace(output_text='<script>alert(1)</script>', status='completed')
        response = self.upload()
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'&lt;script&gt;', response.data)
        call = create.call_args.kwargs
        self.assertEqual(call['input'][0]['content'][0]['text'], server.PROMPT)
        self.assertEqual(json.loads(call['input'][0]['content'][1]['text']), {'previous_results': []})
        self.assertTrue(call['input'][0]['content'][2]['image_url'].startswith('data:image/jpeg;base64,'))
        self.assertFalse(call['store'])

    @patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'})
    @patch('app.OpenAI')
    def test_network_error(self, mock_openai):
        create = mock_openai.return_value.__enter__.return_value.responses.create
        create.side_effect = server.APIConnectionError(request=None)
        response = self.upload()
        self.assertEqual(response.status_code, 502)
        self.assertIn(b'Could not reach OpenAI', response.data)

    def test_oversized_request(self):
        response = self.client.post('/', data=b'x' * (12 * 1024 * 1024), content_type='application/octet-stream')
        self.assertEqual(response.status_code, 413)

    def test_auth_required_and_logout(self):
        self.logout()
        self.assertEqual(self.client.get('/').location, '/login')
        self.assertEqual(self.client.post('/').location, '/login')

    def test_password_hash_and_login(self):
        with server.app.app_context():
            row = server.get_db().execute('SELECT * FROM users').fetchone()
            self.assertNotEqual(row['password_hash'], 'password123')
            self.assertTrue(server.check_password_hash(row['password_hash'], 'password123'))
        self.logout()
        bad = self.client.post('/login', data={'username': 'alice', 'password': 'wrong', 'csrf_token': self.token()})
        self.assertEqual(bad.status_code, 401)
        good = self.client.post('/login', data={'username': 'ALICE', 'password': 'password123', 'csrf_token': self.token()}, follow_redirects=True)
        self.assertIn(b'Signed in as', good.data)

    def test_duplicate_and_invalid_registration(self):
        response = self.register('ALICE')
        self.assertEqual(response.status_code, 400)
        self.assertIn(b'already taken', response.data)
        response = self.register("bad'name")
        self.assertEqual(response.status_code, 400)

    def test_csrf_rejected(self):
        self.assertEqual(self.client.post('/logout').status_code, 400)
        self.assertEqual(self.client.post('/').status_code, 400)
        self.assertEqual(self.client.post('/register', data={'csrf_token': 'wrong'}).status_code, 400)

    @patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'})
    @patch('app.OpenAI')
    def test_history_persistence_order_limit_and_isolation(self, mock_openai):
        create = mock_openai.return_value.__enter__.return_value.responses.create
        for number in range(4):
            create.return_value = SimpleNamespace(output_text=f'Result-{number}', status='completed')
            self.assertEqual(self.upload().status_code, 200)
        self.logout()
        self.client.post('/login', data={'username': 'alice', 'password': 'password123', 'csrf_token': self.token()})
        page = self.client.get('/').data
        self.assertNotIn(b'Result-0', page)
        self.assertLess(page.index(b'Result-3'), page.index(b'Result-2'))
        self.assertLess(page.index(b'Result-2'), page.index(b'Result-1'))
        with server.app.app_context():
            self.assertEqual(server.get_db().execute('SELECT count(*) FROM responses').fetchone()[0], 4)
        other = server.app.test_client()
        other_page = self.register('bob', other)
        self.assertNotIn(b'Result-', other_page.data)
        self.assertIn(b'Add your first pet', other_page.data)

    @patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'})
    @patch('app.OpenAI')
    def test_pet_history_isolation_and_delete(self, mock_openai):
        create = mock_openai.return_value.__enter__.return_value.responses.create
        create.return_value = SimpleNamespace(output_text='Fido only', status='completed')
        self.upload()
        cat_id = self.add_pet('Mittens')
        create.return_value = SimpleNamespace(output_text='Mittens only', status='completed')
        self.upload(pet_id=cat_id)
        cat_page = self.client.get(f'/?pet_id={cat_id}').data
        self.assertIn(b'Mittens only', cat_page)
        self.assertNotIn(b'Fido only', cat_page)
        dog_page = self.client.get(f'/?pet_id={self.pet_id}').data
        self.assertIn(b'Fido only', dog_page)
        self.assertNotIn(b'Mittens only', dog_page)
        response = self.client.post(f'/pets/{cat_id}/delete', data={'csrf_token': self.token()}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        with server.app.app_context():
            self.assertEqual(server.get_db().execute('SELECT count(*) FROM responses').fetchone()[0], 1)

    @patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'})
    @patch('app.OpenAI')
    def test_cannot_use_another_users_pet(self, mock_openai):
        other = server.app.test_client()
        self.register('bob', other)
        other_pet = self.add_pet('Other', other)
        self.assertEqual(self.upload(pet_id=other_pet).status_code, 400)
        self.assertEqual(self.client.post(f'/pets/{other_pet}/delete',
            data={'csrf_token': self.token()}).status_code, 404)
        mock_openai.assert_not_called()

    def test_legacy_history_migration(self):
        with server.app.app_context():
            db = server.get_db()
            with db:
                db.execute('INSERT INTO responses (user_id, answer) VALUES (1, ?)', ('Legacy answer',))
        page = self.client.get('/').data
        self.assertIn(b'Previous uploads', page)
        with server.app.app_context():
            row = server.get_db().execute('SELECT pet_id FROM responses WHERE answer = ?', ('Legacy answer',)).fetchone()
            self.assertIsNotNone(row['pet_id'])
        legacy_page = self.client.get(f'/?pet_id={row["pet_id"]}').data
        self.assertIn(b'Legacy answer', legacy_page)

    @patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'})
    @patch('app.OpenAI')
    def test_last_30_json_only_with_timestamps_and_pet_isolation(self, mock_openai):
        other_pet = self.add_pet('Mittens')
        with server.app.app_context():
            db = server.get_db()
            with db:
                for number in range(32):
                    answer = f'Explanation {number}\n```json\n{{"disease": "item-{number}"}}\n```'
                    db.execute('INSERT INTO responses (user_id, pet_id, answer, json_data, created_at) '
                               'VALUES (?, ?, ?, ?, ?)',
                               (1, self.pet_id, answer, server.extract_json(answer), '2026-10-13 10:00:00'))
                db.execute('INSERT INTO responses (user_id, pet_id, answer, json_data) VALUES (?, ?, ?, ?)',
                           (1, other_pet, '{"disease":"other-pet"}', '{"disease":"other-pet"}'))
        create = mock_openai.return_value.__enter__.return_value.responses.create
        create.return_value = SimpleNamespace(output_text='Current result\n{"disease":"new"}', status='completed')
        self.upload(pet_id=self.pet_id)
        content = create.call_args.kwargs['input'][0]['content']
        context = json.loads(content[1]['text'])['previous_results']
        self.assertEqual(len(context), 30)
        self.assertEqual(context[0]['result'], {'disease': 'item-2'})
        self.assertEqual(context[-1]['result'], {'disease': 'item-31'})
        self.assertEqual(context[0]['timestamp_utc'], '2026-10-13T10:00:00Z')
        self.assertNotIn('Explanation', content[1]['text'])
        self.assertNotIn('other-pet', content[1]['text'])
        with server.app.app_context():
            row = server.get_db().execute('SELECT json_data FROM responses ORDER BY id DESC LIMIT 1').fetchone()
            self.assertEqual(json.loads(row['json_data']), {'disease': 'new'})

    def test_old_answers_backfill_only_valid_json(self):
        with server.app.app_context():
            db = server.get_db()
            with db:
                db.execute('INSERT INTO responses (user_id, pet_id, answer) VALUES (?, ?, ?)',
                           (1, self.pet_id, 'Text before\n```json\n{"disease": "old"}\n```'))
                db.execute('INSERT INTO responses (user_id, pet_id, answer) VALUES (?, ?, ?)',
                           (1, self.pet_id, 'No JSON here'))
        with server.app.app_context():
            db = server.get_db()
            context = json.loads(server.recent_json_context(db, 1, self.pet_id))['previous_results']
            self.assertEqual(len(context), 1)
            self.assertEqual(context[0]['result'], {'disease': 'old'})
            values = [row[0] for row in db.execute('SELECT json_data FROM responses ORDER BY id')]
            self.assertEqual(values, ['{"disease":"old"}', ''])


if __name__ == '__main__':
    unittest.main()
