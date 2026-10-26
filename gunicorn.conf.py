import os

bind = '0.0.0.0:' + os.environ.get('PORT', '8000')
workers = 1
threads = 4
worker_class = 'gthread'
timeout = 120
graceful_timeout = 120
preload_app = True
umask = 0o077
accesslog = None  # Never log cookies, credentials, uploads, or model results.
errorlog = '-'
loglevel = 'warning'
limit_request_line = 4094
limit_request_fields = 50
limit_request_field_size = 8190


def on_starting(server):
    from app import app, get_db
    with app.app_context():
        get_db().execute('PRAGMA journal_mode=WAL')
