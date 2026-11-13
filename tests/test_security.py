import hashlib
import sqlite3
import base64
import hmac
import struct
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import create_app
from app.extensions import db
from app.models.models import (
    AuditLog,
    AttendanceRecord,
    AttendanceSession,
    Course,
    Department,
    FacultyProfile,
    FacultySubjectAssignment,
    LoginAttempt,
    PasswordResetToken,
    QRToken,
    StudentEnrollment,
    StudentProfile,
    Subject,
    User,
    UserSession,
)
from app.services.attendance_analytics import calculate_attendance_percentage
from app.services.backup_service import create_backup
from app.services.csv_service import safe_csv_cell
from app.services.password_reset_service import issue_password_reset
from app.services.qr_service import generate_qr_payload, sign_payload, verify_qr_payload
from app.services.totp_service import encrypt_secret, generate_secret


def _current_totp_code(secret):
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    counter = int(time.time() // 30)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


@pytest.fixture
def app_seed():
    app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "WTF_CSRF_ENABLED": False,
    })
    with app.app_context():
        db.create_all()

        department = Department(name="Computer Science", code="CS")
        db.session.add(department)
        db.session.flush()

        course = Course(name="BSc Computer Science", code="BSC-CS", department_id=department.id)
        db.session.add(course)
        db.session.flush()

        subject = Subject(name="Data Structures", code="CS101", course_id=course.id)
        db.session.add(subject)
        db.session.flush()

        faculty_user = User(email="faculty@example.com", full_name="Faculty One", role="faculty")
        faculty_user.password = "Faculty123!"
        student_user = User(email="student@example.com", full_name="Student One", role="student")
        student_user.password = "Student123!"
        db.session.add_all([faculty_user, student_user])
        db.session.flush()

        faculty_profile = FacultyProfile(user_id=faculty_user.id, department_id=department.id)
        student_profile = StudentProfile(user_id=student_user.id, department_id=department.id, approved=True, status="approved")
        db.session.add_all([faculty_profile, student_profile])
        db.session.flush()

        db.session.add(FacultySubjectAssignment(faculty_id=faculty_profile.id, subject_id=subject.id))
        db.session.add(StudentEnrollment(student_id=student_profile.id, subject_id=subject.id))

        active_session = AttendanceSession(title="Algorithms Lab", subject_id=subject.id, faculty_id=faculty_profile.id, status="active")
        closed_session = AttendanceSession(title="Algorithms Test", subject_id=subject.id, faculty_id=faculty_profile.id, status="finalized")
        db.session.add_all([active_session, closed_session])
        db.session.flush()

        db.session.add(AttendanceRecord(session_id=closed_session.id, student_id=student_profile.id, status="present"))
        db.session.commit()
        subject_id = subject.id
        student_profile_id = student_profile.id
        student_user_id = student_profile.user_id
        active_session_id = active_session.id
        closed_session_id = closed_session.id

    yield (
        app,
        SimpleNamespace(id=subject_id),
        SimpleNamespace(id=student_profile_id, user_id=student_user_id),
        SimpleNamespace(id=active_session_id),
        SimpleNamespace(id=closed_session_id),
    )
    with app.app_context():
        db.session.remove()
        db.drop_all()


def test_qr_signature_validation(app_seed):
    app, subject, student_profile, active_session, closed_session = app_seed
    with app.app_context():
        payload = generate_qr_payload(active_session.id)
        verified = verify_qr_payload(payload)
        assert verified[0].id == active_session.id

        tampered = dict(payload)
        tampered["sig"] = "bad-signature"
        try:
            verify_qr_payload(tampered)
        except ValueError:
            pass
        else:
            raise AssertionError("Tampered QR payload should fail validation")


def test_qr_nonce_is_bound_to_stored_token(app_seed):
    app, subject, student_profile, active_session, closed_session = app_seed
    with app.app_context():
        payload = generate_qr_payload(active_session.id)
        payload["nonce"] = "different-nonce"
        payload["sig"] = sign_payload({key: value for key, value in payload.items() if key != "sig"})
        with pytest.raises(ValueError, match="nonce"):
            verify_qr_payload(payload)


def test_qr_rejects_future_issued_token(app_seed):
    app, subject, student_profile, active_session, closed_session = app_seed
    with app.app_context():
        payload = generate_qr_payload(active_session.id)
        token_record = QRToken.query.filter_by(token_id=payload["token_id"]).one()
        payload["iat"] = int(datetime.now(timezone.utc).timestamp()) + 60
        payload["exp"] = payload["iat"] + app.config["QR_TOKEN_TTL_SECONDS"]
        token_record.issued_at = datetime.utcfromtimestamp(payload["iat"])
        token_record.expires_at = datetime.utcfromtimestamp(payload["exp"])
        payload["sig"] = sign_payload({key: value for key, value in payload.items() if key != "sig"})

        with pytest.raises(ValueError, match="future"):
            verify_qr_payload(payload)


def test_admin_controls_are_admin_only_and_revoke_sessions(app_seed):
    app, subject, student_profile, active_session, closed_session = app_seed
    with app.app_context():
        admin = User(email="admin@example.com", full_name="Admin", role="admin")
        admin.password = "AdminPassword123!"
        db.session.add(admin)
        db.session.commit()
        admin_id = admin.id
        student_user_id = User.query.filter_by(email="student@example.com").one().id
        payload = generate_qr_payload(active_session.id)
        qr_token_id = QRToken.query.filter_by(token_id=payload["token_id"]).one().id

    admin_client = app.test_client()
    admin_login = admin_client.post("/login", data={"email": "admin@example.com", "password": "AdminPassword123!"})
    assert admin_login.status_code == 302
    assert admin_login.headers["Location"].endswith("/admin/dashboard")
    admin_workspace = admin_client.get("/admin/dashboard")
    assert admin_workspace.status_code == 200
    assert b"Dashboard" not in admin_workspace.data
    assert b"Student Management" in admin_workspace.data
    assert b"Faculty Management" in admin_workspace.data
    attendance_report = admin_client.get("/admin/attendance.csv")
    assert attendance_report.status_code == 200
    assert b"checked_in_utc,student,student_number,faculty,subject,session,status,verification" in attendance_report.data
    assert b"Student One" in attendance_report.data
    audit_export = admin_client.get("/admin/audit.csv")
    assert audit_export.status_code == 200
    assert b"timestamp_utc,role,event_type" in audit_export.data
    assert b"password_hash" not in audit_export.data
    revoked = admin_client.post(f"/admin/qr-token/{qr_token_id}/revoke")
    assert revoked.status_code == 302
    with app.app_context():
        with pytest.raises(ValueError, match="no longer active"):
            verify_qr_payload(payload)

    student_client = app.test_client()
    student_login = student_client.post("/login", data={"email": "student@example.com", "password": "Student123!"})
    assert student_login.status_code == 302
    with student_client.session_transaction() as student_session:
        session_auth_version = student_session.get("auth_version")
    assert session_auth_version == 0
    with app.app_context():
        user = db.session.get(User, student_user_id)
        suspension = admin_client.post(f"/admin/user/{user.id}/status", data={"status": "suspended"})
        suspended_user = db.session.get(User, student_user_id)
        assert suspension.status_code == 302
        assert suspended_user.is_active is False
        assert suspended_user.auth_version == 1
    with student_client.session_transaction() as student_session:
        assert student_session.get("_user_id") == str(student_user_id)
        assert student_session.get("auth_version") == 0
    student_dashboard = student_client.get("/student/dashboard")
    assert student_dashboard.status_code == 302, student_dashboard.get_data(as_text=True)
    assert admin_client.post(f"/admin/user/{admin_id}/status", data={"status": "suspended"}).status_code == 302


def test_admin_can_edit_account_without_changing_role_and_invalidates_sessions(app_seed):
    app, subject, student_profile, active_session, closed_session = app_seed
    with app.app_context():
        admin = User(email="admin@example.com", full_name="Admin", role="admin")
        admin.password = "AdminPassword123!"
        db.session.add(admin)
        db.session.commit()
        target = User.query.filter_by(email="student@example.com").one()
        target_id = target.id
        department_id = Department.query.one().id

    client = app.test_client()
    assert client.post("/login", data={"email": "admin@example.com", "password": "AdminPassword123!"}).status_code == 302
    response = client.post(
        f"/admin/user/{target_id}/edit",
        data={
            "full_name": "Student Updated",
            "email": "student.updated@example.com",
            "department_id": str(department_id),
            "student_number": "REG-1001",
            "year": "Year 3",
        },
    )
    assert response.status_code == 302

    with app.app_context():
        target = db.session.get(User, target_id)
        assert target.full_name == "Student Updated"
        assert target.email == "student.updated@example.com"
        assert target.role == "student"
        assert target.auth_version == 1
        assert target.student_profile.student_number == "REG-1001"
        assert target.student_profile.year == "Year 3"


def test_csv_cells_neutralize_spreadsheet_formulas():
    assert safe_csv_cell("=1+1") == "'=1+1"
    assert safe_csv_cell("@SUM(A1:A2)") == "'@SUM(A1:A2)"
    assert safe_csv_cell("  =1+1") == "'  =1+1"
    assert safe_csv_cell("ordinary value") == "ordinary value"


def test_attendance_percentage_counts_eligible_sessions_only(app_seed):
    app, subject, student_profile, active_session, closed_session = app_seed
    with app.app_context():
        percentage = calculate_attendance_percentage(student_profile.id, subject.id)
        assert percentage == 100.0


def test_student_dashboard_subject_history_and_pdf_report_are_private(app_seed):
    app, subject, student_profile, active_session, closed_session = app_seed
    client = app.test_client()
    login = client.post("/login", data={"email": "student@example.com", "password": "Student123!"})
    assert login.status_code == 302

    dashboard = client.get("/student/dashboard")
    assert dashboard.status_code == 200
    assert b"QR attendance history" in dashboard.data
    assert b"Account Protected:" not in dashboard.data

    subject_page = client.get(f"/student/subject/{subject.id}")
    assert subject_page.status_code == 200
    assert b"Complete date-wise attendance history" in subject_page.data

    report = client.get("/student/report.pdf")
    assert report.status_code == 200
    assert report.content_type == "application/pdf"
    assert report.data.startswith(b"%PDF")
    assert report.headers["Cache-Control"] == "private, no-store"

    with app.app_context():
        current_user = db.session.get(User, student_profile.user_id)
        outsider = User(email="outsider@example.com", full_name="Outsider", role="student")
        outsider.password = "OutsiderPassword123!"
        db.session.add(outsider)
        db.session.flush()
        outsider_profile = StudentProfile(user_id=outsider.id, approved=True, status="approved")
        db.session.add(outsider_profile)
        db.session.commit()

    outsider_client = app.test_client()
    outsider_login = outsider_client.post(
        "/login",
        data={"email": "outsider@example.com", "password": "OutsiderPassword123!"},
    )
    assert outsider_login.status_code == 302
    assert outsider_client.get(f"/student/subject/{subject.id}").status_code == 404
    assert outsider_client.get(f"/profile/photo/{student_profile.user_id}").status_code == 404

    with app.app_context():
        session_count = UserSession.query.filter_by(user_id=student_profile.user_id, revoked_at=None).count()
        assert session_count == 1


def test_two_factor_login_is_required_and_code_is_single_use(app_seed):
    app, subject, student_profile, active_session, closed_session = app_seed
    secret = generate_secret()
    with app.app_context():
        user = db.session.get(User, student_profile.user_id)
        user.totp_secret = encrypt_secret(secret)
        user.totp_enabled = True
        db.session.commit()

    client = app.test_client()
    login = client.post("/login", data={"email": "student@example.com", "password": "Student123!"})
    assert login.status_code == 302
    assert login.headers["Location"].endswith("/login/two-factor")
    assert client.get("/student/dashboard").status_code == 302

    code = _current_totp_code(secret)
    verified = client.post("/login/two-factor", data={"code": code})
    assert verified.status_code == 302
    assert verified.headers["Location"].endswith("/student/dashboard")
    assert client.get("/student/dashboard").status_code == 200

    second_login = client.post("/login", data={"email": "student@example.com", "password": "Student123!"})
    assert second_login.headers["Location"].endswith("/login/two-factor")
    reused = client.post("/login/two-factor", data={"code": code})
    assert reused.status_code == 200
    assert b"already used" in reused.data


def test_logout_all_revokes_server_tracked_sessions(app_seed):
    app, subject, student_profile, active_session, closed_session = app_seed
    first_client = app.test_client()
    second_client = app.test_client()
    for client in (first_client, second_client):
        login = client.post("/login", data={"email": "student@example.com", "password": "Student123!"})
        assert login.status_code == 302

    profile_page = first_client.get("/profile")
    assert profile_page.status_code == 200
    assert b"Active sessions on this account: 2" in profile_page.data

    logout_all = first_client.post("/profile", data={"action": "logout_all"})
    assert logout_all.status_code == 302
    assert second_client.get("/student/dashboard").status_code == 302
    with app.app_context():
        assert UserSession.query.filter_by(user_id=student_profile.user_id, revoked_at=None).count() == 0


def test_admin_announcements_are_visible_to_students_and_admin_only(app_seed):
    app, subject, student_profile, active_session, closed_session = app_seed
    with app.app_context():
        admin = User(email="announcements.admin@example.com", full_name="Admin", role="admin")
        admin.password = "AdminPassword123!"
        db.session.add(admin)
        db.session.commit()

    admin_client = app.test_client()
    admin_login = admin_client.post(
        "/login",
        data={"email": "announcements.admin@example.com", "password": "AdminPassword123!"},
    )
    assert admin_login.status_code == 302
    start_time = (datetime.utcnow() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
    created = admin_client.post(
        "/admin/announcement/create",
        data={
            "title": "Cyber Security Lab",
            "message": "Bring your student ID to the lab.",
            "starts_at": start_time,
        },
    )
    assert created.status_code == 302

    student_client = app.test_client()
    student_login = student_client.post(
        "/login",
        data={"email": "student@example.com", "password": "Student123!"},
    )
    assert student_login.status_code == 302
    dashboard = student_client.get("/student/dashboard")
    assert b"Cyber Security Lab" in dashboard.data
    assert b"Upcoming announcement" in dashboard.data

    forbidden = student_client.post(
        "/admin/announcement/create",
        data={"title": "Not allowed", "message": "Students cannot publish."},
    )
    assert forbidden.status_code == 302
    with app.app_context():
        assert AuditLog.query.filter_by(event_type="announcement_created").count() == 1


def test_student_can_enroll_and_enable_two_factor_with_verified_code(app_seed):
    app, subject, student_profile, active_session, closed_session = app_seed
    client = app.test_client()
    login = client.post("/login", data={"email": "student@example.com", "password": "Student123!"})
    assert login.status_code == 302

    setup = client.post(
        "/profile",
        data={"action": "totp_setup", "current_password": "Student123!"},
    )
    assert setup.status_code == 302
    with app.app_context():
        user = db.session.get(User, student_profile.user_id)
        assert user.totp_secret
        assert user.totp_enabled is False
        from app.services.totp_service import decrypt_secret

        secret = decrypt_secret(user.totp_secret)

    profile_page = client.get("/profile")
    assert profile_page.status_code == 200
    assert b"Authenticator setup QR code" in profile_page.data

    enabled = client.post(
        "/profile",
        data={"action": "totp_enable", "code": _current_totp_code(secret)},
    )
    assert enabled.status_code == 302
    with app.app_context():
        user = db.session.get(User, student_profile.user_id)
        assert user.totp_enabled is True
        assert user.totp_last_counter is not None


def test_login_lockout_does_not_store_attempted_email(app_seed):
    app, subject, student_profile, active_session, closed_session = app_seed
    client = app.test_client()
    attempted_email = "student@example.com"

    for _ in range(app.config["LOGIN_FAILURE_LIMIT"]):
        response = client.post("/login", data={"email": attempted_email, "password": "IncorrectPassword!"})
        assert response.status_code == 200

    locked_login = client.post("/login", data={"email": attempted_email, "password": "Student123!"})
    assert locked_login.status_code == 200
    with app.app_context():
        attempt = LoginAttempt.query.one()
        assert attempt.locked_until is not None
        assert attempted_email not in attempt.identifier_hash
        assert AuditLog.query.filter_by(event_type="login_failure").count() == app.config["LOGIN_FAILURE_LIMIT"] + 1
        assert all(attempted_email not in (event.details or "") for event in AuditLog.query.all())


def test_password_reset_is_hashed_single_use_and_changes_password(app_seed):
    app, subject, student_profile, active_session, closed_session = app_seed
    with app.app_context():
        user = db.session.get(User, student_profile.user_id)
        raw_token = issue_password_reset(user)
        reset = PasswordResetToken.query.one()
        assert reset.token_hash == hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        assert raw_token not in reset.token_hash

    client = app.test_client()
    path = "/password-reset"
    first_use = client.post(path, data={"token": raw_token, "password": "UpdatedStudentPass123!", "confirm_password": "UpdatedStudentPass123!"})
    assert first_use.status_code == 302
    replay = client.post(path, data={"token": raw_token, "password": "AnotherUpdatedPass123!", "confirm_password": "AnotherUpdatedPass123!"})
    assert replay.status_code == 302
    with app.app_context():
        user = db.session.get(User, student_profile.user_id)
        assert user.check_password("UpdatedStudentPass123!")
        assert not user.check_password("AnotherUpdatedPass123!")


def test_password_reset_rejects_expired_token(app_seed):
    app, subject, student_profile, active_session, closed_session = app_seed
    with app.app_context():
        user = db.session.get(User, student_profile.user_id)
        raw_token = issue_password_reset(user)
        reset = PasswordResetToken.query.one()
        reset.expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.session.commit()

    response = app.test_client().post(
        "/password-reset",
        data={"token": raw_token, "password": "UpdatedStudentPass123!", "confirm_password": "UpdatedStudentPass123!"},
    )
    assert response.status_code == 302


def test_audit_events_include_role_and_request_correlation(app_seed):
    app, subject, student_profile, active_session, closed_session = app_seed
    with app.app_context():
        event = AuditLog.create_event(
            student_profile.user_id,
            "security_test",
            details="  sanitized\n event  ",
            success=False,
            correlation_id="test-correlation-id",
        )
        assert event.role == "student"
        assert event.success is False
        assert event.correlation_id == "test-correlation-id"
        assert event.details == "sanitized event"


def test_backup_uses_sqlite_copy_and_passes_integrity(tmp_path):
    database_path = tmp_path / "test-attendance.db"
    app = create_app({
        "TESTING": True,
        "SECRET_KEY": "backup-test-secret",
        "QR_SIGNING_SECRET": "backup-qr-secret",
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database_path.as_posix()}",
        "WTF_CSRF_ENABLED": False,
    })
    with app.app_context():
        db.create_all()
        department = Department(name="Backup Test", code="BT")
        db.session.add(department)
        db.session.commit()
        backup_path = create_backup()
        connection = sqlite3.connect(backup_path)
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT COUNT(*) FROM departments").fetchone()[0] == 1
        connection.close()
