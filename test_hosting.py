"""Exercise the real production server on Linux CI, without any API requests."""
import json
import os
import secrets
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path


@unittest.skipIf(os.name == 'nt', 'Gunicorn runs on Linux; verified by GitHub CI.')
class HostingTests(unittest.TestCase):
    def test_gunicorn_health_secure_cookie_and_https_redirect(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                port = listener.getsockname()[1]
            env = os.environ | {
                'APP_ENV': 'production', 'RAILWAY_VOLUME_MOUNT_PATH': directory,
                'APP_DATA_DIR': directory, 'SECRET_KEY': secrets.token_urlsafe(64),
                'REGISTRATION_CODE': secrets.token_urlsafe(32), 'PORT': str(port),
                'RAILWAY_PUBLIC_DOMAIN': 'example.up.railway.app',
            }
            from cryptography.fernet import Fernet
            env['DATA_ENCRYPTION_KEY'] = Fernet.generate_key().decode()
            process = subprocess.Popen(['gunicorn', '--config', 'gunicorn.conf.py', 'app:app'],
                                       env=env, cwd=Path(__file__).parent,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                for _ in range(100):
                    try:
                        request = urllib.request.Request(f'http://127.0.0.1:{port}/health',
                                                          headers={'Host': 'healthcheck.railway.app'})
                        with urllib.request.urlopen(request, timeout=1) as response:
                            self.assertEqual(json.load(response), {'status': 'ok'})
                        break
                    except urllib.error.URLError:
                        if process.poll() is not None:
                            self.fail('Gunicorn failed to start: ' + process.stderr.read().decode())
                        time.sleep(0.1)
                else:
                    self.fail('Production healthcheck did not become ready.')
                request = urllib.request.Request(f'http://127.0.0.1:{port}/login',
                    headers={'Host': 'example.up.railway.app', 'X-Forwarded-Proto': 'https'})
                with urllib.request.urlopen(request, timeout=5) as response:
                    cookie = response.headers['Set-Cookie']
                    self.assertIn('__Host-session=', cookie)
                    self.assertIn('Secure', cookie)
                    self.assertIn('HttpOnly', cookie)
                    self.assertIn('SameSite=Lax', cookie)
                    self.assertIn('max-age=31536000', response.headers['Strict-Transport-Security'])
                    self.assertIn(b'/static/auth.css', response.read())
                class NoRedirect(urllib.request.HTTPRedirectHandler):
                    def redirect_request(self, *args):
                        return None
                opener = urllib.request.build_opener(NoRedirect)
                request = urllib.request.Request(f'http://127.0.0.1:{port}/login',
                                                 headers={'Host': 'example.up.railway.app'})
                with self.assertRaises(urllib.error.HTTPError) as captured:
                    opener.open(request, timeout=5)
                self.assertEqual(captured.exception.code, 308)
                self.assertEqual(captured.exception.headers['Location'], 'https://example.up.railway.app/login')
            finally:
                process.terminate()
                process.communicate(timeout=10)
