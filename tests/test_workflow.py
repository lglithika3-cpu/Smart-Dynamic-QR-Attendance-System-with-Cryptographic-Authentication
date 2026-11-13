import json
import re
import time

import pytest

from app import create_app
from app.extensions import db
from app.models.models import (
    AttendanceCorrectionRequest,
    AttendanceRecord,
    AttendanceSession,
    AuditLog,
    Course,
    Department,
    FacultyProfile,
    FacultySubjectAssignment,
    StudentEnrollment,
    StudentProfile,
    Subject,
    User,
)
from app.services.qr_service import sign_payload
from app.services.attendance_analytics import calculate_attendance_percentage


@pytest.fixture
def workflow_app():
    app = create_app({
        "TESTING": True,
        "SECRET_KEY": "workflow-test-secret",
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "WTF_CSRF_ENABLED": True,
        "RATELIMIT_ENABLED": False,
    })
    with app.app_context():
        db.create_all()
        department = Department(name="Computer Science", code="CS")
        db.session.add(department)
        db.session.flush()
        course = Course(name="Computer Science", code="CS-1", department_id=department.id)
        db.session.add(course)
        db.session.flush()
        subject = Subject(name="Algorithms", code="CS101", course_id=course.id)
        db.session.add(subject)
        db.session.flush()

        faculty = User(email="faculty@example.com", full_name="Faculty One", role="faculty")
        faculty.password = "Faculty123!"
        student_users = [
            User(email="student1@example.com", full_name="Student One", role="student"),
            User(email="student2@example.com", full_name="Student Two", role="student"),
        ]
        for student in student_users:
            student.password = "Student123!"
        db.session.add_all([faculty, *student_users])
        db.session.flush()

        faculty_profile = FacultyProfile(user_id=faculty.id, department_id=department.id)
        db.session.add(faculty_profile)
        db.session.flush()
        db.session.add(FacultySubjectAssignment(faculty_id=faculty_profile.id, subject_id=subject.id))
        for student in student_users:
            profile = StudentProfile(user_id=student.id, department_id=department.id, approved=True, status="approved")
            db.session.add(profile)
            db.session.flush()
            db.session.add(StudentEnrollment(student_id=profile.id, subject_id=subject.id))
        db.session.commit()
    yield app
    with app.app_context():
        db.session.remove()
        db.drop_all()


def _csrf_from_html(html):
    match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    assert match, "Expected a CSRF token in the rendered form"
    return match.group(1)


def _login(client, email, password):
    response = client.get("/login")
    token = _csrf_from_html(response.get_data(as_text=True))
    return client.post(
        "/login",
        data={"csrf_token": token, "email": email, "password": password},
        follow_redirects=False,
    )


def test_csrf_protected_faculty_to_student_attendance_workflow(workflow_app):
    faculty_client = workflow_app.test_client()
    assert _login(faculty_client, "faculty@example.com", "Faculty123!").status_code == 302

    dashboard = faculty_client.get("/faculty/dashboard")
    dashboard_html = dashboard.get_data(as_text=True)
    assert b"Dashboard" not in dashboard.data
    assert b"Student List" in dashboard.data
    with workflow_app.app_context():
        unassigned_student = User(email="unassigned@example.com", full_name="Unassigned Student", role="student")
        unassigned_student.password = "Student123!"
        db.session.add(unassigned_student)
        db.session.flush()
        db.session.add(StudentProfile(user_id=unassigned_student.id, approved=True, status="approved"))
        db.session.commit()
    dashboard = faculty_client.get("/faculty/dashboard")
    dashboard_html = dashboard.get_data(as_text=True)
    assert b"Student One" in dashboard.data
    assert b"Unassigned Student" not in dashboard.data
    csrf_token = _csrf_from_html(dashboard_html)
    with workflow_app.app_context():
        subject_id = Subject.query.first().id

    missing_token = faculty_client.post(
        "/faculty/session/create",
        data={"title": "Morning Lab", "subject_id": subject_id},
    )
    assert missing_token.status_code == 400
    assert b"security token is missing or expired" in missing_token.data

    created = faculty_client.post(
        "/faculty/session/create",
        data={"csrf_token": csrf_token, "title": "Morning Lab", "subject_id": subject_id},
        follow_redirects=True,
    )
    assert created.status_code == 200
    assert b"Session created and attendance is open." in created.data

    repeated_create = faculty_client.post(
        "/faculty/session/create",
        data={"csrf_token": csrf_token, "title": "Morning Lab", "subject_id": subject_id},
        follow_redirects=True,
    )
    assert repeated_create.status_code == 200
    assert b"duplicate submission was ignored" in repeated_create.data

    with workflow_app.app_context():
        session = AttendanceSession.query.filter_by(title="Morning Lab").one()
        assert session.status == "active"
        session_id = session.id
        assert AttendanceSession.query.filter_by(title="Morning Lab").count() == 1

    qr_without_csrf = faculty_client.post(f"/faculty/session/{session_id}/qr")
    assert qr_without_csrf.status_code == 400
    qr_response = faculty_client.post(
        f"/faculty/session/{session_id}/qr",
        headers={"X-CSRFToken": csrf_token},
    )
    assert qr_response.status_code == 200
    qr_data = qr_response.get_json()
    assert qr_data["qr_image"].startswith("data:image/png;base64,")
    qr_token = json.loads(qr_data["token"])
    assert qr_token["exp"] - qr_token["iat"] == workflow_app.config["QR_TOKEN_TTL_SECONDS"]
    assert qr_data["refresh_seconds"] == max(workflow_app.config["QR_TOKEN_TTL_SECONDS"] - 1, 1)
    assert qr_token["faculty_id"]
    assert qr_token["nonce"]

    student_client = workflow_app.test_client()
    assert _login(student_client, "student1@example.com", "Student123!").status_code == 302
    student_dashboard = student_client.get("/student/dashboard")
    assert b"Morning Lab" in student_dashboard.data
    assert b"My Attendance" in student_dashboard.data
    assert b"Dashboard" not in student_dashboard.data
    scan_page = student_client.get("/student/scan")
    assert b"Camera access requires HTTPS or localhost." in scan_page.data
    scan_csrf = re.search(r'<meta name="csrf-token" content="([^"]+)"', scan_page.get_data(as_text=True)).group(1)

    invalid_payload = json.loads(qr_data["token"])
    invalid_payload["sig"] = "invalid-signature"
    invalid = student_client.post(
        "/student/submit-attendance",
        json={"token": json.dumps(invalid_payload)},
        headers={"X-CSRFToken": scan_csrf},
    )
    assert invalid.status_code == 400
    assert invalid.get_json()["ok"] is False

    expired_payload = json.loads(qr_data["token"])
    expired_payload["exp"] = int(time.time()) - 1
    with workflow_app.app_context():
        expired_payload["sig"] = sign_payload({key: value for key, value in expired_payload.items() if key != "sig"})
    expired = student_client.post(
        "/student/submit-attendance",
        json={"token": json.dumps(expired_payload)},
        headers={"X-CSRFToken": scan_csrf},
    )
    assert expired.status_code == 400
    assert "expired" in expired.get_json()["message"].lower()

    attendance = student_client.post(
        "/student/submit-attendance",
        json={"token": qr_data["token"]},
        headers={"X-CSRFToken": scan_csrf},
    )
    assert attendance.status_code == 200
    assert attendance.get_json()["ok"] is True

    duplicate = student_client.post(
        "/student/submit-attendance",
        json={"token": qr_data["token"]},
        headers={"X-CSRFToken": scan_csrf},
    )
    assert duplicate.status_code == 200
    assert "already recorded" in duplicate.get_json()["message"].lower()

    second_student = workflow_app.test_client()
    assert _login(second_student, "student2@example.com", "Student123!").status_code == 302
    second_scan = second_student.get("/student/scan").get_data(as_text=True)
    second_csrf = re.search(r'<meta name="csrf-token" content="([^"]+)"', second_scan).group(1)
    second_attendance = second_student.post(
        "/student/submit-attendance",
        json={"token": qr_data["token"]},
        headers={"X-CSRFToken": second_csrf},
    )
    assert second_attendance.status_code == 200
    assert second_attendance.get_json()["ok"] is True

    with workflow_app.app_context():
        assert AttendanceRecord.query.filter_by(session_id=session_id).count() == 2

    student_dashboard_html = student_client.get("/student/dashboard").get_data(as_text=True)
    student_csrf = _csrf_from_html(student_dashboard_html)
    with workflow_app.app_context():
        own_record = AttendanceRecord.query.filter(AttendanceRecord.session_id == session_id).join(StudentProfile).filter(StudentProfile.user_id == User.query.filter_by(email="student1@example.com").one().id).one()
        other_record = AttendanceRecord.query.filter(AttendanceRecord.session_id == session_id).join(StudentProfile).filter(StudentProfile.user_id == User.query.filter_by(email="student2@example.com").one().id).one()
        own_record_id = own_record.id
        other_record_id = other_record.id
    correction = student_client.post(
        f"/student/attendance/{own_record_id}/correction",
        data={"csrf_token": student_csrf, "reason": "Was present in class."},
        follow_redirects=True,
    )
    assert correction.status_code == 200
    cross_student_correction = student_client.post(
        f"/student/attendance/{other_record_id}/correction",
        data={"csrf_token": student_csrf, "reason": "Attempt to modify another record."},
    )
    assert cross_student_correction.status_code == 404

    faculty_dashboard_html = faculty_client.get("/faculty/dashboard").get_data(as_text=True)
    faculty_csrf = _csrf_from_html(faculty_dashboard_html)
    with workflow_app.app_context():
        correction_request = AttendanceCorrectionRequest.query.one()
        correction_id = correction_request.id
    review = faculty_client.post(
        f"/faculty/correction/{correction_id}/review",
        data={"csrf_token": faculty_csrf, "decision": "rejected"},
        follow_redirects=True,
    )
    assert review.status_code == 200
    with workflow_app.app_context():
        assert db.session.get(AttendanceCorrectionRequest, correction_id).status == "rejected"
        assert AuditLog.query.filter_by(event_type="attendance_duplicate").count() == 1

    updated_student_dashboard = student_client.get("/student/dashboard")
    assert b"Morning Lab" in updated_student_dashboard.data
    faculty_report = faculty_client.get("/faculty/dashboard")
    assert b"Student One" in faculty_report.data
    assert b"Student Two" in faculty_report.data


def test_student_cannot_create_faculty_session(workflow_app):
    client = workflow_app.test_client()
    assert _login(client, "student1@example.com", "Student123!").status_code == 302
    dashboard = client.get("/student/dashboard").get_data(as_text=True)
    csrf_token = re.search(r'<meta name="csrf-token" content="([^"]+)"', dashboard).group(1)
    response = client.post(
        "/faculty/session/create",
        data={"csrf_token": csrf_token, "title": "Unauthorized", "subject_id": 1},
    )
    assert response.status_code == 302
    assert client.get("/admin/dashboard").status_code == 302
    with workflow_app.app_context():
        assert AttendanceSession.query.filter_by(title="Unauthorized").first() is None


def test_session_must_be_finalized_before_counting_and_cannot_be_reopened(workflow_app):
    client = workflow_app.test_client()
    assert _login(client, "faculty@example.com", "Faculty123!").status_code == 302
    dashboard = client.get("/faculty/dashboard").get_data(as_text=True)
    csrf_token = _csrf_from_html(dashboard)
    with workflow_app.app_context():
        subject_id = Subject.query.one().id
        student_id = StudentProfile.query.join(User).filter_by(email="student1@example.com").one().id

    created = client.post(
        "/faculty/session/create",
        data={"csrf_token": csrf_token, "title": "Lifecycle Lab", "subject_id": subject_id},
    )
    assert created.status_code == 302
    with workflow_app.app_context():
        session_id = AttendanceSession.query.filter_by(title="Lifecycle Lab").one().id

    assert client.post(f"/faculty/session/{session_id}/toggle", data={"csrf_token": csrf_token}).status_code == 302
    with workflow_app.app_context():
        session = db.session.get(AttendanceSession, session_id)
        assert session.status == "closed"
        assert calculate_attendance_percentage(student_id, subject_id) == 0.0

    assert client.post(f"/faculty/session/{session_id}/toggle", data={"csrf_token": csrf_token}).status_code == 302
    with workflow_app.app_context():
        session = db.session.get(AttendanceSession, session_id)
        assert session.status == "finalized"
        assert calculate_attendance_percentage(student_id, subject_id) == 0.0

    assert client.post(f"/faculty/session/{session_id}/toggle", data={"csrf_token": csrf_token}).status_code == 302
    with workflow_app.app_context():
        assert db.session.get(AttendanceSession, session_id).status == "finalized"


def test_cancelled_session_is_not_eligible_for_attendance_percentage(workflow_app):
    client = workflow_app.test_client()
    assert _login(client, "faculty@example.com", "Faculty123!").status_code == 302
    dashboard = client.get("/faculty/dashboard").get_data(as_text=True)
    csrf_token = _csrf_from_html(dashboard)
    with workflow_app.app_context():
        subject_id = Subject.query.one().id
        student_id = StudentProfile.query.join(User).filter_by(email="student1@example.com").one().id

    client.post(
        "/faculty/session/create",
        data={"csrf_token": csrf_token, "title": "Cancelled Lab", "subject_id": subject_id},
    )
    with workflow_app.app_context():
        session_id = AttendanceSession.query.filter_by(title="Cancelled Lab").one().id

    cancelled = client.post(f"/faculty/session/{session_id}/cancel", data={"csrf_token": csrf_token})
    assert cancelled.status_code == 302
    with workflow_app.app_context():
        assert db.session.get(AttendanceSession, session_id).status == "cancelled"
        assert calculate_attendance_percentage(student_id, subject_id) == 0.0


def test_logout_requires_csrf_protected_post(workflow_app):
    client = workflow_app.test_client()
    assert _login(client, "student1@example.com", "Student123!").status_code == 302
    assert client.get("/logout").status_code == 405
    assert client.post("/logout").status_code == 400
    dashboard = client.get("/student/dashboard").get_data(as_text=True)
    csrf_token = _csrf_from_html(dashboard)
    logout = client.post("/logout", data={"csrf_token": csrf_token}, follow_redirects=False)
    assert logout.status_code == 302
    assert logout.headers["Location"].endswith("/login")