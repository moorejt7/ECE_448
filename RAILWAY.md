# Railway deployment

Deploy this repository from the **moorejt7** GitHub account. Railway signup, account terms, billing, and GitHub App authorization must be completed by the account owner. Grant the GitHub App access only to `ECE_448`.

## Service and volume

1. Create a Railway project from `moorejt7/ECE_448`, branch `main`.
2. Attach one persistent volume at `/data` **before starting the app**. Keep one service replica. Database migrations run at server startup after the volume is mounted.
3. Railway reads `railway.toml`, installs the pinned requirements, and starts Gunicorn. Python 3.12 is selected by `.python-version`. `/health` checks database availability without exposing account data.
4. Generate a Railway HTTPS domain. `RAILWAY_PUBLIC_DOMAIN` is automatically trusted; add any custom domain to `ALLOWED_HOSTS` as a comma-separated hostname list. Do not include URL schemes or wildcards.

## Service variables

Set these in Railway's Variables panel, never in GitHub:

| Variable | Value |
| --- | --- |
| `APP_ENV` | `production` |
| `APP_DATA_DIR` | `/data` |
| `SECRET_KEY` | A new cryptographically random value of at least 64 characters |
| `REGISTRATION_CODE` | A separate random invitation code of at least 24 characters |
| `DATA_ENCRYPTION_KEY` | A separate Fernet key, generated with `Fernet.generate_key()` |
| `OPENAI_API_KEY` | A new restricted project API key, with project spending limits configured |
| `OPENAI_MODEL` | `gpt-4.1-mini`, or another image-capable model available to the project |
| `IMAGE_DAILY_LIMIT` | Optional; defaults to 30 API attempts per user per UTC day |
| `IMAGE_GLOBAL_DAILY_LIMIT` | Optional; defaults to 100 API attempts total per UTC day |

Use a password manager's random generator for the secret and invitation code. Generate the data key locally with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` and enter it directly into Railway Variables. Store a private recovery copy of this key separately from database backups. Losing the key makes saved responses unrecoverable; changing it in Variables is not a data-key rotation. Keep the invitation code private and share it only with intended users. Any API key previously pasted into chat should be revoked and replaced before deployment. Never paste replacement keys into issues, commits, logs, or chat.

The app refuses to start in production without persistent storage, a strong session secret, an invitation code, or a data encryption key. Cookies are Secure/HttpOnly/SameSite=Lax; HTTP traffic redirects to HTTPS. The app trusts one ingress proxy, so this configuration is intended specifically for Railway behind its ingress. Do not expose the container port directly on another host using this proxy configuration.

## Verify before sharing the URL

- Check that GitHub's **Tests and dependency audit** workflow passes. Enable Railway **Wait for CI** on this service.
- Check `/health`, HTTPS redirect, login, invitation registration, pet creation, upload, and logout.
- Confirm the three newest results appear only under their pet. Restart the service and confirm history remains.
- Configure account MFA on GitHub and Railway where available. Keep GitHub App permissions restricted to this repository.
- Enable Railway volume backups. Also use `python backup_db.py /data/backups/YYYY-MM-DD.sqlite3` for a transaction-consistent SQLite backup, then retain an encrypted offsite copy under the owner's control. A backup on the same volume alone does not protect against losing that volume. Test restoration in a separate private environment.
- Set Railway spending limits/alerts and OpenAI project budget controls before granting access to other users. Application quotas count attempts, including failures, to prevent free retries from bypassing limits.

The hosted app starts with a new database. Response text and JSON are encrypted before database storage with Fernet authenticated encryption, using `DATA_ENCRYPTION_KEY` stored separately in Railway Variables. Account usernames and pet names remain database metadata; passwords are one-way scrypt hashes. The local `instance/` database and `.env` are excluded from deployment. Moving existing accounts and health results requires a separate private data migration.

## Security maintenance

New passwords require at least 12 characters and use Werkzeug's salted scrypt hashes. Sessions have an eight-hour server-side expiry and are revoked on logout. Login and registration attempts are rate-limited using SQLite. CSRF tokens protect all POST forms; user/pet ownership is checked server-side. Uploaded images are validated and not kept. Account and response data are private files on the volume; no database port is exposed.

The browser uses a strict CSP with external scripts/styles, frame blocking, a restrictive permissions policy, no-referrer, no-store, and HSTS. CI tests user isolation, session revocation, CSRF, limits, and production configuration. Runtime requirements include exact versions and package hashes; Dependabot opens dependency update PRs. Review and merge security updates promptly, regenerate the lock with `pip-compile --generate-hashes --strip-extras --no-emit-index-url requirements.in`, and rerun the tests and `pip-audit -r requirements.txt --require-hashes`.

Reference: https://docs.railway.com/guides/flask
