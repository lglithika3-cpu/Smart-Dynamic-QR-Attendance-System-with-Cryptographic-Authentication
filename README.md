# Privacy Attendance

An existing Flask application for role-aware academic attendance, short-lived HMAC-authenticated QR sessions, and SQLAlchemy persistence (SQLite for local development and Aiven MySQL for production). Flask/Jinja renders the dashboards; SQLAlchemy models the data; Flask-Login, Flask-WTF, Flask-Limiter, and Flask-Migrate provide authentication, CSRF, rate limiting, and schema revisions.

## Architecture

- `app/models/models.py`: users and role profiles, departments, courses, subjects, enrollments, faculty assignments, sessions, QR tokens, attendance, corrections, audit events, login attempts, reset tokens, and backup records.
- `app/auth/`: registration, login/logout, lockout, and password reset.
- `app/faculty/`: owned sessions, QR issue/revocation, student filters, CSV export, and correction review.
- `app/student/`: the signed-in student's own attendance, subject summaries, scan submission, and correction requests.
- `app/admin/`: account review/status, security counters, audit search/export, QR revocation, and backups.
- `app/services/`: QR signing/verification, attendance percentage, password reset delivery, CSV safety, and SQLite backup/recovery.
- `migrations/`: a clean-schema baseline followed by additive security metadata.

SQLite uses primary/foreign keys, unique email/subject/session constraints, and indexes. A unique `(session_id, student_id)` constraint prevents duplicate attendance even during concurrent submissions. Passwords are Werkzeug `scrypt` hashes, never encrypted or stored in plaintext.

## Windows Setup

In PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Generate two independent secrets without printing them, then place them in the ignored `.env` file:

```powershell
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
$bytes = New-Object byte[] 48
$rng.GetBytes($bytes)
$secret = [Convert]::ToBase64String($bytes)
$rng.GetBytes($bytes)
$qrSecret = [Convert]::ToBase64String($bytes)
$envLines = Get-Content .env
$envLines = $envLines -replace '^SECRET_KEY=.*$', "SECRET_KEY=$secret"
$envLines = $envLines -replace '^QR_SIGNING_SECRET=.*$', "QR_SIGNING_SECRET=$qrSecret"
Set-Content -Encoding ascii .env $envLines
```

Set a unique `ADMIN_EMAIL` and `ADMIN_PASSWORD` of at least 12 characters in `.env`. Configure `PUBLIC_BASE_URL`, `MAIL_HOST`, and `MAIL_FROM` (plus SMTP credentials if required) to enable password-reset delivery. Set `SESSION_COOKIE_SECURE=True` behind HTTPS. The default `memory://` rate-limit store is for local development; production should configure shared Redis with `RATELIMIT_STORAGE_URI=redis://...`.

### Existing Database

Back up first. For a database created by the original prototype and not yet tracked by Alembic, use the protected SQLite backup helper:

```powershell
python -c "from app import create_app; from app.services.backup_service import create_backup; app=create_app(); context=app.app_context(); context.push(); print(create_backup()); context.pop()"
```

Then mark the existing schema at the frozen baseline and apply the additive security revision:

```powershell
python -m flask --app run.py db stamp bc3c6351086c
python -m flask --app run.py db upgrade
```

Do not stamp an empty or unverified database. The current development database was backed up, migrated, and verified; its existing user, profile, attendance session/record, QR-token, and audit-event row counts were preserved.

### Clean Database

For a new empty database, use the normal migration path:

```powershell
python -m flask --app run.py db upgrade
```

Create the first administrator, then start the local server:

```powershell
python -m flask --app run.py bootstrap-admin
python run.py
```

Visit `http://127.0.0.1:5000`. Startup does not call `create_all()`; schema changes must go through migrations.

## Aiven MySQL and Render Deployment

The production database target is the Aiven MySQL service specified for this deployment. The existing SQLAlchemy models and Alembic migrations are retained; the application does not call `create_all()` at startup. MySQL connections use PyMySQL and require the Aiven CA certificate with certificate and hostname verification enabled. PostgreSQL URL support remains available for existing deployments.

### Configure a local MySQL connection

Copy `.env.example` to `.env` if needed. Set `DB_HOST`, `DB_PORT`, `DB_NAME`, and `DB_USER` to the values in `render.yaml`, put the database password only in the ignored `.env` as `DB_PASSWORD`, and set `DB_SSL_MODE=REQUIRED`. Download the CA certificate from the Aiven service's connection information and set `DATABASE_SSL_CA` to the local certificate file path. Do not put database passwords or private keys in source files or commit `.env`.

When `DB_HOST` is set, the app builds a MySQL URL safely from the separate `DB_*` values, including passwords containing URL punctuation. When `DB_HOST` is blank, the app continues to use `DATABASE_URL` (SQLite by default in `.env.example`). MySQL startup fails clearly if the password, CA file, or required TLS configuration is missing.

### Push the existing project to GitHub

The existing `main` branch is the deployment source. After reviewing the diff, publish only the intended changes from PowerShell:

```powershell
git status --short
git add .gitignore app/config.py app/__init__.py tests/test_basic.py .env.example render.yaml README.md
git diff --cached --check
git commit -m "Configure verified Aiven MySQL deployment"
git push origin main
```

`.env`, `instance/`, and local backups are excluded by `.gitignore`. Never use `git add -f` on these paths.

### Create and deploy the Render web service

1. Sign in to Render and connect the GitHub account that owns the existing repository. Select **New → Blueprint**, choose `lglithika3-cpu/Smart-Dynamic-QR-Attendance-System-with-Cryptographic-Authentication`, and select branch `main`.
2. Review the root `render.yaml` before applying it. It defines a Python 3.11 service build (`pip install -r requirements.txt`), a start command that runs Alembic upgrades and the idempotent administrator bootstrap before Gunicorn, and `/attendance/health` as the health check.
3. In the service's environment settings, provide `DB_PASSWORD`, a strong `QR_SIGNING_SECRET` (at least 32 characters and different from `SECRET_KEY`), `ADMIN_EMAIL`, and a unique `ADMIN_PASSWORD` (at least 12 characters). Render generates `SECRET_KEY`. Do not send these secrets in chat or commit them.
4. In Render, add the Aiven CA certificate as a **Secret File** named `aiven-ca.pem`. Render mounts it at `/etc/secrets/aiven-ca.pem`; set `DATABASE_SSL_CA` to exactly `/etc/secrets/aiven-ca.pem`. Keep `DB_SSL_MODE=REQUIRED`; the application rejects a missing/invalid CA file and uses a verifying TLS context. Do not disable certificate verification.
5. Deploy and review the build/deploy logs. A healthy service must return HTTP 200 from `/attendance/health` with `{"status":"ok","database":"ok"}`. A database or migration failure must be resolved from its explicit log before proceeding.
6. After Render assigns the public `https://` service URL, set `PUBLIC_BASE_URL` to that exact URL if password-reset email is configured. The same HTTPS URL works on laptops and mobile devices; HTTPS (or localhost during development) is required for camera access.

`render.yaml` provides the non-secret Aiven host, port, database name, user, and TLS mode from the supplied configuration. Enter the password and CA file through Render's protected service settings. The first bootstrap creates the administrator only if its email is not already present; it does not reset an existing password. Configure mail settings in Render only if password-reset emails are required.

### Existing database data and migrations

Back up existing Aiven data before schema changes. The deployment start command runs `flask --app run.py db upgrade`, which applies the repository's Alembic revisions; it does not recreate tables with `create_all()`. If the Aiven database already contains application tables, first verify its Alembic revision and schema. Do not stamp a revision or run the SQLite importer against a non-empty target without checking that the schemas match. The importer refuses non-empty application tables and can transfer the local SQLite rows only when the target is empty. Private profile-photo files are outside the database and need separate persistent storage; Render's free filesystem is ephemeral.

The dynamic attendance QR remains HMAC-SHA256 signed, short-lived, nonce-bound, revocable, and checked server-side. The database's unique `(session_id, student_id)` constraint prevents duplicate attendance. For mobile scanning, sign in on the phone, open **Scan QR**, grant camera permission, and scan the active faculty QR displayed on the laptop. Camera scanning itself still needs verification on a real HTTPS deployment and device.

### Local verification before deployment

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe run.py
```

Run `flask db upgrade` only after confirming the selected `DATABASE_URL`/`DB_*` target and backing up any existing database. Do not point a local migration command at a live database unintentionally.

Open `http://127.0.0.1:5000` on the laptop. Camera access on a phone requires the deployed HTTPS address (or a local HTTPS tunnel), not the laptop's `127.0.0.1` address.

## Security Behavior

- CSRF is enabled on browser forms and state-changing actions, including logout, QR issue, account changes, corrections, and backup initiation.
- Login rate limits and keyed-identifier lockout are enforced. Sessions are permanent for a configured bounded period; account suspension and password reset increment `auth_version` to invalidate prior sessions.
- Each successful login is tracked in `user_sessions` until expiration/revocation. Students can see their own active-session timestamps and invalidate all devices; password changes invalidate other sessions while preserving the current one.
- Optional TOTP two-factor authentication is available from the student profile. TOTP secrets are encrypted at rest with a key derived from `QR_SIGNING_SECRET`; preserve that secret during recovery and key rotation or enrolled authenticators will need to be reset. TOTP challenges expire after five minutes and accepted time-step codes cannot be reused.
- Reset tokens use cryptographically random values, are stored only as SHA-256 hashes, expire, and are single-use. Email links require an explicitly configured trusted public origin and SMTP delivery. The token is carried in the URL fragment and submitted in a CSRF-protected POST body.
- QR payloads use canonical JSON and HMAC-SHA256 with a separate environment secret. The signed payload binds session, faculty, issue/expiry timestamps, token ID, and nonce. Default lifetime is 30 seconds (configurable up to 300); tokens are rotated, can be revoked, and closed sessions reject submissions. HMAC authenticates, it does not encrypt or conceal the payload.
- Student attendance and correction routes scope access to the signed-in student's profile. Faculty report/session operations scope to sessions owned by that faculty member. Admin audit/export/status operations require the admin role.
- Audit records contain event type, UTC timestamp, optional role/success, sanitized description, and per-request correlation ID. They do not include passwords, QR payloads, signing keys, reset tokens, or session IDs. Admin counts are monitoring indicators, not a complete intrusion-detection system.
- Security headers, request-size limits, Jinja escaping, rate limits, and SQLAlchemy parameterized queries are enabled. The QR scanner loads the existing `jsQR` browser library from jsDelivr; production CSP must allow that host or serve a pinned local copy.
- Passwords are one-way scrypt hashes; profile fields are ordinary database values, profile photos are private files, and TOTP seeds are encrypted at rest. Do not add new sensitive fields without a key-management/rotation plan.
- Interface icons are local inline SVGs with theme colors; no third-party icon service is contacted.

## Student Profile and Attendance

- Student profiles show the account's register/login ID, verified email, department, year, section, date of birth, gender, phone, last login, and active sessions. Students can update contact/profile details and their photo; register number, department, year, and section remain institution-controlled. Email changes are read-only until an email-verification workflow is configured.
- Profile photos accept JPEG, PNG, and WebP uploads, are normalized, and are stored outside the public static directory. Photo retrieval requires the owning student to be signed in.
- The attendance dashboard calculates overall and per-subject totals from finalized database sessions and present attendance records. `ATTENDANCE_ELIGIBILITY_THRESHOLD` controls Eligible/Shortage status (default: `80` percent).
- Subject pages include date-wise present/absent history, class title/period, faculty, marking time, and whether a record has an authenticated QR token. Students can download a database-backed PDF report (overall, subject-wise, and date-wise) or print the dashboard.
- Attendance/security notices are derived from recent records and audit events. Admins can publish scheduled attendance announcements from the admin dashboard; students see current announcements and announcements scheduled within 30 days.
- The account security indicator is shown as protected only when the request is HTTPS, secure cookies are enabled, the account is active, the password hash is scrypt, and TOTP is enabled. HTTPS must be terminated by a trusted reverse proxy or configured web server; the Flask development server itself does not provide HTTPS.
- The demo curriculum contains ten Computer Science subjects. Do not run the demo seeder against a real student database: wait for and use the institution's complete roster instead. No student accounts were created for this request because only one roster row was supplied and you asked to wait for all 63.

## Backup and Recovery

Backups use SQLite's online backup API, live under `instance/backups` (outside Flask's public static directory), and are checked with `PRAGMA integrity_check` plus table row-count comparison. Admins can initiate backups from the dashboard; the CLI alternative is:

```powershell
python -m flask --app run.py create-backup
```

Restore only a reviewed backup. This process validates the file, creates a separate safety backup of the current database, copies to a temporary database, verifies it, then replaces the active database:

```powershell
$env:BACKUP_FILENAME = 'attendance_backup_YYYYMMDDTHHMMSSffffffZ.db'
$env:CONFIRM_RESTORE = 'YES'
python -m flask --app run.py restore-backup
Remove-Item Env:BACKUP_FILENAME
Remove-Item Env:CONFIRM_RESTORE
```

The SQLite backup does not contain `.env`. Keep `SECRET_KEY` and `QR_SIGNING_SECRET` in a separate protected secret store and preserve them during recovery; losing the QR key invalidates existing signed QR payloads.

## Tests

Tests use independent in-memory SQLite databases. Run:

```powershell
python -m pytest -q
```

Coverage includes role restrictions, CSRF, QR signature/expiry/nonce, duplicate attendance, lockout/reset handling, audit records, correction ownership, account/session invalidation, CSV formula escaping, and backup integrity. Fresh and legacy migration paths were also exercised against disposable SQLite files.

The current schema migration merges the existing faculty/profile branches and adds student profile metadata, encrypted TOTP settings, server-tracked browser sessions, and scheduled announcements. Apply it with `python -m flask --app run.py db upgrade` after taking a backup. Existing signed-cookie sessions will need to sign in again after this migration because the new server-side session inventory starts empty. `cryptography` encrypts TOTP seeds and `reportlab` generates PDF reports; both are installed from `requirements.txt`.

## Limitations

- Password-reset email requires SMTP and a trusted `PUBLIC_BASE_URL`; without those settings the response remains generic and no usable reset token is left active.
- Rate limits default to process memory, which is not shared or durable across workers; configure Redis in production.
- Existing audit rows predate role/success/correlation metadata and retain `NULL` in those fields.
- QR attendance can establish possession of a valid short-lived token, not physical presence by itself.
- No AES field encryption is implemented because the current schema has no justified confidential field to encrypt. HMAC is not encryption.
