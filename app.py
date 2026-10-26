"""Local image upload demo backed by the OpenAI Responses API."""
import base64
import io
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import warnings
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, Request, render_template, request, session, redirect, url_for, g, abort
from werkzeug.security import generate_password_hash, check_password_hash
from openai import OpenAI, APIConnectionError, APIStatusError, AuthenticationError, RateLimitError
from PIL import Image, ImageOps, UnidentifiedImageError
from settings import configure_app

load_dotenv(Path(__file__).with_name('.env'))


class MemoryUploadRequest(Request):
    def _get_file_stream(self, total_content_length, content_type, filename=None, content_length=None):
        # MAX_CONTENT_LENGTH bounds memory; avoid Werkzeug's disk-spooled uploads.
        return io.BytesIO()


app = Flask(__name__)
app.request_class = MemoryUploadRequest
data_dir = configure_app(app)
DUMMY_PASSWORD_HASH = generate_password_hash(secrets.token_urlsafe(32))
IMAGE_PROCESSING_LOCK = threading.BoundedSemaphore(1)


def protect_text(value):
    cipher = app.config['DATA_CIPHER']
    return cipher.encrypt(value.encode()).decode() if value and cipher else value


def reveal_text(value, encrypted):
    if not value or not encrypted:
        return value
    cipher = app.config['DATA_CIPHER']
    if cipher is None:
        raise RuntimeError('The original DATA_ENCRYPTION_KEY is required to read this database.')
    return cipher.decrypt(value.encode()).decode()


def extract_json(answer):
    """Return the first complete JSON object/array from an answer, without prose."""
    decoder = json.JSONDecoder()
    for position, character in enumerate(answer):
        if character not in '{[':
            continue
        try:
            value, _ = decoder.raw_decode(answer[position:])
        except (json.JSONDecodeError, RecursionError):
            continue
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    return ''


def recent_json_context(db, user_id, pet_id):
    """Build JSON-only context from the last 30 responses for this pet."""
    rows = db.execute(
        'SELECT json_data, created_at, encrypted FROM responses '
        'WHERE user_id = ? AND pet_id = ? ORDER BY id DESC LIMIT 30',
        (user_id, pet_id),
    ).fetchall()
    return json.dumps({
        'previous_results': [
            {'timestamp_utc': row['created_at'].replace(' ', 'T') + 'Z',
             'result': json.loads(reveal_text(row['json_data'], row['encrypted']))}
            for row in reversed(rows) if row['json_data']
        ]
    }, ensure_ascii=False, separators=(',', ':'))


def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(app.config['DATABASE'], timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA foreign_keys = ON')
        g.db.executescript('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS responses (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                answer TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
            );
            CREATE INDEX IF NOT EXISTS responses_user_recent ON responses(user_id, id DESC);
            CREATE TABLE IF NOT EXISTS pets (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name TEXT NOT NULL COLLATE NOCASE,
                UNIQUE(user_id, name)
            );
            CREATE INDEX IF NOT EXISTS pets_user ON pets(user_id, id);
            CREATE TABLE IF NOT EXISTS auth_sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rate_limits (
                key TEXT PRIMARY KEY,
                count INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS app_metadata (
                name TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        ''')
        key_check = g.db.execute("SELECT value FROM app_metadata WHERE name = 'data_key_check'").fetchone()
        if key_check:
            if reveal_text(key_check['value'], True) != 'pet-image-review':
                raise RuntimeError('Incorrect DATA_ENCRYPTION_KEY.')
        elif app.config['DATA_CIPHER']:
            with g.db:
                g.db.execute("INSERT INTO app_metadata (name, value) VALUES ('data_key_check', ?)",
                             (protect_text('pet-image-review'),))
        # Upgrade databases created before pet profiles, preserving old history.
        columns = {column['name'] for column in g.db.execute('PRAGMA table_info(responses)')}
        if 'pet_id' not in columns:
            g.db.execute('ALTER TABLE responses ADD COLUMN pet_id INTEGER REFERENCES pets(id)')
        with g.db:
            for row in g.db.execute('SELECT DISTINCT user_id FROM responses WHERE pet_id IS NULL').fetchall():
                pet = g.db.execute(
                    "SELECT id FROM pets WHERE user_id = ? AND name = 'Previous uploads'",
                    (row['user_id'],),
                ).fetchone()
                pet_id = pet['id'] if pet else g.db.execute(
                    'INSERT INTO pets (user_id, name) VALUES (?, ?)',
                    (row['user_id'], 'Previous uploads'),
                ).lastrowid
                g.db.execute('UPDATE responses SET pet_id = ? WHERE user_id = ? AND pet_id IS NULL',
                             (pet_id, row['user_id']))
        g.db.execute('CREATE INDEX IF NOT EXISTS responses_pet_recent ON responses(pet_id, id DESC)')
        columns = {column['name'] for column in g.db.execute('PRAGMA table_info(responses)')}
        if 'json_data' not in columns:
            g.db.execute('ALTER TABLE responses ADD COLUMN json_data TEXT')
        if 'encrypted' not in columns:
            g.db.execute('ALTER TABLE responses ADD COLUMN encrypted INTEGER NOT NULL DEFAULT 0')
        # Empty string marks an answer without parseable JSON, so it is not reparsed.
        pending = g.db.execute('SELECT id, answer, encrypted FROM responses WHERE json_data IS NULL').fetchall()
        if pending:
            with g.db:
                g.db.executemany('UPDATE responses SET json_data = ? WHERE id = ?',
                                 [(protect_text(extract_json(reveal_text(row['answer'], row['encrypted'])))
                                   if row['encrypted'] else extract_json(row['answer']), row['id']) for row in pending])
        if app.config['DATA_CIPHER']:
            with g.db:
                for row in g.db.execute('SELECT id, answer, json_data FROM responses WHERE encrypted = 0').fetchall():
                    g.db.execute('UPDATE responses SET answer = ?, json_data = ?, encrypted = 1 WHERE id = ?',
                                 (protect_text(row['answer']), protect_text(row['json_data']), row['id']))
    return g.db


@app.teardown_appcontext
def close_db(error=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()


@app.before_request
def authenticate_and_check_csrf():
    g.user = None
    if request.endpoint == 'health':
        return None
    if app.config['PRODUCTION'] and not request.is_secure:
        return redirect(request.url.replace('http://', 'https://', 1), code=308)
    if session.get('auth_token'):
        g.user = get_db().execute(
            'SELECT users.id, users.username FROM users JOIN auth_sessions '
            'ON users.id = auth_sessions.user_id WHERE token_hash = ? AND expires_at > ?',
            (hashlib.sha256(session['auth_token'].encode()).hexdigest(), int(time.time())),
        ).fetchone()
    if request.endpoint in {'index', 'logout', 'add_pet', 'delete_pet'} and g.user is None:
        return redirect(url_for('login'))
    if request.method == 'POST':
        token = request.form.get('csrf_token', '')
        if not token or not secrets.compare_digest(token.encode(), session.get('csrf_token', '').encode()):
            abort(400, 'Your form expired. Reload the page and try again.')


def limit_request(category, identity, limit, seconds):
    """Persistent, atomic fixed-window limits; identities are HMAC hashed."""
    if not app.config['RATE_LIMIT_ENABLED']:
        return
    now = int(time.time())
    bucket = now // seconds
    key = hmac.new(app.secret_key.encode(), f'{category}:{identity}:{bucket}'.encode(), hashlib.sha256).hexdigest()
    db = get_db()
    with db:
        db.execute('DELETE FROM rate_limits WHERE expires_at <= ?', (now,))
        row = db.execute(
            'INSERT INTO rate_limits (key, count, expires_at) VALUES (?, 1, ?) '
            'ON CONFLICT(key) DO UPDATE SET count = count + 1 RETURNING count',
            (key, (bucket + 1) * seconds),
        ).fetchone()
    if row['count'] > limit:
        abort(429)


def sign_in(user_id):
    token = secrets.token_urlsafe(32)
    db = get_db()
    with db:
        db.execute('DELETE FROM auth_sessions WHERE expires_at <= ?', (int(time.time()),))
        if session.get('auth_token'):
            db.execute('DELETE FROM auth_sessions WHERE token_hash = ?',
                       (hashlib.sha256(session['auth_token'].encode()).hexdigest(),))
        db.execute('INSERT INTO auth_sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)',
                   (hashlib.sha256(token.encode()).hexdigest(), user_id, int(time.time()) + 8 * 3600))
    session.clear()
    session['auth_token'] = token
    session.permanent = True


@app.context_processor
def template_context():
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_hex(32)
    return {'csrf_token': session['csrf_token']}


def owned_pet(pet_id):
    try:
        pet_id = int(pet_id)
    except (TypeError, ValueError):
        return None
    if not 1 <= pet_id <= 9223372036854775807:
        return None
    return get_db().execute('SELECT id, name FROM pets WHERE id = ? AND user_id = ?',
                            (pet_id, g.user['id'])).fetchone()


def show_home(error=None, status=200):
    pets = get_db().execute('SELECT id, name FROM pets WHERE user_id = ? ORDER BY id',
                            (g.user['id'],)).fetchall()
    requested_id = request.args.get('pet_id') if request.method == 'GET' else None
    selected = owned_pet(requested_id or session.get('pet_id'))
    if selected is None and pets:
        selected = pets[0]
    if selected:
        session['pet_id'] = selected['id']
        history = get_db().execute(
            'SELECT answer, created_at, encrypted FROM responses WHERE pet_id = ? AND user_id = ? ORDER BY id DESC LIMIT 3',
            (selected['id'], g.user['id']),
        ).fetchall()
        history = [{'answer': reveal_text(row['answer'], row['encrypted']), 'created_at': row['created_at']}
                   for row in history]
    else:
        session.pop('pet_id', None)
        history = []
    return render_template('index.html', pets=pets, selected_pet=selected,
                           history=history, error=error), status


@app.post('/pets')
def add_pet():
    limit_request('add-pet', g.user['id'], 30, 3600)
    name = request.form.get('name', '').strip()
    if not name or len(name) > 60 or any(ord(character) < 32 for character in name):
        return show_home('Use a pet name with 1–60 visible characters.', 400)
    try:
        db = get_db()
        with db:
            pet_id = db.execute('INSERT INTO pets (user_id, name) VALUES (?, ?)',
                                (g.user['id'], name)).lastrowid
    except sqlite3.IntegrityError:
        return show_home('You already have a pet with that name.', 400)
    session['pet_id'] = pet_id
    return redirect(url_for('index', pet_id=pet_id))


@app.post('/pets/<int:pet_id>/delete')
def delete_pet(pet_id):
    pet = owned_pet(pet_id)
    if pet is None:
        abort(404)
    db = get_db()
    with db:
        db.execute('DELETE FROM responses WHERE pet_id = ? AND user_id = ?', (pet_id, g.user['id']))
        db.execute('DELETE FROM pets WHERE id = ? AND user_id = ?', (pet_id, g.user['id']))
    if session.get('pet_id') == pet_id:
        session.pop('pet_id', None)
    return redirect(url_for('index'))


@app.route('/register', methods=['GET', 'POST'])
def register():
    error = None
    if request.method == 'POST':
        limit_request('register-ip', request.remote_addr, 5, 3600)
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        invitation = app.config['REGISTRATION_CODE']
        supplied_invitation = request.form.get('registration_code', '')
        if invitation and not secrets.compare_digest(invitation.encode(), supplied_invitation.encode()):
            error = 'A valid access code is required to create an account.'
        elif not re.fullmatch(r'[A-Za-z0-9_]{3,32}', username):
            error = 'Use 3–32 letters, numbers, or underscores for your username.'
        elif not 12 <= len(password) <= 128:
            error = 'Use a password with 12–128 characters.'
        elif password != request.form.get('confirm_password'):
            error = 'Passwords do not match.'
        else:
            try:
                db = get_db()
                with db:
                    user_id = db.execute('INSERT INTO users (username, password_hash) VALUES (?, ?)',
                                         (username, generate_password_hash(password))).lastrowid
                sign_in(user_id)
                return redirect(url_for('index'))
            except sqlite3.IntegrityError:
                error = 'That username is already taken.'
    return render_template('auth.html', registering=True, error=error,
                           invitation_required=bool(app.config['REGISTRATION_CODE'])), 400 if error else 200


@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        limit_request('login-ip', request.remote_addr, 20, 900)
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user = get_db().execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        valid = len(password) <= 128 and check_password_hash(user['password_hash'] if user else DUMMY_PASSWORD_HASH, password)
        if user and valid:
            sign_in(user['id'])
            return redirect(url_for('index'))
        error = 'Incorrect username or password.'
    return render_template('auth.html', registering=False, error=error), 401 if error else 200


@app.post('/logout')
def logout():
    db = get_db()
    with db:
        db.execute('DELETE FROM auth_sessions WHERE token_hash = ?',
                   (hashlib.sha256(session.get('auth_token', '').encode()).hexdigest(),))
    session.clear()
    return redirect(url_for('login'))
Image.MAX_IMAGE_PIXELS = 20_000_000
PROMPT = 'Your job is to identify any signs of disease based on this image of a human/animal. Respond either confidently healthy, or list the diseases you may see symptoms of and the % confident that you are for that disease. Additionally respond below that with your answers in a simple json list with the structure disease : % confidnce'


def prepare_image(data):
    """Validate real image bytes, apply orientation, and strip metadata."""
    if not data or len(data) > 10 * 1024 * 1024:
        raise ValueError('Choose an image smaller than 10 MB.')
    try:
        with IMAGE_PROCESSING_LOCK, warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as source:
                if source.format not in {'JPEG', 'PNG', 'WEBP', 'GIF'}:
                    raise ValueError('Use a JPEG, PNG, WebP, or non-animated GIF.')
                if getattr(source, 'is_animated', False):
                    raise ValueError('Please use a non-animated image.')
                source.load()
                normalized = ImageOps.exif_transpose(source).convert('RGB')
                output = io.BytesIO()
                normalized.save(output, format='JPEG', quality=95)
                return 'data:image/jpeg;base64,' + base64.b64encode(output.getvalue()).decode('ascii')
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValueError('The image is damaged, unsupported, or exceeds 20 million pixels.') from None


@app.after_request
def no_cache(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    )
    if app.config['PRODUCTION']:
        response.headers['Strict-Transport-Security'] = 'max-age=31536000'
    return response


@app.errorhandler(429)
def throttled(error):
    response = app.make_response(('Too many attempts. Please try again later.', 429))
    response.headers['Retry-After'] = '900'
    return response


@app.errorhandler(500)
def internal_error(error):
    return 'The request could not be completed. Please try again later.', 500


@app.get('/health')
def health():
    get_db().execute('SELECT 1')
    return {'status': 'ok'}


@app.errorhandler(413)
def too_large(error):
    if getattr(g, 'user', None) is not None:
        return show_home('Upload too large. Choose an image smaller than 10 MB.', 413)
    return 'Request too large.', 413


@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'GET':
        return show_home()
    pet = owned_pet(request.form.get('pet_id'))
    if pet is None:
        return show_home('Choose or create a pet before submitting an image.', 400)
    session['pet_id'] = pet['id']
    upload = request.files.get('image')
    if not upload or not upload.filename:
        return show_home('Please choose an image.', 400)
    try:
        image_url = prepare_image(upload.read())
    except ValueError as exc:
        return show_home(str(exc), 400)
    key = os.getenv('OPENAI_API_KEY', '').strip()
    if not key:
        message = 'Image analysis is unavailable. Please contact the app owner.' if app.config['PRODUCTION'] else 'Add OPENAI_API_KEY to the .env file and restart the server.'
        return show_home(message, 503)
    limit_request('image-user', g.user['id'], app.config['IMAGE_DAILY_LIMIT'], 86400)
    limit_request('image-global', 'all', app.config['IMAGE_GLOBAL_DAILY_LIMIT'], 86400)
    db = get_db()
    prior_results = recent_json_context(db, g.user['id'], pet['id'])
    try:
        with OpenAI(api_key=key, timeout=90.0, max_retries=0) as client:
            result = client.responses.create(
                model=os.getenv('OPENAI_MODEL', '').strip() or 'gpt-4.1-mini',
                store=False,
                max_output_tokens=1500,
                input=[{'role': 'user', 'content': [
                    {'type': 'input_text', 'text': PROMPT},
                    {'type': 'input_text', 'text': prior_results},
                    {'type': 'input_image', 'image_url': image_url},
                ]}],
            )
        answer = result.output_text
        if not answer:
            answer = 'No text was returned. The model may have declined this request.'
        if result.status == 'incomplete':
            answer += '\n\n[The response was cut short. It may be incomplete.]'
        with db:
            db.execute('INSERT INTO responses (user_id, pet_id, answer, json_data, encrypted) VALUES (?, ?, ?, ?, ?)',
                       (g.user['id'], pet['id'], protect_text(answer), protect_text(extract_json(answer)),
                        int(app.config['DATA_CIPHER'] is not None)))
        return redirect(url_for('index', pet_id=pet['id']))
    except AuthenticationError:
        error = 'The API key was rejected. Check OPENAI_API_KEY and restart.'
    except RateLimitError:
        error = 'API quota or rate limit reached. Check API billing or try again later.'
    except APIConnectionError:
        error = 'Could not reach OpenAI, or the request timed out. Please try again.'
    except APIStatusError:
        error = 'OpenAI could not process this request. Check model access and try another image.'
    return show_home(error, 502)


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=False)
