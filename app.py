"""Local image upload demo backed by the OpenAI Responses API."""
import base64
import io
import json
import os
import re
import secrets
import sqlite3
import warnings
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, render_template, request, session, redirect, url_for, g, abort
from werkzeug.security import generate_password_hash, check_password_hash
from openai import OpenAI, APIConnectionError, APIStatusError, AuthenticationError, RateLimitError
from PIL import Image, ImageOps, UnidentifiedImageError

load_dotenv(Path(__file__).with_name('.env'))
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 11 * 1024 * 1024
data_dir = Path(__file__).with_name('instance')
data_dir.mkdir(exist_ok=True)
secret_path = data_dir / 'session.key'
try:
    with secret_path.open('x') as secret_file:
        secret_file.write(secrets.token_hex(32))
except FileExistsError:
    pass
app.config.update(
    SECRET_KEY=secret_path.read_text().strip(),
    DATABASE=str(data_dir / 'history.sqlite3'),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
)


def extract_json(answer):
    """Return the first complete JSON object/array from an answer, without prose."""
    decoder = json.JSONDecoder()
    for position, character in enumerate(answer):
        if character not in '{[':
            continue
        try:
            value, _ = decoder.raw_decode(answer[position:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    return ''


def recent_json_context(db, user_id, pet_id):
    """Build JSON-only context from the last 30 responses for this pet."""
    rows = db.execute(
        'SELECT json_data, created_at FROM responses '
        'WHERE user_id = ? AND pet_id = ? ORDER BY id DESC LIMIT 30',
        (user_id, pet_id),
    ).fetchall()
    return json.dumps({
        'previous_results': [
            {'timestamp_utc': row['created_at'].replace(' ', 'T') + 'Z',
             'result': json.loads(row['json_data'])}
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
        ''')
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
        # Empty string marks an answer without parseable JSON, so it is not reparsed.
        pending = g.db.execute('SELECT id, answer FROM responses WHERE json_data IS NULL').fetchall()
        if pending:
            with g.db:
                g.db.executemany('UPDATE responses SET json_data = ? WHERE id = ?',
                                 [(extract_json(row['answer']), row['id']) for row in pending])
    return g.db


@app.teardown_appcontext
def close_db(error=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()


@app.before_request
def authenticate_and_check_csrf():
    g.user = None
    if session.get('user_id'):
        g.user = get_db().execute('SELECT id, username FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    if request.endpoint in {'index', 'logout', 'add_pet', 'delete_pet'} and g.user is None:
        return redirect(url_for('login'))
    if request.method == 'POST':
        token = request.form.get('csrf_token', '')
        if not token or not secrets.compare_digest(token, session.get('csrf_token', '')):
            abort(400, 'Your form expired. Reload the page and try again.')


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
            'SELECT answer, created_at FROM responses WHERE pet_id = ? AND user_id = ? ORDER BY id DESC LIMIT 3',
            (selected['id'], g.user['id']),
        ).fetchall()
    else:
        session.pop('pet_id', None)
        history = []
    return render_template('index.html', pets=pets, selected_pet=selected,
                           history=history, error=error), status


@app.post('/pets')
def add_pet():
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
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if not re.fullmatch(r'[A-Za-z0-9_]{3,32}', username):
            error = 'Use 3–32 letters, numbers, or underscores for your username.'
        elif not 8 <= len(password) <= 128:
            error = 'Use a password with 8–128 characters.'
        elif password != request.form.get('confirm_password'):
            error = 'Passwords do not match.'
        else:
            try:
                db = get_db()
                with db:
                    user_id = db.execute('INSERT INTO users (username, password_hash) VALUES (?, ?)',
                                         (username, generate_password_hash(password))).lastrowid
                session.clear()
                session['user_id'] = user_id
                session.permanent = True
                return redirect(url_for('index'))
            except sqlite3.IntegrityError:
                error = 'That username is already taken.'
    return render_template('auth.html', registering=True, error=error), 400 if error else 200


@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user = get_db().execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        if user and len(password) <= 128 and check_password_hash(user['password_hash'], password):
            session.clear()
            session['user_id'] = user['id']
            session.permanent = True
            return redirect(url_for('index'))
        error = 'Incorrect username or password.'
    return render_template('auth.html', registering=False, error=error), 401 if error else 200


@app.post('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))
Image.MAX_IMAGE_PIXELS = 20_000_000
PROMPT = 'Your job is to identify any signs of disease based on this image of a human/animal. Respond either confidently healthy, or list the diseases you may see symptoms of and the % confident that you are for that disease. Additionally respond below that with your answers in a simple json list with the structure disease : % confidnce'


def prepare_image(data):
    """Validate real image bytes, apply orientation, and strip metadata."""
    if not data or len(data) > 10 * 1024 * 1024:
        raise ValueError('Choose an image smaller than 10 MB.')
    try:
        with warnings.catch_warnings():
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
    return response


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
        return show_home('Add OPENAI_API_KEY to the .env file and restart the server.', 503)
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
            db.execute('INSERT INTO responses (user_id, pet_id, answer, json_data) VALUES (?, ?, ?, ?)',
                       (g.user['id'], pet['id'], answer, extract_json(answer)))
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
