import csv
from datetime import datetime, timedelta
from io import StringIO

from flask import Blueprint, current_app, flash, make_response, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import or_
from email_validator import EmailNotValidError, validate_email

from app.extensions import db
from app.models.models import (
    Announcement,
    AttendanceRecord,
    AttendanceSession,
    AuditLog,
    Course,
    Department,
    FacultyProfile,
    FacultySubjectAssignment,
    DatabaseBackupRecord,
    QRToken,
    StudentEnrollment,
    StudentProfile,
    Subject,
    User,
)
from app.services.csv_service import safe_csv_cell

admin_bp = Blueprint("admin", __name__)


def role_required(role):
    def decorator(func):
        def wrapper(*args, **kwargs):
            from flask_login import current_user

            if not current_user.is_authenticated or current_user.role != role:
                flash("You do not have access to this page.", "error")
                return redirect(url_for("auth.login"))
            return func(*args, **kwargs)

        wrapper.__name__ = func.__name__
        return wrapper

    return decorator


@admin_bp.route("/dashboard")
@login_required
@role_required("admin")
def dashboard():
    users = User.query.order_by(User.created_at.desc()).all()
    departments = Department.query.order_by(Department.name).all()
    courses = Course.query.order_by(Course.name).all()
    subjects = Subject.query.order_by(Subject.name).all()
    sessions = AttendanceSession.query.order_by(AttendanceSession.created_at.desc()).limit(8).all()
    stats = {
        "users": len(users),
        "students": User.query.filter_by(role="student").count(),
        "faculty": User.query.filter_by(role="faculty").count(),
        "admins": User.query.filter_by(role="admin").count(),
        "sessions": AttendanceSession.query.count(),
        "subjects": Subject.query.count(),
        "active_sessions": AttendanceSession.query.filter_by(status="active").count(),
        "login_success": AuditLog.query.filter_by(event_type="login_success").count(),
        "login_failure": AuditLog.query.filter_by(event_type="login_failure").count(),
        "qr_invalid": AuditLog.query.filter_by(event_type="qr_invalid").count(),
        "qr_expired": AuditLog.query.filter_by(event_type="qr_expired").count(),
        "attendance_duplicates": AuditLog.query.filter_by(event_type="attendance_duplicate").count(),
        "qr_active": QRToken.query.filter(QRToken.is_active.is_(True), QRToken.expires_at > datetime.utcnow()).count(),
        "qr_revoked": QRToken.query.filter_by(is_active=False).count(),
    }
    stats["alert_threshold"] = current_app.config["SECURITY_ALERT_THRESHOLD"]
    stats["security_alert"] = (
        stats["login_failure"] >= stats["alert_threshold"]
        or stats["qr_invalid"] >= stats["alert_threshold"]
        or stats["qr_expired"] >= stats["alert_threshold"]
    )
    start_date = request.args.get("start_date", "")
    end_date = request.args.get("end_date", "")
    role_filter = request.args.get("role", "")
    search = request.args.get("search", "").strip()[:100]
    audit_query = AuditLog.query
    if role_filter in {"admin", "faculty", "student"}:
        audit_query = audit_query.filter_by(role=role_filter)
    for value, lower_bound in ((start_date, True), (end_date, False)):
        if value:
            try:
                day = datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                continue
            if lower_bound:
                audit_query = audit_query.filter(AuditLog.created_at >= day)
            else:
                audit_query = audit_query.filter(AuditLog.created_at < day + timedelta(days=1))
    if search:
        audit_query = audit_query.filter(or_(AuditLog.event_type.ilike(f"%{search}%"), AuditLog.details.ilike(f"%{search}%")))
    audit_events = audit_query.order_by(AuditLog.created_at.desc()).limit(100).all()
    qr_tokens = QRToken.query.order_by(QRToken.issued_at.desc()).limit(10).all()
    backup_records = DatabaseBackupRecord.query.order_by(DatabaseBackupRecord.created_at.desc()).limit(10).all()
    announcements = Announcement.query.order_by(Announcement.starts_at.desc()).limit(20).all()
    return render_template(
        "admin/dashboard.html",
        stats=stats,
        users=users,
        departments=departments,
        courses=courses,
        subjects=subjects,
        sessions=sessions,
        audit_events=audit_events,
        audit_filters={"start_date": start_date, "end_date": end_date, "role": role_filter, "search": search},
        qr_tokens=qr_tokens,
        backup_records=backup_records,
        announcements=announcements,
        now=datetime.utcnow(),
    )


@admin_bp.route("/announcement/create", methods=["POST"])
@login_required
@role_required("admin")
def create_announcement():
    title = (request.form.get("title") or "").strip()
    message = (request.form.get("message") or "").strip()
    start_value = (request.form.get("starts_at") or "").strip()
    end_value = (request.form.get("ends_at") or "").strip()
    if not title or len(title) > 120 or not message or len(message) > 2000:
        flash("Enter an announcement title and message within the allowed limits.", "error")
        return redirect(url_for("admin.dashboard", _anchor="announcements"))
    try:
        starts_at = datetime.fromisoformat(start_value) if start_value else datetime.utcnow()
        ends_at = datetime.fromisoformat(end_value) if end_value else None
    except ValueError:
        flash("Enter valid announcement dates.", "error")
        return redirect(url_for("admin.dashboard", _anchor="announcements"))
    if ends_at is not None and ends_at <= starts_at:
        flash("The announcement end must be later than its start.", "error")
        return redirect(url_for("admin.dashboard", _anchor="announcements"))

    announcement = Announcement(
        title=title,
        message=message,
        starts_at=starts_at,
        ends_at=ends_at,
        created_by_id=current_user.id,
    )
    db.session.add(announcement)
    db.session.commit()
    AuditLog.create_event(current_user.id, "announcement_created", details="Attendance announcement published.")
    flash("Attendance announcement saved.", "success")
    return redirect(url_for("admin.dashboard", _anchor="announcements"))


@admin_bp.route("/department/create", methods=["POST"])
@login_required
@role_required("admin")
def create_department():
    name = request.form.get("name", "").strip()
    code = request.form.get("code", "").strip()
    if name and code:
        dep = Department(name=name, code=code)
        db.session.add(dep)
        db.session.commit()
        AuditLog.create_event(current_user.id, "department_created", details="Department created.")
        flash("Department created.", "success")
    else:
        flash("Department name and code are required.", "error")
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/course/create", methods=["POST"])
@login_required
@role_required("admin")
def create_course():
    name = request.form.get("name", "").strip()
    code = request.form.get("code", "").strip()
    department_id = request.form.get("department_id")
    if name and code:
        course = Course(name=name, code=code, department_id=int(department_id) if department_id else None)
        db.session.add(course)
        db.session.commit()
        AuditLog.create_event(current_user.id, "course_created", details="Course created.")
        flash("Course created.", "success")
    else:
        flash("Course name and code are required.", "error")
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/subject/create", methods=["POST"])
@login_required
@role_required("admin")
def create_subject():
    name = request.form.get("name", "").strip()
    code = request.form.get("code", "").strip()
    course_id = request.form.get("course_id")
    if name and code and course_id:
        subject = Subject(name=name, code=code, course_id=int(course_id))
        db.session.add(subject)
        db.session.commit()
        AuditLog.create_event(current_user.id, "subject_created", details="Subject created.")
        flash("Subject created.", "success")
    else:
        flash("All subject details are required.", "error")
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/user/create", methods=["POST"])
@login_required
@role_required("admin")
def create_user():
    full_name = request.form.get("full_name", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    role = request.form.get("role", "student")
    department_id = request.form.get("department_id")

    if not full_name or not email or not password:
        flash("Missing user details.", "error")
        return redirect(url_for("admin.dashboard"))

    if User.query.filter_by(email=email).first():
        flash("That email is already in use.", "error")
        return redirect(url_for("admin.dashboard"))

    user = User(email=email, full_name=full_name, role=role)
    user.password = password
    user.is_active = True
    db.session.add(user)
    db.session.flush()

    if role == "student":
        profile = StudentProfile(user_id=user.id, department_id=int(department_id) if department_id else None, approved=True, status="approved")
        db.session.add(profile)
    elif role == "faculty":
        profile = FacultyProfile(user_id=user.id, department_id=int(department_id) if department_id else None, designation="Faculty")
        db.session.add(profile)

    db.session.commit()
    AuditLog.create_event(current_user.id, "account_created", details=f"User account {user.id} created for role {role}.")
    flash("User created successfully.", "success")
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/approve-student/<int:user_id>", methods=["POST"])
@login_required
@role_required("admin")
def approve_student(user_id):
    user = User.query.get_or_404(user_id)
    profile = user.student_profile
    if profile:
        profile.approved = True
        profile.status = "approved"
        db.session.commit()
        AuditLog.create_event(current_user.id, "student_approved", details=f"Student account {user.id} approved.")
        flash("Student approved.", "success")
    else:
        flash("Student profile not found.", "error")
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/assign-subject", methods=["POST"])
@login_required
@role_required("admin")
def assign_subject():
    faculty_id = request.form.get("faculty_id")
    subject_id = request.form.get("subject_id")
    if faculty_id and subject_id:
        assignment = FacultySubjectAssignment(faculty_id=int(faculty_id), subject_id=int(subject_id))
        db.session.add(assignment)
        try:
            db.session.commit()
            AuditLog.create_event(current_user.id, "subject_assignment", details=f"Subject {subject_id} assigned to faculty profile {faculty_id}.")
            flash("Faculty assignment saved.", "success")
        except Exception:
            db.session.rollback()
            flash("Assignment already exists.", "error")
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/enroll-student", methods=["POST"])
@login_required
@role_required("admin")
def enroll_student():
    student_id = request.form.get("student_id")
    subject_id = request.form.get("subject_id")
    if student_id and subject_id:
        enrollment = StudentEnrollment(student_id=int(student_id), subject_id=int(subject_id))
        db.session.add(enrollment)
        try:
            db.session.commit()
            AuditLog.create_event(current_user.id, "student_enrolled", details=f"Student profile {student_id} enrolled in subject {subject_id}.")
            flash("Student enrolled.", "success")
        except Exception:
            db.session.rollback()
            flash("Enrollment already exists.", "error")
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/backup", methods=["POST"])
@login_required
@role_required("admin")
def create_backup():
    if not current_app.config["APP_BACKUP_SUPPORTED"]:
        flash("MySQL backups are managed by your database provider.", "info")
        return redirect(url_for("admin.dashboard"))

    from app.services.backup_service import create_backup

    try:
        backup_path = create_backup()
        backup_record = DatabaseBackupRecord(
            filename=backup_path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1],
            status="verified",
            integrity_result="ok",
        )
        db.session.add(backup_record)
        db.session.commit()
        AuditLog.create_event(current_user.id, "backup_created", details=f"Backup verified: {backup_record.filename}.")
        flash(f"Backup verified: {backup_record.filename}", "success")
    except Exception:
        db.session.rollback()
        filename = f"failed-{datetime.utcnow().strftime('%Y%m%dT%H%M%S%f')}"
        db.session.add(DatabaseBackupRecord(filename=filename, status="failed", integrity_result="failed"))
        db.session.commit()
        AuditLog.create_event(current_user.id, "backup_failed", details="Database backup could not be verified.", success=False)
        flash("Backup failed verification. The error was recorded for administrators.", "error")
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/user/<int:user_id>/status", methods=["POST"])
@login_required
@role_required("admin")
def update_user_status(user_id):
    user = User.query.get_or_404(user_id)
    requested_status = request.form.get("status")
    if user.id == current_user.id and requested_status == "suspended":
        flash("You cannot suspend your own administrator account.", "error")
        return redirect(url_for("admin.dashboard", _anchor="accounts"))
    if requested_status not in {"active", "suspended"}:
        flash("Invalid account status.", "error")
        return redirect(url_for("admin.dashboard", _anchor="accounts"))
    user.is_active = requested_status == "active"
    user.auth_version += 1
    db.session.commit()
    AuditLog.create_event(current_user.id, "account_status_changed", details=f"Account {user.id} status changed to {requested_status}.")
    flash("Account status updated.", "success")
    return redirect(url_for("admin.dashboard", _anchor="accounts"))


@admin_bp.route("/user/<int:user_id>/edit", methods=["POST"])
@login_required
@role_required("admin")
def edit_user(user_id):
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash("Edit your own account details from the profile page.", "error")
        return redirect(url_for("admin.dashboard", _anchor="accounts"))

    full_name = (request.form.get("full_name") or "").strip()
    email = (request.form.get("email") or "").strip()
    if not full_name or len(full_name) > 255 or not email or len(email) > 255:
        flash("A valid name and email address are required.", "error")
        return redirect(url_for("admin.dashboard", _anchor="accounts"))
    try:
        email = validate_email(email, check_deliverability=False).normalized.lower()
    except EmailNotValidError:
        flash("Enter a valid email address.", "error")
        return redirect(url_for("admin.dashboard", _anchor="accounts"))
    if User.query.filter(User.email == email, User.id != user.id).first():
        flash("That email address is already in use.", "error")
        return redirect(url_for("admin.dashboard", _anchor="accounts"))

    department_id = request.form.get("department_id") or None
    department = None
    if department_id:
        if not department_id.isdigit():
            flash("Choose a valid department.", "error")
            return redirect(url_for("admin.dashboard", _anchor="accounts"))
        department = db.session.get(Department, int(department_id))
        if department is None:
            flash("Choose a valid department.", "error")
            return redirect(url_for("admin.dashboard", _anchor="accounts"))

    if user.student_profile:
        student_number = (request.form.get("student_number") or "").strip()
        year = (request.form.get("year") or "").strip()
        if len(student_number) > 60 or len(year) > 20:
            flash("Student number or year exceeds its length limit.", "error")
            return redirect(url_for("admin.dashboard", _anchor="accounts"))
        if student_number and StudentProfile.query.filter(
            StudentProfile.student_number == student_number,
            StudentProfile.user_id != user.id,
        ).first():
            flash("That student number is already in use.", "error")
            return redirect(url_for("admin.dashboard", _anchor="accounts"))
        user.student_profile.student_number = student_number or None
        user.student_profile.year = year or None
        user.student_profile.department_id = department.id if department else None
    elif user.faculty_profile:
        user.faculty_profile.department_id = department.id if department else None

    user.full_name = full_name
    user.email = email
    user.auth_version += 1
    db.session.commit()
    AuditLog.create_event(current_user.id, "account_updated", details=f"Account {user.id} details updated.")
    flash("Account details updated; their existing sessions have been invalidated.", "success")
    return redirect(url_for("admin.dashboard", _anchor="accounts"))


@admin_bp.route("/qr-token/<int:token_id>/revoke", methods=["POST"])
@login_required
@role_required("admin")
def revoke_qr_token(token_id):
    token = QRToken.query.get_or_404(token_id)
    token.is_active = False
    db.session.commit()
    AuditLog.create_event(current_user.id, "qr_token_revoked", details=f"QR token {token.id} revoked.")
    flash("QR token revoked.", "success")
    return redirect(url_for("admin.dashboard", _anchor="security"))


@admin_bp.route("/audit.csv")
@login_required
@role_required("admin")
def export_audit_csv():
    query = AuditLog.query
    role = request.args.get("role", "")
    if role in {"admin", "faculty", "student"}:
        query = query.filter_by(role=role)
    search = request.args.get("search", "").strip()[:100]
    start_date = request.args.get("start_date", "")
    end_date = request.args.get("end_date", "")
    for value, lower_bound in ((start_date, True), (end_date, False)):
        if value:
            try:
                day = datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                continue
            if lower_bound:
                query = query.filter(AuditLog.created_at >= day)
            else:
                query = query.filter(AuditLog.created_at < day + timedelta(days=1))
    if search:
        query = query.filter(or_(AuditLog.event_type.ilike(f"%{search}%"), AuditLog.details.ilike(f"%{search}%")))
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["timestamp_utc", "role", "event_type", "success", "description", "request_id"])
    for event in query.order_by(AuditLog.created_at.desc()).limit(5000).all():
        writer.writerow(map(safe_csv_cell, [
            event.created_at.isoformat(timespec="seconds") + "Z",
            event.role or "unknown",
            event.event_type,
            "" if event.success is None else str(event.success).lower(),
            event.details or "",
            event.correlation_id or "",
        ]))
    response = make_response(output.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = "attachment; filename=security-audit.csv"
    response.headers["Cache-Control"] = "no-store"
    AuditLog.create_event(current_user.id, "audit_exported", details="Security audit CSV exported.")
    return response


@admin_bp.route("/attendance.csv")
@login_required
@role_required("admin")
def export_attendance_csv():
    query = AttendanceRecord.query.join(AttendanceSession).join(StudentProfile).join(User)
    subject_id = request.args.get("subject_id", "")
    student_id = request.args.get("student_id", "")
    faculty_id = request.args.get("faculty_id", "")
    if subject_id.isdigit():
        query = query.filter(AttendanceSession.subject_id == int(subject_id))
    if student_id.isdigit():
        query = query.filter(AttendanceRecord.student_id == int(student_id))
    if faculty_id.isdigit():
        query = query.filter(AttendanceSession.faculty_id == int(faculty_id))

    for parameter, lower_bound in (("start_date", True), ("end_date", False)):
        value = request.args.get(parameter, "")
        if value:
            try:
                day = datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                continue
            if lower_bound:
                query = query.filter(AttendanceRecord.checked_in_at >= day)
            else:
                query = query.filter(AttendanceRecord.checked_in_at < day + timedelta(days=1))

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["checked_in_utc", "student", "student_number", "faculty", "subject", "session", "status", "verification"])
    for record in query.order_by(AttendanceRecord.checked_in_at.desc()).limit(10000).all():
        writer.writerow(map(safe_csv_cell, [
            record.checked_in_at.isoformat(timespec="seconds") + "Z",
            record.student.user.full_name,
            record.student.student_number or "",
            record.session.faculty.user.full_name,
            record.session.subject.code,
            record.session.title,
            record.status,
            "verified" if record.token_id else "legacy/unverified",
        ]))

    response = make_response(output.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = "attachment; filename=institution-attendance.csv"
    response.headers["Cache-Control"] = "no-store"
    AuditLog.create_event(current_user.id, "institution_attendance_exported", details="Institution attendance report exported.")
    return response
