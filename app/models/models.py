from datetime import datetime

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    full_name = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(32), nullable=False, default="student")
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    phone = db.Column(db.String(40), nullable=True)
    location = db.Column(db.String(120), nullable=True)
    bio = db.Column(db.Text, nullable=True)
    privacy_level = db.Column(db.String(20), nullable=False, default="standard", server_default="standard")
    auth_version = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    totp_secret = db.Column(db.Text, nullable=True)
    totp_enabled = db.Column(db.Boolean, nullable=False, default=False, server_default="0")
    totp_last_counter = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )
    last_login = db.Column(db.DateTime, nullable=True)

    student_profile = db.relationship(
        "StudentProfile",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
    )
    faculty_profile = db.relationship(
        "FacultyProfile",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
    )
    audit_logs = db.relationship("AuditLog", back_populates="user")
    sessions = db.relationship("UserSession", back_populates="user", cascade="all, delete-orphan")

    def __init__(self, email, full_name, role="student", password=None, **kwargs):
        self.email = email.lower().strip()
        self.full_name = full_name
        self.role = role
        self.is_active = kwargs.get("is_active", True)
        if password is not None:
            self.password = password

    @property
    def password(self):
        raise AttributeError("Password is write-only.")

    @password.setter
    def password(self, value):
        self.password_hash = generate_password_hash(value, method="scrypt")

    def check_password(self, value):
        return check_password_hash(self.password_hash, value)

    def get_dashboard_url(self):
        dashboard_map = {
            "admin": "admin.dashboard",
            "faculty": "faculty.dashboard",
            "student": "student.dashboard",
        }
        return dashboard_map.get(self.role, "auth.login")


class UserSession(db.Model):
    __tablename__ = "user_sessions"

    session_id = db.Column(db.String(64), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    auth_version = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    last_seen_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    revoked_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship("User", back_populates="sessions")


class Department(db.Model):
    __tablename__ = "departments"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    code = db.Column(db.String(40), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    faculty_profiles = db.relationship("FacultyProfile", back_populates="department")
    student_profiles = db.relationship("StudentProfile", back_populates="department")
    courses = db.relationship("Course", back_populates="department")


class Course(db.Model):
    __tablename__ = "courses"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    code = db.Column(db.String(40), nullable=False, unique=True)
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    department = db.relationship("Department", back_populates="courses")
    subjects = db.relationship("Subject", back_populates="course")


class Subject(db.Model):
    __tablename__ = "subjects"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True, nullable=False)
    name = db.Column(db.String(150), nullable=False)
    course_id = db.Column(db.Integer, db.ForeignKey("courses.id"), nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    course = db.relationship("Course", back_populates="subjects")
    sessions = db.relationship("AttendanceSession", back_populates="subject")
    enrollments = db.relationship("StudentEnrollment", back_populates="subject")
    faculty_assignments = db.relationship("FacultySubjectAssignment", back_populates="subject")


class Announcement(db.Model):
    __tablename__ = "announcements"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(120), nullable=False)
    message = db.Column(db.String(2000), nullable=False)
    starts_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    ends_at = db.Column(db.DateTime, nullable=True, index=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    created_by = db.relationship("User")


class StudentProfile(db.Model):
    __tablename__ = "student_profiles"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False)
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id"), nullable=True)
    student_number = db.Column(db.String(60), unique=True, nullable=True)
    year = db.Column(db.String(20), nullable=True)
    section = db.Column(db.String(20), nullable=True)
    date_of_birth = db.Column(db.Date, nullable=True)
    gender = db.Column(db.String(40), nullable=True)
    profile_photo = db.Column(db.String(80), nullable=True)
    address = db.Column(db.String(255), nullable=True)
    guardian_name = db.Column(db.String(255), nullable=True)
    guardian_contact = db.Column(db.String(40), nullable=True)
    emergency_contact = db.Column(db.String(40), nullable=True)
    approved = db.Column(db.Boolean, default=False)
    status = db.Column(db.String(30), default="pending")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", back_populates="student_profile")
    department = db.relationship("Department", back_populates="student_profiles")
    enrollments = db.relationship("StudentEnrollment", back_populates="student")
    attendance_records = db.relationship("AttendanceRecord", back_populates="student")


class FacultyProfile(db.Model):
    __tablename__ = "faculty_profiles"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False)
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id"), nullable=True)
    employee_id = db.Column(db.String(40), unique=True, nullable=True, index=True)
    designation = db.Column(db.String(100), nullable=True)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", back_populates="faculty_profile")
    department = db.relationship("Department", back_populates="faculty_profiles")
    subject_assignments = db.relationship("FacultySubjectAssignment", back_populates="faculty")
    sessions = db.relationship("AttendanceSession", back_populates="faculty")


class StudentEnrollment(db.Model):
    __tablename__ = "student_enrollments"

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student_profiles.id"), nullable=False)
    subject_id = db.Column(db.Integer, db.ForeignKey("subjects.id"), nullable=False)
    enrolled_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint("student_id", "subject_id", name="uq_student_subject_enrollment"),
    )

    student = db.relationship("StudentProfile", back_populates="enrollments")
    subject = db.relationship("Subject", back_populates="enrollments")


class FacultySubjectAssignment(db.Model):
    __tablename__ = "faculty_subject_assignments"

    id = db.Column(db.Integer, primary_key=True)
    faculty_id = db.Column(db.Integer, db.ForeignKey("faculty_profiles.id"), nullable=False)
    subject_id = db.Column(db.Integer, db.ForeignKey("subjects.id"), nullable=False)
    assigned_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint("faculty_id", "subject_id", name="uq_faculty_subject_assignment"),
    )

    faculty = db.relationship("FacultyProfile", back_populates="subject_assignments")
    subject = db.relationship("Subject", back_populates="faculty_assignments")


class AttendanceSession(db.Model):
    __tablename__ = "attendance_sessions"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(140), nullable=False)
    subject_id = db.Column(db.Integer, db.ForeignKey("subjects.id"), nullable=False)
    faculty_id = db.Column(db.Integer, db.ForeignKey("faculty_profiles.id"), nullable=False)
    start_time = db.Column(db.DateTime, nullable=True)
    end_time = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(30), default="draft")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    subject = db.relationship("Subject", back_populates="sessions")
    faculty = db.relationship("FacultyProfile", back_populates="sessions")
    qr_tokens = db.relationship("QRToken", back_populates="session")
    attendance_records = db.relationship("AttendanceRecord", back_populates="session")


class QRToken(db.Model):
    __tablename__ = "qr_tokens"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("attendance_sessions.id"), nullable=False)
    token_id = db.Column(db.String(64), unique=True, nullable=False, index=True)
    nonce = db.Column(db.String(64), unique=True, nullable=True, index=True)
    issued_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at = db.Column(db.DateTime, nullable=True)
    is_active = db.Column(db.Boolean, default=True)
    verification_result = db.Column(db.String(32), nullable=True)

    session = db.relationship("AttendanceSession", back_populates="qr_tokens")
    attendance_records = db.relationship("AttendanceRecord", back_populates="token")


class AttendanceRecord(db.Model):
    __tablename__ = "attendance_records"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("attendance_sessions.id"), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey("student_profiles.id"), nullable=False)
    token_id = db.Column(db.String(64), db.ForeignKey("qr_tokens.token_id"), nullable=True)
    status = db.Column(db.String(20), default="present")
    checked_in_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint("session_id", "student_id", name="uq_session_student_attendance"),
    )

    session = db.relationship("AttendanceSession", back_populates="attendance_records")
    student = db.relationship("StudentProfile", back_populates="attendance_records")
    token = db.relationship("QRToken", back_populates="attendance_records")


class AttendanceCorrectionRequest(db.Model):
    __tablename__ = "attendance_correction_requests"

    id = db.Column(db.Integer, primary_key=True)
    record_id = db.Column(db.Integer, db.ForeignKey("attendance_records.id"), nullable=False)
    requester_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    reason = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(30), default="pending")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    record = db.relationship("AttendanceRecord")


class AuditLog(db.Model):
    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    event_type = db.Column(db.String(120), nullable=False, index=True)
    details = db.Column(db.Text, nullable=True)
    ip_address = db.Column(db.String(80), nullable=True)
    role = db.Column(db.String(32), nullable=True, index=True)
    success = db.Column(db.Boolean, nullable=True, index=True)
    correlation_id = db.Column(db.String(64), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user = db.relationship("User", back_populates="audit_logs")

    @staticmethod
    def create_event(user_id, event_type, details=None, ip_address=None, success=True, role=None, correlation_id=None):
        from flask import g

        safe_details = " ".join(str(details or "").split())[:500] or None
        if role is None and user_id is not None:
            user = db.session.get(User, user_id)
            role = user.role if user is not None else None
        event = AuditLog(
            user_id=user_id,
            event_type=event_type,
            details=safe_details,
            ip_address=ip_address,
            role=role,
            success=success,
            correlation_id=correlation_id or getattr(g, "request_id", None),
        )
        db.session.add(event)
        db.session.commit()
        return event


class LoginAttempt(db.Model):
    __tablename__ = "login_attempts"

    identifier_hash = db.Column(db.String(64), primary_key=True)
    failed_attempts = db.Column(db.Integer, nullable=False, default=0)
    window_started_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    locked_until = db.Column(db.DateTime, nullable=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class PasswordResetToken(db.Model):
    __tablename__ = "password_reset_tokens"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    token_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    used_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship("User")


class DatabaseBackupRecord(db.Model):
    __tablename__ = "database_backup_records"

    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False, unique=True)
    status = db.Column(db.String(20), nullable=False)
    integrity_result = db.Column(db.String(32), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
