import os
import secrets
from flask import Flask, flash, g, jsonify, redirect, render_template, request, session, url_for
from flask_login import current_user
from flask_wtf.csrf import CSRFError
from werkzeug.middleware.proxy_fix import ProxyFix

from app.config import Config
from app.extensions import db, login_manager, migrate, csrf, limiter


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(Config)

    if test_config:
        app.config.from_mapping(test_config)

    if (
        not app.testing
        and app.config.get("REQUIRE_DATABASE_URL")
        and app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite:")
    ):
        raise RuntimeError("A persistent MySQL DATABASE_URL is required for this deployment.")
    if app.testing:
        if not app.config.get("SECRET_KEY"):
            app.config["SECRET_KEY"] = secrets.token_urlsafe(48)
        if not app.config.get("QR_SIGNING_SECRET"):
            app.config["QR_SIGNING_SECRET"] = secrets.token_urlsafe(48)
    if not app.config.get("SECRET_KEY") or not app.config.get("QR_SIGNING_SECRET"):
        raise RuntimeError("SECRET_KEY and QR_SIGNING_SECRET must be configured outside tests.")
    if not app.testing and (
        len(app.config["SECRET_KEY"]) < 32
        or len(app.config["QR_SIGNING_SECRET"]) < 32
        or app.config["SECRET_KEY"].lower().startswith(("change-me", "replace-with"))
        or app.config["QR_SIGNING_SECRET"].lower().startswith(("generate-", "replace-with"))
        or app.config["SECRET_KEY"] == app.config["QR_SIGNING_SECRET"]
    ):
        raise RuntimeError("Configure distinct random SECRET_KEY and QR_SIGNING_SECRET values of at least 32 characters.")
    if not 1 <= app.config["QR_TOKEN_TTL_SECONDS"] <= 300:
        raise RuntimeError("QR_TOKEN_TTL_SECONDS must be between 1 and 300.")
    if not 0 <= app.config["ATTENDANCE_ELIGIBILITY_THRESHOLD"] <= 100:
        raise RuntimeError("ATTENDANCE_ELIGIBILITY_THRESHOLD must be between 0 and 100.")
    app.config["RATELIMIT_DEFAULT"] = app.config["RATE_LIMIT_DEFAULT"]
    app.config["APP_BACKUP_SUPPORTED"] = app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite:")
    if app.config.get("TRUST_PROXY"):
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    csrf.init_app(app)
    migrate.init_app(app, db)
    limiter.init_app(app)

    from app.auth.routes import auth_bp
    from app.admin.routes import admin_bp
    from app.faculty.routes import faculty_bp
    from app.student.routes import student_bp
    from app.attendance.routes import attendance_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(faculty_bp, url_prefix="/faculty")
    app.register_blueprint(student_bp, url_prefix="/student")
    app.register_blueprint(attendance_bp, url_prefix="/attendance")

    @login_manager.user_loader
    def load_user(user_id):
        from datetime import datetime, timedelta

        from app.models.models import User, UserSession

        user = db.session.get(User, int(user_id))
        session_id = session.get("server_session_id")
        active_session = db.session.get(UserSession, session_id) if session_id else None
        session_version = session.get("auth_version")
        if (
            user is None
            or not user.is_active
            or active_session is None
            or active_session.user_id != user.id
            or active_session.revoked_at is not None
            or active_session.expires_at <= datetime.utcnow()
            or active_session.auth_version != user.auth_version
            or session_version is None
            or session_version != user.auth_version
        ):
            return None
        if active_session.last_seen_at < datetime.utcnow() - timedelta(minutes=1):
            active_session.last_seen_at = datetime.utcnow()
            db.session.commit()
        return user

    @app.before_request
    def assign_request_id():
        g.request_id = secrets.token_hex(16)

    @app.after_request
    def add_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(self), microphone=(), geolocation=()")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' https://cdn.jsdelivr.net; style-src 'self'; "
            "img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; "
            "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'",
        )
        response.headers["X-Request-ID"] = getattr(g, "request_id", "")
        if app.config.get("SESSION_COOKIE_SECURE"):
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if request.path.endswith("/qr") or request.path.endswith("/submit-attendance"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/")
    def index():
        if current_user.is_authenticated:
            return redirect(url_for(current_user.get_dashboard_url()))
        return redirect(url_for("auth.login"))

    @app.cli.command("bootstrap-admin")
    def bootstrap_admin():
        import click
        from app.models.models import User

        email = os.getenv("ADMIN_EMAIL", "").strip().lower()
        password = os.getenv("ADMIN_PASSWORD", "")
        if not email or len(password) < 12 or password.lower().startswith(("change-me", "replace-with")):
            raise click.ClickException("Set ADMIN_EMAIL and a unique ADMIN_PASSWORD of at least 12 characters in .env.")
        user = User.query.filter_by(email=email).first()
        if user is None:
            user = User(email=email, full_name="System Administrator", role="admin")
            user.password = password
            user.is_active = True
            db.session.add(user)
            db.session.commit()
            print("Administrator account created.")
        else:
            print("Administrator account already exists.")

    @app.cli.command("seed-demo-data")
    def seed_demo_data():
        from scripts.seed_demo_data import seed_demo_data as seed

        seed()
        print("Demo data loaded.")

    @app.cli.command("create-backup")
    def create_database_backup():
        from app.models.models import AuditLog, DatabaseBackupRecord
        from app.services.backup_service import create_backup

        backup_path = create_backup()
        filename = os.path.basename(backup_path)
        db.session.add(DatabaseBackupRecord(filename=filename, status="verified", integrity_result="ok"))
        db.session.commit()
        AuditLog.create_event(None, "backup_created", details=f"Backup verified: {filename}.", role="admin")
        print(f"Backup verified: {filename}")

    @app.cli.command("restore-backup")
    def restore_database_backup():
        import click
        from pathlib import Path

        from app.models.models import AuditLog
        from app.services.backup_service import restore_backup

        backup_filename = os.getenv("BACKUP_FILENAME", "").strip()
        if not backup_filename or Path(backup_filename).name != backup_filename:
            raise click.ClickException("Set BACKUP_FILENAME to a filename inside instance/backups.")
        if os.getenv("CONFIRM_RESTORE") != "YES":
            raise click.ClickException("Set CONFIRM_RESTORE=YES only after reviewing the verified backup.")
        backup_path = Path(app.instance_path) / "backups" / backup_filename
        restored_path = restore_backup(backup_path, confirm=True)
        AuditLog.create_event(None, "database_restored", details=f"Database restored from {backup_filename}.", role="admin")
        print(f"Database restored and verified: {Path(restored_path).name}")

    @app.errorhandler(403)
    def forbidden_page(error):
        flash("You do not have permission to view that page.", "error")
        return redirect(url_for("index"))

    @app.errorhandler(CSRFError)
    def handle_csrf_error(error):
        message = "The security token is missing or expired. Reload the page and try again."
        if request.is_json or request.path == url_for("student.submit_attendance"):
            return jsonify({"ok": False, "message": message}), 400
        return render_template("auth/csrf_error.html", message=message), 400

    @app.errorhandler(413)
    def request_too_large(error):
        message = "The submitted request is too large."
        if request.is_json:
            return jsonify({"ok": False, "message": message}), 413
        return render_template("auth/csrf_error.html", message=message), 413

    return app
