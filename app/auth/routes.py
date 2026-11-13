import hashlib
import hmac
import base64
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path
import secrets

import qrcode
from PIL import Image, UnidentifiedImageError
from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, send_from_directory, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy import delete, func, or_
from werkzeug.utils import secure_filename

from app.auth.forms import LoginForm, RegistrationForm
from app.extensions import db, limiter
from app.models.models import AuditLog, FacultyProfile, LoginAttempt, PasswordResetToken, StudentProfile, User, UserSession
from app.services.password_reset_service import issue_password_reset, send_password_reset
from app.services.totp_service import decrypt_secret, encrypt_secret, generate_secret, matching_counter, provisioning_uri


auth_bp = Blueprint("auth", __name__)


def _complete_login(user):
    session.clear()
    session.permanent = True
    login_user(user, fresh=True)
    session["auth_version"] = user.auth_version
    session_id = secrets.token_urlsafe(32)
    session["server_session_id"] = session_id
    user.last_login = datetime.utcnow()
    now = datetime.utcnow()
    db.session.add_all([
        user,
        UserSession(
            session_id=session_id,
            user_id=user.id,
            auth_version=user.auth_version,
            created_at=now,
            last_seen_at=now,
            expires_at=now + current_app.config["PERMANENT_SESSION_LIFETIME"],
        ),
    ])
    db.session.commit()
    AuditLog.create_event(user.id, "login_success", details="User logged in.", success=True)


def _profile_totp_qr(user):
    if not user.totp_secret or user.totp_enabled:
        return None
    secret = decrypt_secret(user.totp_secret)
    image = qrcode.make(provisioning_uri(secret, user.email))
    output = BytesIO()
    image.save(output, format="PNG")
    return base64.b64encode(output.getvalue()).decode("ascii")


@auth_bp.route("/login", methods=["GET", "POST"])
@limiter.limit(lambda: current_app.config["RATE_LIMIT_AUTH"])
def login():
    form = LoginForm()
    if request.method == "POST" and form.validate_on_submit():
        identifier = form.email.data.strip()
        normalized_identifier = identifier.lower()
        identifier_hash = hmac.new(
            current_app.config["SECRET_KEY"].encode("utf-8"),
            normalized_identifier.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        now = datetime.utcnow()
        attempt = db.session.get(LoginAttempt, identifier_hash)
        locked = attempt is not None and attempt.locked_until is not None and attempt.locked_until > now
        user = User.query.filter_by(email=normalized_identifier).first()
        if user is None:
            user = (
                User.query.outerjoin(StudentProfile, StudentProfile.user_id == User.id)
                .outerjoin(FacultyProfile, FacultyProfile.user_id == User.id)
                .filter(or_(
                    func.lower(StudentProfile.student_number) == normalized_identifier,
                    func.lower(FacultyProfile.employee_id) == normalized_identifier,
                ))
                .first()
            )
        if not locked and user and user.check_password(form.password.data) and user.is_active:
            if attempt is not None:
                db.session.delete(attempt)
            db.session.commit()
            if user.totp_enabled:
                if user.totp_secret:
                    session.clear()
                    session["pending_totp_user_id"] = user.id
                    session["pending_totp_since"] = int(now.timestamp())
                    return redirect(url_for("auth.verify_totp"))
            else:
                _complete_login(user)
                flash("Login successful.", "success")
                return redirect(url_for(user.get_dashboard_url()))

        if not locked:
            if attempt is None:
                attempt = LoginAttempt(identifier_hash=identifier_hash, failed_attempts=0, window_started_at=now)
                db.session.add(attempt)
            if now - attempt.window_started_at > timedelta(minutes=current_app.config["LOGIN_LOCKOUT_MINUTES"]):
                attempt.failed_attempts = 0
                attempt.window_started_at = now
            attempt.failed_attempts += 1
            if attempt.failed_attempts >= current_app.config["LOGIN_FAILURE_LIMIT"]:
                attempt.locked_until = now + timedelta(minutes=current_app.config["LOGIN_LOCKOUT_MINUTES"])
            db.session.commit()
        AuditLog.create_event(
            user.id if user else None,
            "login_failure",
            details="Invalid credentials or temporarily locked account.",
            success=False,
            role=user.role if user else None,
        )
        flash("Invalid credentials or inactive account.", "error")

    return render_template("auth/login.html", form=form)


@auth_bp.route("/login/two-factor", methods=["GET", "POST"])
@limiter.limit("10 per minute")
def verify_totp():
    user_id = session.get("pending_totp_user_id")
    pending_since = session.get("pending_totp_since")
    if not user_id or not pending_since or datetime.utcnow().timestamp() - pending_since > 300:
        session.clear()
        flash("Two-factor sign-in expired. Please sign in again.", "error")
        return redirect(url_for("auth.login"))

    user = db.session.get(User, user_id)
    if user is None or not user.is_active or not user.totp_enabled or not user.totp_secret:
        session.clear()
        flash("Two-factor sign-in could not be completed. Please sign in again.", "error")
        return redirect(url_for("auth.login"))

    if request.method == "POST":
        code = (request.form.get("code") or "").strip()
        counter = matching_counter(decrypt_secret(user.totp_secret), code)
        if counter is not None and (user.totp_last_counter is None or counter > user.totp_last_counter):
            user.totp_last_counter = counter
            db.session.commit()
            _complete_login(user)
            flash("Login successful.", "success")
            return redirect(url_for(user.get_dashboard_url()))

        AuditLog.create_event(user.id, "totp_login_failure", details="Invalid or reused two-factor code.", success=False)
        flash("The verification code is invalid or was already used.", "error")

    return render_template("auth/verify_totp.html")


@auth_bp.route("/register", methods=["GET", "POST"])
@limiter.limit("10/minute")
def register():
    form = RegistrationForm()
    if request.method == "POST" and form.validate_on_submit():
        email = form.email.data.lower().strip()
        if User.query.filter_by(email=email).first():
            flash("That email is already registered.", "error")
            return render_template("auth/login.html", form=form)

        user = User(email=email, full_name=form.full_name.data, role="student")
        user.password = form.password.data
        user.is_active = True
        db.session.add(user)
        db.session.flush()

        student_profile = StudentProfile(user_id=user.id, approved=False, status="pending")
        db.session.add(student_profile)
        db.session.commit()
        AuditLog.create_event(user.id, "student_registration", details="Student registered and pending approval.")
        flash("Registration submitted. Please wait for admin approval.", "success")
        return redirect(url_for("auth.login"))

    return render_template("auth/login.html", form=form, register_view=True)


@auth_bp.route("/logout", methods=["POST"])
@login_required
def logout():
    user_id = current_user.id
    session_id = session.get("server_session_id")
    active_session = db.session.get(UserSession, session_id) if session_id else None
    if active_session is not None:
        active_session.revoked_at = datetime.utcnow()
        db.session.commit()
    logout_user()
    AuditLog.create_event(user_id, "logout", details="User logged out.")
    flash("Logged out successfully.", "success")
    return redirect(url_for("auth.login"))


@auth_bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    student_profile = current_user.student_profile
    if request.method == "POST":
        action = request.form.get("action", "profile")
        if action == "change_password":
            current_password = request.form.get("current_password") or ""
            new_password = request.form.get("new_password") or ""
            if not current_user.check_password(current_password):
                flash("Current password is incorrect.", "error")
            elif len(new_password) < 12:
                flash("Choose a password with at least 12 characters.", "error")
            elif current_user.check_password(new_password):
                flash("Choose a new password different from your current password.", "error")
            else:
                current_user.password = new_password
                current_user.auth_version += 1
                active_session = db.session.get(UserSession, session.get("server_session_id"))
                if active_session is not None:
                    active_session.auth_version = current_user.auth_version
                db.session.commit()
                session["auth_version"] = current_user.auth_version
                AuditLog.create_event(current_user.id, "password_changed", details="Password changed from profile.")
                flash("Password changed. Other active sessions have been signed out.", "success")
            return redirect(url_for("auth.profile"))

        if action == "logout_all":
            user_id = current_user.id
            user = db.session.get(User, user_id)
            user.auth_version += 1
            UserSession.query.filter_by(user_id=user_id, revoked_at=None).update(
                {UserSession.revoked_at: datetime.utcnow()},
                synchronize_session=False,
            )
            db.session.commit()
            logout_user()
            AuditLog.create_event(user_id, "logout_all_sessions", details="All signed-in sessions were invalidated.")
            flash("All active sessions have been signed out.", "success")
            return redirect(url_for("auth.login"))

        if action == "totp_setup":
            if current_user.totp_enabled:
                flash("Two-factor authentication is already enabled.", "info")
            elif not current_user.check_password(request.form.get("current_password") or ""):
                flash("Current password is incorrect.", "error")
            else:
                current_user.totp_secret = encrypt_secret(generate_secret())
                current_user.totp_last_counter = None
                db.session.commit()
                AuditLog.create_event(current_user.id, "totp_setup_started", details="Two-factor enrollment started.")
                flash("Scan the setup QR code and verify a code to enable two-factor authentication.", "success")
            return redirect(url_for("auth.profile"))

        if action == "totp_enable":
            if not current_user.totp_secret or current_user.totp_enabled:
                flash("Start two-factor setup before enabling it.", "error")
            else:
                counter = matching_counter(
                    decrypt_secret(current_user.totp_secret),
                    (request.form.get("code") or "").strip(),
                )
                if counter is None:
                    flash("That verification code is invalid.", "error")
                else:
                    current_user.totp_enabled = True
                    current_user.totp_last_counter = counter
                    db.session.commit()
                    AuditLog.create_event(current_user.id, "totp_enabled", details="Two-factor authentication enabled.")
                    flash("Two-factor authentication is enabled.", "success")
            return redirect(url_for("auth.profile"))

        if action == "totp_disable":
            code = (request.form.get("code") or "").strip()
            counter = (
                matching_counter(decrypt_secret(current_user.totp_secret), code)
                if current_user.totp_secret
                else None
            )
            if (
                not current_user.totp_enabled
                or not current_user.check_password(request.form.get("current_password") or "")
                or counter is None
                or (current_user.totp_last_counter is not None and counter <= current_user.totp_last_counter)
            ):
                flash("Password or current unused verification code is incorrect.", "error")
            else:
                current_user.totp_enabled = False
                current_user.totp_secret = None
                current_user.totp_last_counter = None
                db.session.commit()
                AuditLog.create_event(current_user.id, "totp_disabled", details="Two-factor authentication disabled.")
                flash("Two-factor authentication has been disabled.", "success")
            return redirect(url_for("auth.profile"))

        full_name = (request.form.get("full_name") or "").strip()
        phone = (request.form.get("phone") or "").strip()
        location = (request.form.get("location") or "").strip()
        bio = (request.form.get("bio") or "").strip()
        privacy_level = request.form.get("privacy_level", "standard")
        values = (full_name, phone, location, bio)
        limits = (255, 40, 120, 2000)
        if not full_name or any(len(value) > limit for value, limit in zip(values, limits)):
            flash("Enter a name and keep profile fields within their length limits.", "error")
            return redirect(url_for("auth.profile"))
        if privacy_level not in {"standard", "limited"}:
            flash("Choose a valid privacy setting.", "error")
            return redirect(url_for("auth.profile"))

        current_user.full_name = full_name
        current_user.phone = phone or None
        current_user.location = location or None
        current_user.bio = bio or None
        current_user.privacy_level = privacy_level

        if student_profile is not None:
            section = (request.form.get("section") or "").strip()
            gender = (request.form.get("gender") or "").strip()
            birth_date_value = (request.form.get("date_of_birth") or "").strip()
            address = (request.form.get("address") or "").strip()
            guardian_name = (request.form.get("guardian_name") or "").strip()
            guardian_contact = (request.form.get("guardian_contact") or "").strip()
            emergency_contact = (request.form.get("emergency_contact") or "").strip()
            student_values = (section, gender, address, guardian_name, guardian_contact, emergency_contact)
            student_limits = (20, 40, 255, 255, 40, 40)
            if any(len(value) > limit for value, limit in zip(student_values, student_limits)):
                flash("One or more student profile fields exceed their length limit.", "error")
                return redirect(url_for("auth.profile"))
            try:
                birth_date = date.fromisoformat(birth_date_value) if birth_date_value else None
            except ValueError:
                flash("Enter a valid date of birth.", "error")
                return redirect(url_for("auth.profile"))
            if birth_date and birth_date >= date.today():
                flash("Date of birth must be in the past.", "error")
                return redirect(url_for("auth.profile"))

            student_profile.section = section or None
            student_profile.gender = gender or None
            student_profile.date_of_birth = birth_date
            student_profile.address = address or None
            student_profile.guardian_name = guardian_name or None
            student_profile.guardian_contact = guardian_contact or None
            student_profile.emergency_contact = emergency_contact or None

        db.session.commit()
        flash("Profile updated.", "success")
        return redirect(url_for("auth.profile"))

    active_sessions = (
        UserSession.query.filter_by(user_id=current_user.id, revoked_at=None)
        .filter(UserSession.expires_at > datetime.utcnow())
        .order_by(UserSession.last_seen_at.desc())
        .all()
    )
    current_session = next(
        (item for item in active_sessions if item.session_id == session.get("server_session_id")),
        None,
    )
    return render_template(
        "auth/profile.html",
        student_profile=student_profile,
        active_sessions=active_sessions,
        current_session=current_session,
        totp_qr_data=_profile_totp_qr(current_user),
        totp_secret_for_display=(
            decrypt_secret(current_user.totp_secret)
            if current_user.totp_secret and not current_user.totp_enabled
            else None
        ),
        security_protected=(
            current_user.is_active
            and current_user.password_hash.startswith("scrypt:")
            and current_user.totp_enabled
            and current_app.config["SESSION_COOKIE_SECURE"]
            and request.is_secure
        ),
    )


@auth_bp.route("/profile/photo", methods=["POST"])
@login_required
def update_profile_photo():
    profile = current_user.student_profile
    photo = request.files.get("profile_photo")
    if profile is None or photo is None or not photo.filename:
        flash("Choose a profile photo to upload.", "error")
        return redirect(url_for("auth.profile"))

    try:
        image = Image.open(photo.stream)
        image.verify()
        photo.stream.seek(0)
        image = Image.open(photo.stream)
        if image.format not in {"JPEG", "PNG", "WEBP"} or image.width * image.height > 20_000_000:
            raise ValueError("Unsupported image format or dimensions.")
        image.load()
    except (UnidentifiedImageError, OSError, ValueError):
        flash("Upload a valid JPEG, PNG, or WebP image under the request size limit.", "error")
        return redirect(url_for("auth.profile"))

    private_dir = Path(current_app.instance_path) / "private-profile-photos"
    private_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{secrets.token_hex(24)}.jpg"
    destination = private_dir / secure_filename(filename)
    image.convert("RGB").save(destination, format="JPEG", quality=88, optimize=True)
    old_photo = profile.profile_photo
    profile.profile_photo = filename
    db.session.commit()
    if old_photo:
        (private_dir / secure_filename(old_photo)).unlink(missing_ok=True)
    AuditLog.create_event(current_user.id, "profile_photo_updated", details="Profile photo updated.")
    flash("Profile photo updated.", "success")
    return redirect(url_for("auth.profile"))


@auth_bp.route("/profile/photo/<int:user_id>")
@login_required
def profile_photo(user_id):
    if current_user.id != user_id:
        abort(404)
    profile = current_user.student_profile
    if profile is None or not profile.profile_photo:
        abort(404)
    response = send_from_directory(
        Path(current_app.instance_path) / "private-profile-photos",
        secure_filename(profile.profile_photo),
        mimetype="image/jpeg",
        max_age=0,
    )
    response.headers["Cache-Control"] = "private, no-store"
    return response


@auth_bp.route("/password-reset/request", methods=["GET", "POST"])
@limiter.limit("5/minute")
def request_password_reset():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        user = User.query.filter_by(email=email).first() if email else None
        if user is not None and user.is_active:
            raw_token = issue_password_reset(user)
            delivered = send_password_reset(user, raw_token)
            if not delivered:
                reset = PasswordResetToken.query.filter_by(
                    token_hash=hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
                ).first()
                if reset is not None:
                    reset.used_at = datetime.utcnow()
                    db.session.commit()
            AuditLog.create_event(
                user.id,
                "password_reset_requested",
                details="Password reset email sent." if delivered else "Password reset delivery unavailable.",
                success=delivered,
            )
        else:
            AuditLog.create_event(None, "password_reset_requested", details="Password reset requested.", success=True)
        flash("If the account is eligible, a password reset link will be sent.", "success")
        return redirect(url_for("auth.login"))
    return render_template("auth/request_reset.html")


@auth_bp.route("/password-reset", methods=["GET", "POST"])
@limiter.limit("10/minute")
def reset_password():
    from app.auth.forms import PasswordResetForm

    token = (request.form.get("token") or "").strip()
    if request.method == "GET":
        return render_template("auth/reset_password.html", form=PasswordResetForm())
    if not token or len(token) > 128:
        flash("This password reset link is invalid or expired.", "error")
        return redirect(url_for("auth.login"))
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    reset = PasswordResetToken.query.filter_by(token_hash=token_hash).first()
    now = datetime.utcnow()
    if reset is None or reset.used_at is not None or reset.expires_at <= now:
        AuditLog.create_event(None, "password_reset_invalid", details="Invalid or expired password reset link.", success=False)
        flash("This password reset link is invalid or expired.", "error")
        return redirect(url_for("auth.login"))

    form = PasswordResetForm()
    if form.validate_on_submit():
        user = db.session.get(User, reset.user_id)
        if user is None or not user.is_active:
            reset.used_at = now
            db.session.commit()
            flash("This password reset link is invalid or expired.", "error")
            return redirect(url_for("auth.login"))
        user.password = form.password.data
        user.auth_version += 1
        reset.used_at = now
        db.session.execute(delete(PasswordResetToken).where(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.id != reset.id,
        ))
        db.session.commit()
        AuditLog.create_event(user.id, "password_reset_completed", details="Password reset completed.", success=True)
        flash("Password updated. Sign in with your new password.", "success")
        return redirect(url_for("auth.login"))
    return render_template("auth/reset_password.html", form=form)
