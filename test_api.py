import io
import json
import os
import re
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
from werkzeug.serving import make_server
import app as server
import test_app as fixtures
from raspberry_pi import pi_upload


class ApiTests(unittest.TestCase):
    setUp = fixtures.AppTests.setUp
    register = fixtures.AppTests.register
    token = fixtures.AppTests.token
    add_pet = fixtures.AppTests.add_pet
    upload = fixtures.AppTests.upload

    def device(self, pet_id=None):
        pet_id = self.pet_id if pet_id is None else pet_id
        response = self.client.post(f'/pets/{pet_id}/tokens', data={
            'csrf_token': self.token(), 'password': 'password12345', 'name': 'Pi', 'days': '30'})
        self.assertEqual(response.status_code, 200)
        return re.search(rb'pet_[A-Za-z0-9_-]{43}', response.data)[0].decode()

    def api_upload(self, credential, pet_id=None, data=None):
        if data is None:
            buffer = io.BytesIO()
            Image.new('RGB', (10, 10)).save(buffer, format='PNG')
            data = buffer.getvalue()
        form = {'image': (io.BytesIO(data), 'image.png')}
        if pet_id is not None:
            form['pet_id'] = str(pet_id)
        response = self.client.post('/api/images', data=form, headers={'Authorization': 'Bearer ' + credential})
        self.addCleanup(response.close)
        self.addCleanup(response.request.close)
        self.addCleanup(response.request.environ['wsgi.input'].close)
        return response

    @patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'})
    @patch('app.OpenAI')
    def test_api_returns_plain_string_and_uses_private_history(self, mock_openai):
        credential = self.device()
        other = self.add_pet('Other')
        with server.app.app_context():
            db = server.get_db()
            with db:
                db.execute('INSERT INTO responses (user_id, pet_id, answer, json_data) VALUES (1, ?, ?, ?)',
                           (self.pet_id, 'Past explanation {"result":"prior"}', '{"result":"prior"}'))
                db.execute('INSERT INTO responses (user_id, pet_id, answer, json_data) VALUES (1, ?, ?, ?)',
                           (other, 'Other pet', '{"result":"other"}'))
        create = mock_openai.return_value.__enter__.return_value.responses.create
        create.return_value = SimpleNamespace(output_text='Analysis string\n{"result":"current"}', status='completed')
        response = self.api_upload(credential)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'text/plain')
        self.assertEqual(response.text, 'Analysis string\n{"result":"current"}')
        self.assertIn('X-Response-ID', response.headers)
        context = json.loads(create.call_args.kwargs['input'][0]['content'][1]['text'])
        self.assertEqual(context['previous_results'][0]['result'], {'result': 'prior'})
        self.assertNotIn('Other pet', str(context))
        page = self.client.get(f'/?pet_id={self.pet_id}')
        self.assertIn(b'Analysis string', page.data)

    def test_cookie_is_not_api_authentication(self):
        response = self.client.get('/api/pets')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.mimetype, 'text/plain')
        self.assertNotIn('Location', response.headers)
        self.assertEqual(self.client.get('/api/not-found').mimetype, 'text/plain')

    def test_device_hash_and_show_once(self):
        credential = self.device()
        self.assertNotIn(credential.encode(), self.client.get('/').data)
        with server.app.app_context():
            row = server.get_db().execute('SELECT token_hash FROM device_tokens').fetchone()
            self.assertNotEqual(row['token_hash'], credential)
            self.assertEqual(len(row['token_hash']), 64)

    @patch('app.OpenAI')
    def test_token_is_scoped_expiring_and_revocable(self, mock_openai):
        credential = self.device()
        other_pet = self.add_pet('Mittens')
        self.assertEqual(self.api_upload(credential, other_pet).status_code, 403)
        with server.app.app_context():
            db = server.get_db()
            token_id = db.execute('SELECT id FROM device_tokens').fetchone()[0]
            with db:
                db.execute('UPDATE device_tokens SET expires_at = ?', (int(time.time()) - 1,))
        self.assertEqual(self.api_upload(credential).status_code, 401)
        self.client.post(f'/devices/{token_id}/revoke', data={'csrf_token': self.token()})
        mock_openai.assert_not_called()
        credential = self.device()
        self.assertEqual(self.client.post('/api/logout', headers={'Authorization': 'Bearer ' + credential}).status_code, 200)
        self.assertEqual(self.api_upload(credential).status_code, 401)

    def test_login_scopes_credentials_and_does_not_use_cookies(self):
        response = self.client.post('/api/login', json={
            'username': 'alice', 'password': 'password12345', 'pet_name': 'Fido'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['pet_id'], self.pet_id)
        self.assertLessEqual(response.json['expires_at'] - time.time(), 3600)
        self.assertNotIn('Set-Cookie', response.headers)
        other = server.app.test_client()
        self.register('bob', other)
        foreign_pet = self.add_pet('Other', other)
        response = self.client.post('/api/login', json={
            'username': 'alice', 'password': 'password12345', 'pet_id': foreign_pet})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.client.post('/api/login', json={'username': 'alice', 'password': 'wrong', 'pet_id': self.pet_id}).status_code, 401)
        self.assertEqual(self.client.post('/api/login', data={'username': 'alice'}).status_code, 415)

    def test_browser_cannot_create_device_without_password_and_csrf(self):
        self.assertEqual(self.client.post(f'/pets/{self.pet_id}/tokens').status_code, 400)
        response = self.client.post(f'/pets/{self.pet_id}/tokens', data={
            'csrf_token': self.token(), 'password': 'wrong', 'name': 'Pi', 'days': '30'})
        self.assertEqual(response.status_code, 401)
        with server.app.app_context():
            self.assertEqual(server.get_db().execute('SELECT count(*) FROM device_tokens').fetchone()[0], 0)

    def test_cross_user_cannot_revoke_and_pet_removal_revokes(self):
        credential = self.device()
        with server.app.app_context():
            token_id = server.get_db().execute('SELECT id FROM device_tokens').fetchone()[0]
        other = server.app.test_client()
        self.register('bob', other)
        self.assertEqual(other.post(f'/devices/{token_id}/revoke', data={'csrf_token': self.token(other)}).status_code, 404)
        self.client.post(f'/pets/{self.pet_id}/delete', data={'csrf_token': self.token()})
        self.assertEqual(self.client.get('/api/pets', headers={'Authorization': 'Bearer ' + credential}).status_code, 401)

    def test_api_requires_https_in_production_and_blocks_origins(self):
        with patch.dict(server.app.config, {'PRODUCTION': True}):
            response = self.client.post('/api/login', json={})
            self.assertEqual(response.status_code, 400)
            self.assertNotIn('Location', response.headers)
        response = self.client.post('/api/login', json={}, headers={'Origin': 'https://evil.example'})
        self.assertEqual(response.status_code, 403)

    @patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'})
    @patch('app.OpenAI')
    def test_browser_and_api_share_quota(self, mock_openai):
        credential = self.device()
        create = mock_openai.return_value.__enter__.return_value.responses.create
        create.return_value = SimpleNamespace(output_text='{}', status='completed')
        with patch.dict(server.app.config, {'RATE_LIMIT_ENABLED': True, 'IMAGE_DAILY_LIMIT': 1}):
            self.assertEqual(self.upload().status_code, 200)
            self.assertEqual(self.api_upload(credential).status_code, 429)
        self.assertEqual(create.call_count, 1)

    def test_invalid_image_and_oversized_api_errors_are_plain_text(self):
        credential = self.device()
        response = self.api_upload(credential, data=b'not an image')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.mimetype, 'text/plain')
        response = self.api_upload(credential, data=b'x' * (12 * 1024 * 1024))
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.mimetype, 'text/plain')

    @patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'})
    @patch('app.OpenAI')
    def test_actual_pi_client_multipart(self, mock_openai):
        credential = self.device()
        create = mock_openai.return_value.__enter__.return_value.responses.create
        create.return_value = SimpleNamespace(output_text='Pi received this string.', status='completed')
        image_path = Path(self.directory.name) / 'photo.png'
        Image.new('RGB', (10, 10)).save(image_path)
        httpd = make_server('127.0.0.1', 0, server.app)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            answer = pi_upload.upload_image(f'http://127.0.0.1:{httpd.server_port}', credential, image_path)
            self.assertEqual(answer, 'Pi received this string.')
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)


class PiClientTests(unittest.TestCase):
    def test_remote_http_and_credentials_in_url_rejected(self):
        for url in ('http://pets.example', 'https://user:pass@pets.example', 'https://pets.example/?key=secret'):
            with self.assertRaises(pi_upload.UploadError):
                pi_upload.validate_server(url)
        self.assertEqual(pi_upload.validate_server('https://pets.example/'), 'https://pets.example')

    def test_redirects_are_not_followed(self):
        attempts = []
        def redirect_server(environ, start_response):
            attempts.append(environ['PATH_INFO'])
            start_response('302 Found', [('Location', '/leak')])
            return [b'']
        httpd = make_server('127.0.0.1', 0, redirect_server)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaises(pi_upload.UploadError):
                pi_upload.api_request(f'http://127.0.0.1:{httpd.server_port}', '/api/images', token='test-only', body=b'')
            self.assertEqual(attempts, ['/api/images'])
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_interactive_client_revokes_temporary_credential(self):
        import tempfile
        credential = 'pet_' + 'a' * 43
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            image = Path(directory) / 'image.jpg'
            image.write_bytes(b'fake image bytes for mocked server')
            replies = [json.dumps({'access_token': credential}), 'Simple analysis string', 'Credential revoked.']
            output = io.StringIO()
            with patch.object(pi_upload, 'api_request', side_effect=replies) as request_call, \
                 patch.object(pi_upload.getpass, 'getpass', return_value='test-password'), \
                 patch('sys.stdout', output):
                result = pi_upload.main([str(image), '--server', 'https://pets.example', '--username', 'alice', '--pet-id', '1'])
            self.assertEqual(result, 0)
            self.assertEqual(output.getvalue(), 'Simple analysis string\n')
            self.assertEqual(request_call.call_args_list[-1].args[1], '/api/logout')
            self.assertNotIn(credential, output.getvalue())

    def test_capture_client_removes_temporary_photo(self):
        import tempfile
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            token_file = Path(directory) / 'device-token'
            token_file.write_text('pet_' + 'a' * 43)
            token_file.chmod(0o600)
            captured = []
            def capture(command, **kwargs):
                photo = Path(command[-1])
                photo.write_bytes(b'fake photo for mocked server')
                captured.append(photo)
            with patch.object(pi_upload.subprocess, 'run', side_effect=capture), \
                 patch.object(pi_upload, 'api_request', return_value='Camera result'), patch('sys.stdout', io.StringIO()):
                result = pi_upload.main(['--capture', '--server', 'https://pets.example', '--token-file', str(token_file)])
            self.assertEqual(result, 0)
            self.assertFalse(captured[0].exists())
