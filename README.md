# Privacy Attendance

An existing Flask application for role-aware academic attendance, short-lived HMAC-authenticated QR sessions, and SQLite persistence. Flask/Jinja renders the dashboards; SQLAlchemy models the data; Flask-Login, Flask-WTF, Flask-Limiter, and Flask-Migrate provide authentication, CSRF, rate limiting, and schema revisions.

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

## Render Deployment

Render hosts the Flask web service but its managed database product is PostgreSQL, not MySQL. This project can connect to an external cloud MySQL 8-compatible provider; create the database with that provider and use its externally reachable, TLS-enabled connection details. The application uses SQLAlchemy with PyMySQL when `DATABASE_URL` uses `mysql://` or `mysql+pymysql://`.

Because you selected an import of the current SQLite records, complete the **Migrate the current SQLite records** steps before creating or starting the Render web service. Its startup command creates the initial administrator, and the importer intentionally refuses a non-empty target database.

### Push the existing project to GitHub

From the project directory in PowerShell, use the empty repository you created at [Smart-Dynamic-QR-Attendance-System-with-Cryptographic-Authentication](https://github.com/lglithika3-cpu/Smart-Dynamic-QR-Attendance-System-with-Cryptographic-Authentication), then run:

```powershell
git init
git add .
git status --short
```

Confirm `.env` and `instance/` are not listed before committing; they contain credentials, the local database, backups, and private profile photos.

```powershell
git commit -m "Prepare attendance app for deployment"
git branch -M main
git remote add origin https://github.com/lglithika3-cpu/Smart-Dynamic-QR-Attendance-System-with-Cryptographic-Authentication.git
git push -u origin main
```

The root `render.yaml` defines the Python web service, health check, migrations, initial administrator creation, and Gunicorn start command. After migrating the SQLite data, in Render choose **New → Blueprint**, connect this GitHub repository, and apply the Blueprint. Set the prompted environment variables:

- `DATABASE_URL`: the cloud provider's MySQL URL, for example `mysql+pymysql://USER:PASSWORD@HOST:3306/DATABASE?charset=utf8mb4`. URL-encode special characters in the username/password. Follow the provider's instructions to allow the Render service to connect and require TLS; if it supplies a CA certificate file, add it as a Render secret file and set `DATABASE_SSL_CA` to its path.
- `ADMIN_EMAIL` and `ADMIN_PASSWORD`: the first administrator's email and a unique password of at least 12 characters. The startup command creates this account once and does not reset an existing account's password.
- `PUBLIC_BASE_URL`: set this after the first deploy to the exact HTTPS URL Render assigns, for example `https://privacy-attendance.onrender.com`. Set it to enable trusted password-reset links.
- Configure `MAIL_HOST`, `MAIL_PORT`, `MAIL_FROM`, and any SMTP credentials in Render if password-reset email is required.

Render generates `SECRET_KEY`; set `QR_SIGNING_SECRET` manually. Because this app also uses that key to encrypt saved TOTP seeds, migrating an existing database requires using the exact current `QR_SIGNING_SECRET` from the local `.env`. Never put it in GitHub or send it in chat. Keep both keys stable across later deploys. The Blueprint enables secure cookies and trusts one Render proxy hop. It runs `flask db upgrade` and `bootstrap-admin` before Gunicorn starts. `/attendance/health` executes `SELECT 1`, so Render reports the service unhealthy if the configured database is unreachable.

### Migrate the current SQLite records

The GitHub repository does not contain your local database. To transfer its rows, first back up the SQLite database and confirm the local schema is at the latest Alembic revision. Create an empty cloud MySQL database, then from PowerShell set its URL for this process only (the prompt masks the value):

```powershell
$secureUrl = Read-Host "MySQL DATABASE_URL" -AsSecureString
$env:DATABASE_URL = [System.Net.NetworkCredential]::new("", $secureUrl).Password
try {
    python -m flask --app run.py db upgrade
    python scripts/migrate_sqlite_to_mysql.py --source "instance\attendance.db" --confirm-empty-target
}
finally {
    Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
}
```

The importer refuses non-SQLite sources, mismatched schemas, and non-empty application tables; it preserves primary keys, copies in foreign-key order, and verifies row counts in a transaction. It intentionally does not copy the Alembic version row because migrations establish the target version. If the import reports a schema mismatch, stop and resolve it rather than forcing the copy. Then configure the same MySQL URL and the existing `QR_SIGNING_SECRET` in Render before creating the web service. Bootstrap will preserve an imported administrator with the same email; use that account's existing password. Private profile-photo files live outside the database and need a separate persistent-disk or object-storage transfer.

The public URL shown in the Render dashboard is the same URL to open on laptops and phones. Use that `https://...onrender.com` URL on both devices; do not use `localhost` on a phone. HTTPS is required for browser camera permission. On the student device, sign in, open **Scan QR**, grant camera permission, and scan the active faculty QR displayed on the laptop. The QR remains HMAC-SHA256 signed, short-lived, nonce-bound, revocable, and protected against duplicate attendance by the database's unique constraint.

Alembic migrations create the schema; the importer above transfers relational records but not private profile-photo files. SQLite's application-managed backup/restore commands do not work with MySQL; configure backups and recovery with the cloud MySQL provider. Render's free filesystem is ephemeral, so private profile-photo uploads require a persistent disk or external object storage to survive deploys/restarts; do not store them in GitHub.

### Local verification before deployment

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m flask --app run.py db upgrade
.\.venv\Scripts\python.exe run.py
```

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
