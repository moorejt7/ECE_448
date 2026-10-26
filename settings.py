"""Fail-closed production configuration; local start.bat still works."""
import os
import secrets
from datetime import timedelta
from pathlib import Path

from werkzeug.middleware.proxy_fix import ProxyFix
from cryptography.fernet import Fernet


def configure_app(app):
    production = os.getenv('APP_ENV') == 'production' or bool(os.getenv('RAILWAY_ENVIRONMENT_ID'))
    volume = os.getenv('RAILWAY_VOLUME_MOUNT_PATH', '')
    data_dir = Path(os.getenv('APP_DATA_DIR') or volume or Path(__file__).with_name('instance'))
    secret = os.getenv('SECRET_KEY', '')
    registration_code = os.getenv('REGISTRATION_CODE', '')
    encryption_key = os.getenv('DATA_ENCRYPTION_KEY', '')
    cipher = Fernet(encryption_key.encode()) if encryption_key else None
    if production:
        if not volume or not data_dir.resolve().is_relative_to(Path(volume).resolve()):
            raise RuntimeError('Production requires a Railway volume for APP_DATA_DIR.')
        if len(secret) < 64 or len(set(secret)) < 16:
            raise RuntimeError('Set SECRET_KEY to a cryptographically random value of at least 64 characters.')
        if len(registration_code) < 24:
            raise RuntimeError('Set REGISTRATION_CODE to a random invitation code of at least 24 characters.')
        if cipher is None:
            raise RuntimeError('Set DATA_ENCRYPTION_KEY to a separate Fernet encryption key.')
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != 'nt':
        data_dir.chmod(0o700)
    if not secret:
        secret_path = data_dir / 'session.key'
        try:
            descriptor = os.open(secret_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, 'w') as handle:
                handle.write(secrets.token_hex(32))
        except FileExistsError:
            pass
        secret = secret_path.read_text().strip()
    hosts = [host.strip() for host in os.getenv('ALLOWED_HOSTS', '').split(',') if host.strip()]
    public_domain = os.getenv('RAILWAY_PUBLIC_DOMAIN', '')
    if public_domain:
        hosts.append(public_domain)
    if production:
        hosts.append('healthcheck.railway.app')
        # Only deploy behind Railway's single trusted ingress proxy.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
    app.config.update(
        PRODUCTION=production,
        SECRET_KEY=secret,
        DATABASE=str(data_dir / 'history.sqlite3'),
        REGISTRATION_CODE=registration_code,
        DATA_CIPHER=cipher,
        TRUSTED_HOSTS=hosts if production else ['localhost', '127.0.0.1', '[::1]'],
        SESSION_COOKIE_NAME='__Host-session' if production else 'session',
        SESSION_COOKIE_SECURE=production,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_PATH='/',
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        MAX_CONTENT_LENGTH=11 * 1024 * 1024,
        MAX_FORM_MEMORY_SIZE=128 * 1024,
        MAX_FORM_PARTS=10,
        RATE_LIMIT_ENABLED=True,
        IMAGE_DAILY_LIMIT=int(os.getenv('IMAGE_DAILY_LIMIT', '30')),
        IMAGE_GLOBAL_DAILY_LIMIT=int(os.getenv('IMAGE_GLOBAL_DAILY_LIMIT', '100')),
    )
    return data_dir
