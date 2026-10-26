# Image Health Review

A local Python/Flask HTML server that sends one uploaded image and your exact requested prompt to the OpenAI Responses API, then displays the returned text on the page.

For HTTPS hosting, persistent storage, deployment variables, and security maintenance, see [RAILWAY.md](RAILWAY.md). Production signup requires an invitation code to protect paid API usage.

## Run on Windows (Python 3.10+)

Double-click `start.bat` to create a virtual environment, install dependencies, and start the server. Or run these commands:

```powershell
cd C:\ECE_449
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

Open http://127.0.0.1:5000, create an account, and log in. Stop with Ctrl+C.

## Accounts, pets, and response history

Passwords are stored as salted scrypt hashes, not plain text. New passwords need 12 characters or more; existing passwords still work. Usernames are case-insensitive. Login sessions expire after eight hours, are revoked on logout, and forms include CSRF protection. Log out using the button on the upload page.

SQLite creates `instance/history.sqlite3` automatically. After login, add a pet and select its name before uploading an image. Every successful API response is saved under that pet. The selected pet's three newest responses appear below the image submission area, newest first, with UTC timestamps. Older responses remain in the database. Removing a pet permanently deletes its saved responses after a confirmation. Previously saved account-level responses are moved into a "Previous uploads" pet for each affected account. Responses generated before account history existed cannot be recovered.

For each new image, the app also reads that pet's last 30 saved responses. It extracts valid JSON objects or arrays from those responses and sends only those JSON values with their UTC timestamps as context alongside the new image. Explanatory prose, malformed JSON, and results belonging to other pets or accounts are omitted. The JSON is stored separately from the full answer; the webpage still shows the full answer. Older saved answers are processed automatically when the database is opened after this update. If none of the last 30 answers has valid JSON, the context is an empty JSON list. Prior model results are context, not verified diagnoses.

Keep the `instance` folder private and back it up to preserve accounts and history. For local use, `instance/session.key` persists the session signing secret across restarts. Production uses separate `SECRET_KEY` and `DATA_ENCRYPTION_KEY` environment variables. Images are not stored; hosted response text and JSON use authenticated encryption in the database. Local history stays unencrypted unless `DATA_ENCRYPTION_KEY` is explicitly configured. The database and signing key are excluded from Git. Hosting uses the Railway configuration described in `RAILWAY.md`.

The API key is intentionally blank in `.env`. Add it locally:

```dotenv
OPENAI_API_KEY=your-key-here
OPENAI_MODEL=gpt-4.1-mini
```

Restart after editing `.env`. An existing environment variable takes precedence over `.env`. The page loads without a key; analysis shows a setup message until a key is supplied. An OpenAI API account with billing and access to the selected image-capable model is required. This uses the API, not a signed-in ChatGPT browser session. Do not commit or share `.env`.

## Behavior

- Accepts JPEG, PNG, WebP, and non-animated GIF, up to 10 MB / 20 million pixels.
- Validates image contents, applies camera orientation, and converts to JPEG without metadata before sending.
- Sends the exact prompt in `app.py` without rewriting it. The model may decline, qualify its answer, or use a different response format.
- Shows returned text with HTML escaping. Responses are saved per pet in SQLite; uploaded images are not saved. Requests use `store=False`; OpenAI's own data policies still apply.
- Handles missing/invalid keys, invalid uploads, timeouts, quota issues, and API errors.
- `start.bat` binds only to localhost with debug disabled. Railway uses Gunicorn, HTTPS, persistent storage, invitation-only signup, and secure cookies.

The requested health labels and percentages are model-generated and are not validated diagnoses or probabilities. The page includes a clinician/veterinarian notice.

API reference: https://developers.openai.com/api/docs/guides/images-vision

## Offline tests

```powershell
.\.venv\Scripts\python.exe -m unittest -v test_app.py
```

Tests mock OpenAI, so they do not send images or incur API charges. A real API call still needs your key.
