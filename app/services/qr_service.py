import hashlib
import hmac
import base64
from io import BytesIO
import json
import secrets
from datetime import datetime, timedelta, timezone

from flask import current_app
import qrcode
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.extensions import db
from app.models.models import AttendanceRecord, AttendanceSession, QRToken, StudentEnrollment, User


def _canonicalize(payload):
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _secret():
    return current_app.config["QR_SIGNING_SECRET"].encode("utf-8")


def sign_payload(payload):
    digest = hmac.new(_secret(), _canonicalize(payload), hashlib.sha256).hexdigest()
    return digest


def generate_qr_payload(session_id):
    session = db.session.get(AttendanceSession, session_id)
    if session is None:
        raise ValueError("Session not found")
    if session.status != "active":
        raise ValueError("Session is not active")
    token_id = secrets.token_urlsafe(16)
    nonce = secrets.token_urlsafe(24)
    issued_at = datetime.now(timezone.utc)
    expires_at = issued_at + timedelta(seconds=current_app.config["QR_TOKEN_TTL_SECONDS"])
    payload = {
        "session_id": session_id,
        "faculty_id": session.faculty_id,
        "token_id": token_id,
        "nonce": nonce,
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    payload["sig"] = sign_payload(payload)
    qr_token = QRToken(
        session_id=session_id,
        token_id=token_id,
        nonce=nonce,
        issued_at=issued_at,
        expires_at=expires_at,
        is_active=True,
    )
    QRToken.query.filter_by(session_id=session_id, is_active=True).update({"is_active": False})
    db.session.add(qr_token)
    try:
        db.session.commit()
    except SQLAlchemyError as exc:
        db.session.rollback()
        raise ValueError("Unable to issue a QR code. Please try again.") from exc

    return payload


def generate_qr_image(payload):
    serialized = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    image = qrcode.make(serialized)
    image_buffer = BytesIO()
    image.save(image_buffer, format="PNG")
    image_data = base64.b64encode(image_buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{image_data}"


def verify_qr_payload(raw_payload):
    if isinstance(raw_payload, str):
        raw_payload = json.loads(raw_payload)
    if not isinstance(raw_payload, dict):
        raise ValueError("Malformed QR payload")

    required = {"session_id", "faculty_id", "token_id", "nonce", "iat", "exp", "sig"}
    if set(raw_payload) != required:
        raise ValueError("Malformed QR payload")

    if (
        type(raw_payload["session_id"]) is not int
        or type(raw_payload["faculty_id"]) is not int
        or type(raw_payload["iat"]) is not int
        or type(raw_payload["exp"]) is not int
        or not isinstance(raw_payload["token_id"], str)
        or not isinstance(raw_payload["nonce"], str)
        or not isinstance(raw_payload["sig"], str)
    ):
        raise ValueError("Malformed QR payload")

    provided_sig = raw_payload["sig"]
    payload_without_sig = {k: v for k, v in raw_payload.items() if k != "sig"}
    expected_sig = sign_payload(payload_without_sig)
    if not hmac.compare_digest(provided_sig, expected_sig):
        raise ValueError("Invalid QR signature")

    session_id = raw_payload["session_id"]
    session = db.session.get(AttendanceSession, session_id)
    if session is None:
        raise ValueError("Invalid session")
    if session.status != "active":
        raise ValueError("Session is not active")
    if session.faculty_id != raw_payload["faculty_id"]:
        raise ValueError("QR session reference is invalid")
    if session.subject is None:
        raise ValueError("Subject is invalid")
    if not session.subject.is_active:
        raise ValueError("Subject inactive")
    server_time = int(datetime.now(timezone.utc).timestamp())
    if raw_payload["iat"] > server_time:
        raise ValueError("QR code was issued in the future")
    if raw_payload["exp"] <= server_time:
        raise ValueError("QR code expired")
    if raw_payload["iat"] > raw_payload["exp"]:
        raise ValueError("Invalid timestamps")

    token_record = QRToken.query.filter_by(token_id=raw_payload["token_id"]).first()
    if token_record is None:
        raise ValueError("QR token not recognized")
    if token_record.session_id != session_id:
        raise ValueError("Token does not match the session")
    if token_record.nonce and not hmac.compare_digest(token_record.nonce, raw_payload["nonce"]):
        raise ValueError("QR nonce is invalid")
    if int(token_record.expires_at.replace(tzinfo=timezone.utc).timestamp()) != raw_payload["exp"]:
        raise ValueError("QR expiry does not match the issued token")
    if int(token_record.issued_at.replace(tzinfo=timezone.utc).timestamp()) != raw_payload["iat"]:
        raise ValueError("QR issue time does not match the issued token")
    if not token_record.is_active:
        raise ValueError("QR code is no longer active")

    expiry_dt = token_record.expires_at
    if expiry_dt.tzinfo is None:
        expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)
    if expiry_dt.timestamp() <= datetime.now(timezone.utc).timestamp():
        raise ValueError("Token expired")

    return session, token_record


def validate_student_attendance(student_user: User, payload):
    session, token_record = verify_qr_payload(payload)

    student_profile = student_user.student_profile
    if student_profile is None:
        raise ValueError("Student profile missing")
    if not student_user.is_active:
        raise ValueError("Account inactive")
    if not student_profile.approved:
        raise ValueError("Student account not approved")

    enrolled = StudentEnrollment.query.filter_by(
        student_id=student_profile.id,
        subject_id=session.subject_id,
    ).first()
    if enrolled is None:
        raise ValueError("Student is not enrolled in this subject")

    existing = AttendanceRecord.query.filter_by(
        session_id=session.id,
        student_id=student_profile.id,
    ).first()
    if existing is not None:
        token_record.verification_result = "duplicate"
        db.session.commit()
        return "already-recorded", existing

    record = AttendanceRecord(
        session_id=session.id,
        student_id=student_profile.id,
        token_id=token_record.token_id,
        status="present",
        checked_in_at=datetime.now(timezone.utc),
    )
    db.session.add(record)
    token_record.verification_result = "verified"
    try:
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        existing = AttendanceRecord.query.filter_by(
            session_id=session.id,
            student_id=student_profile.id,
        ).first()
        if existing is not None:
            return "already-recorded", existing
        raise ValueError("Attendance could not be saved. Please try again.") from exc
    except SQLAlchemyError as exc:
        db.session.rollback()
        raise ValueError("Attendance could not be saved. Please try again.") from exc

    saved_record = db.session.get(AttendanceRecord, record.id)
    if saved_record is None:
        raise ValueError("Attendance could not be confirmed. Please try again.")

    return "recorded", saved_record
