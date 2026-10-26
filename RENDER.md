# Render deployment

The `render.yaml` Blueprint deploys `moorejt7/ECE_448` on a paid Python web service with one 1 GB persistent disk. It builds with hash-locked dependencies, runs Gunicorn, checks `/health`, and deploys new commits only after GitHub CI passes. Account creation, GitHub authorization, and paid-service approval must be completed by the owner.

## Deploy

1. Sign in to Render with the **moorejt7** GitHub account. Grant the Render GitHub App access only to `ECE_448`.
2. Choose **New > Blueprint**, select `moorejt7/ECE_448`, and use `render.yaml` from `main`.
3. Review the paid `0.5c-512mb` service and 1 GB disk before approving creation. The Blueprint defines a paid resource; it is not a free-tier deployment.
4. When prompted, enter a **new** restricted OpenAI project API key directly into Render's `OPENAI_API_KEY` field. Revoke the key previously shared in chat. Do not put keys in GitHub, chat, issues, or logs.
5. Deploy. Render generates the session secret, access code, and data encryption key as separate random 256-bit values. These are created once and retained on subsequent Blueprint syncs. Store a private recovery copy of `DATA_ENCRYPTION_KEY` separately from database backups. If it is lost, saved responses cannot be recovered. Changing its variable is not a data-key rotation.
6. Open the provided `https://...onrender.com` URL. Retrieve `REGISTRATION_CODE` privately from Render's Environment panel to create an app account. Share it only with intended users; it protects paid API usage from public signup abuse.

No live deployment is created merely by committing the Blueprint.

## Storage, domains, and environment

The SQLite database lives at `/var/data/app/history.sqlite3` on the attached disk. `PERSISTENT_DATA_ROOT` points to the `/var/data` mount and `APP_DATA_DIR` points to the private `/var/data/app` subdirectory. The app refuses to create a missing production storage root, reducing the risk of accidentally using ephemeral storage. Do not remove the disk. Keep a single service instance; this SQLite deployment does not support horizontal scaling. Migrations run during Gunicorn startup after Render mounts the disk, not during build or pre-deploy.

Render's `RENDER=true` enables production settings automatically. `RENDER_EXTERNAL_HOSTNAME` adds the generated hostname to the trusted-host list. For a custom domain, set `ALLOWED_HOSTS` to its exact hostname (comma-separated for multiple domains), without a URL scheme or wildcard. Render health checks use a custom domain if one is configured, so update the trusted hosts before adding custom domains. `/health` allows internal HTTP health checks, while browser pages require HTTPS. Proxy headers are trusted only for the provider's ingress; do not expose this configuration directly to arbitrary clients on another host.

`GUNICORN_CMD_ARGS` overrides Render's default access-log arguments; the repository's Gunicorn settings keep request logs disabled. Never log API keys, session cookies, uploaded images, response data, or invitation codes. App errors return generic messages with debug mode disabled.

## Security and operations

- Keep the generated `SECRET_KEY`, `REGISTRATION_CODE`, and `DATA_ENCRYPTION_KEY` in Render variables. Never commit them. Use MFA for GitHub and Render where available and limit repository/deployment access.
- Responses and JSON use application-level Fernet authenticated encryption. Usernames and pet names remain metadata; passwords use salted scrypt hashes. Images stay in bounded memory and are not persisted by the app.
- Secure/HttpOnly/SameSite cookies, server-side session expiry/revocation, CSRF, trusted hosts, HTTPS/HSTS, CSP, and request limits remain enabled.
- Review the API quotas: defaults are 30 attempts per user and 100 total per UTC day. Set OpenAI project budget controls and Render usage alerts before sharing access.
- Create transaction-consistent backups with `python backup_db.py /var/data/app/backups/YYYY-MM-DD.sqlite3` from the service shell. Keep an encrypted offsite copy and test restoration separately. Render disk snapshots are an additional recovery layer; do not rely solely on filesystem snapshots for database recovery.
- Verify GitHub CI, `/health`, HTTPS, login, invitation registration, pets, an upload with the replacement API key, logout, and history persistence after a restart before sharing the URL. Linux CI exercises the real Gunicorn server in both Render and Railway configurations without making API calls.
- Review Dependabot updates promptly. Runtime dependencies remain pinned with hashes and audited in CI.

The hosted database starts empty. Local accounts, health results, `.env`, and `instance/` are not uploaded. Existing private data requires a separate migration.

Persistent-disk deploys briefly interrupt availability. Do not downgrade to Render's free service: it cannot preserve this SQLite database with an attached persistent disk.

Official references: https://render.com/docs/blueprint-spec, https://render.com/docs/disks, https://render.com/docs/health-checks
