import hashlib
import secrets
import smtplib
import ssl
from datetime import datetime, timedelta
from email.message import EmailMessage
from urllib.parse import urljoin

from flask import current_app

from app.extensions import db
from app.models.models import PasswordResetToken


def issue_password_reset(user):
    raw_token = secrets.token_urlsafe(32)
    token_digest = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    now = datetime.utcnow()
    PasswordResetToken.query.filter_by(user_id=user.id, used_at=None).update({"used_at": now})
    reset = PasswordResetToken(
        user_id=user.id,
        token_hash=token_digest,
        expires_at=now + timedelta(minutes=current_app.config["PASSWORD_RESET_TTL_MINUTES"]),
    )
    db.session.add(reset)
    db.session.commit()
    return raw_token


def send_password_reset(user, raw_token):
    host = current_app.config.get("MAIL_HOST")
    sender = current_app.config.get("MAIL_FROM")
    public_base_url = current_app.config.get("PUBLIC_BASE_URL")
    if not host or not sender or not public_base_url:
        return False

    reset_url = urljoin(public_base_url.rstrip("/") + "/", "password-reset") + f"#{raw_token}"
    message = EmailMessage()
    message["Subject"] = "Reset your Privacy Attendance password"
    message["From"] = sender
    message["To"] = user.email
    message.set_content(
        "A password reset was requested for your account. "
        f"Use this one-time link within {current_app.config['PASSWORD_RESET_TTL_MINUTES']} minutes:\n\n"
        f"{reset_url}\n\nIf you did not request this change, ignore this message."
    )

    try:
        with smtplib.SMTP(host, current_app.config["MAIL_PORT"], timeout=10) as server:
            if current_app.config["MAIL_USE_TLS"]:
                server.starttls(context=ssl.create_default_context())
            username = current_app.config.get("MAIL_USERNAME")
            password = current_app.config.get("MAIL_PASSWORD")
            if username and password:
                server.login(username, password)
            server.send_message(message)
        return True
    except (OSError, smtplib.SMTPException):
        return False