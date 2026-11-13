from datetime import datetime, timedelta
import json
import csv
from io import StringIO

from flask import Blueprint, current_app, flash, jsonify, make_response, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, or_
from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db, limiter
from app.models.models import (
    AttendanceCorrectionRequest,
    AttendanceRecord,
    AttendanceSession,
    AuditLog,
    FacultyProfile,
    FacultySubjectAssignment,
    QRToken,
    StudentEnrollment,
    StudentProfile,
    User,
    Subject,
)
from app.services.csv_service import safe_csv_cell
from app.services.qr_service import generate_qr_image, generate_qr_payload

faculty_bp = Blueprint("faculty", __name__)


def role_required(role):
    def decorator(func):
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated or current_user.role != role:
                flash("Unauthorized access.", "error")
                return redirect(url_for("auth.login"))
            return func(*args, **kwargs)

        wrapper.__name__ = func.__name__
        return wrapper

    return decorator


@faculty_bp.route("/dashboard")
@login_required
@role_required("faculty")
def dashboard():
    faculty_profile = current_user.faculty_profile
    assignments = FacultySubjectAssignment.query.filter_by(faculty_id=faculty_profile.id).all() if faculty_profile else []
    allowed_subject_ids = [assignment.subject_id for assignment in assignments]
    owned_sessions = AttendanceSession.query.filter_by(faculty_id=faculty_profile.id).all() if faculty_profile else []
    sessions_query = AttendanceSession.query.filter_by(faculty_id=faculty_profile.id) if faculty_profile else AttendanceSession.query.filter(False)

    search = request.args.get("search", "").strip()[:100]
    subject_filter = request.args.get("subject_id", "")
    start_date = request.args.get("start_date", "")
    end_date = request.args.get("end_date", "")
    if subject_filter:
        try:
            requested_subject_id = int(subject_filter)
        except ValueError:
            requested_subject_id = None
        if requested_subject_id in allowed_subject_ids:
            sessions_query = sessions_query.filter_by(subject_id=requested_subject_id)
    for value, lower_bound in ((start_date, True), (end_date, False)):
        if value:
            try:
                day = datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                continue
            if lower_bound:
                sessions_query = sessions_query.filter(AttendanceSession.created_at >= day)
            else:
                sessions_query = sessions_query.filter(AttendanceSession.created_at < day + timedelta(days=1))
    if search:
        sessions_query = sessions_query.join(AttendanceRecord).join(StudentProfile).join(User).filter(
            or_(User.full_name.ilike(f"%{search}%"), StudentProfile.student_number.ilike(f"%{search}%"))
        ).distinct()
    sessions = sessions_query.order_by(AttendanceSession.created_at.desc()).all()
    enrolled_student_ids = set(
        student_id
        for (student_id,) in db.session.query(StudentEnrollment.student_id)
        .filter(StudentEnrollment.subject_id.in_(allowed_subject_ids)).distinct().all()
    ) if allowed_subject_ids else set()
    enrolled_students = (
        StudentProfile.query.join(StudentEnrollment)
        .filter(StudentEnrollment.subject_id.in_(allowed_subject_ids))
        .distinct()
        .order_by(StudentProfile.student_number, StudentProfile.id)
        .all()
        if allowed_subject_ids
        else []
    )
    completed_sessions = [session for session in owned_sessions if session.status == "finalized"]
    expected_attendance = sum(
        StudentEnrollment.query.filter_by(subject_id=session.subject_id).count()
        for session in completed_sessions
    )
    actual_attendance = sum(len(session.attendance_records) for session in completed_sessions)
    correction_requests = (
        AttendanceCorrectionRequest.query.join(AttendanceRecord, AttendanceCorrectionRequest.record_id == AttendanceRecord.id)
        .join(AttendanceSession, AttendanceRecord.session_id == AttendanceSession.id)
        .filter(AttendanceSession.faculty_id == faculty_profile.id, AttendanceCorrectionRequest.status == "pending")
        .order_by(AttendanceCorrectionRequest.created_at.desc())
        .all()
        if faculty_profile
        else []
    )
    stats = {
        "subjects": len(assignments),
        "students": len(enrolled_student_ids),
        "active_sessions": sum(session.status == "active" for session in owned_sessions),
        "students_present": sum(len(session.attendance_records) for session in owned_sessions if session.status == "active"),
        "attendance_percentage": round(actual_attendance * 100 / expected_attendance, 2) if expected_attendance else 0.0,
    }
    return render_template(
        "faculty/dashboard.html",
        assignments=assignments,
        enrolled_students=enrolled_students,
        sessions=sessions,
        stats=stats,
        correction_requests=correction_requests,
        session_filters={"search": search, "subject_id": subject_filter, "start_date": start_date, "end_date": end_date},
    )


@faculty_bp.route("/session/create", methods=["POST"])
@login_required
@role_required("faculty")
def create_session():
    title = request.form.get("title", "").strip()
    subject_id = request.form.get("subject_id")
    faculty_profile = current_user.faculty_profile
    if not faculty_profile:
        flash("Faculty profile missing.", "error")
        return redirect(url_for("faculty.dashboard"))
    try:
        subject_id = int(subject_id)
    except (TypeError, ValueError):
        subject_id = None
    if not title or len(title) > 140 or subject_id is None:
        flash("Session title and subject are required.", "error")
        return redirect(url_for("faculty.dashboard"))

    assigned = FacultySubjectAssignment.query.filter_by(faculty_id=faculty_profile.id, subject_id=subject_id).first()
    subject = Subject.query.filter_by(id=subject_id, is_active=True).first()
    if not assigned or not subject:
        flash("You are not assigned to an active subject.", "error")
        return redirect(url_for("faculty.dashboard"))

    duplicate_cutoff = datetime.utcnow() - timedelta(seconds=10)
    recent_session = AttendanceSession.query.filter(
        AttendanceSession.faculty_id == faculty_profile.id,
        AttendanceSession.subject_id == subject_id,
        AttendanceSession.title == title,
        AttendanceSession.created_at >= duplicate_cutoff,
    ).first()
    if recent_session:
        flash("That session was just created; duplicate submission was ignored.", "info")
        return redirect(url_for("faculty.dashboard"))

    session = AttendanceSession(
        title=title,
        subject_id=subject_id,
        faculty_id=faculty_profile.id,
        status="active",
        start_time=datetime.utcnow(),
    )
    db.session.add(session)
    db.session.add(AuditLog(user_id=current_user.id, event_type="session_created", details=f"Session created: {title}"))
    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        flash("The attendance session could not be saved. Please try again.", "error")
        return redirect(url_for("faculty.dashboard"))

    flash("Session created and attendance is open.", "success")
    return redirect(url_for("faculty.dashboard"))


@faculty_bp.route("/session/<int:session_id>/toggle", methods=["POST"])
@login_required
@role_required("faculty")
def toggle_session(session_id):
    session = AttendanceSession.query.get_or_404(session_id)
    faculty_profile = current_user.faculty_profile
    if faculty_profile is None or session.faculty_id != faculty_profile.id:
        flash("You may only manage your own sessions.", "error")
        return redirect(url_for("faculty.dashboard"))
    if session.status == "active":
        session.status = "closed"
        session.end_time = datetime.utcnow()
        QRToken.query.filter_by(session_id=session.id, is_active=True).update({"is_active": False})
    elif session.status == "closed":
        session.status = "finalized"
    else:
        flash("Only active or closed sessions can change state.", "error")
        return redirect(url_for("faculty.dashboard"))
    db.session.commit()
    AuditLog.create_event(current_user.id, "session_status_changed", details=f"Session {session.id} toggled to {session.status}.")
    flash(f"Session {session.status}.", "success")
    return redirect(url_for("faculty.dashboard"))


@faculty_bp.route("/session/<int:session_id>/cancel", methods=["POST"])
@login_required
@role_required("faculty")
def cancel_session(session_id):
    session = db.get_or_404(AttendanceSession, session_id)
    faculty_profile = current_user.faculty_profile
    if faculty_profile is None or session.faculty_id != faculty_profile.id:
        flash("You may only manage your own sessions.", "error")
        return redirect(url_for("faculty.dashboard"))
    if session.status not in {"active", "closed"}:
        flash("Only active or closed sessions can be cancelled.", "error")
        return redirect(url_for("faculty.dashboard"))

    session.status = "cancelled"
    session.end_time = session.end_time or datetime.utcnow()
    QRToken.query.filter_by(session_id=session.id, is_active=True).update({"is_active": False})
    db.session.commit()
    AuditLog.create_event(current_user.id, "session_cancelled", details=f"Session {session.id} cancelled.")
    flash("Session cancelled; it will not count toward attendance percentages.", "success")
    return redirect(url_for("faculty.dashboard"))


@faculty_bp.route("/session/<int:session_id>/qr", methods=["POST"])
@login_required
@role_required("faculty")
@limiter.limit("30 per minute")
def get_qr(session_id):
    session = db.get_or_404(AttendanceSession, session_id)
    if session.faculty_id != current_user.faculty_profile.id:
        flash("You may only view your own QR codes.", "error")
        return redirect(url_for("faculty.dashboard"))
    if session.status != "active":
        return jsonify({"ok": False, "message": "QR codes are available only for active sessions."}), 409
    payload = generate_qr_payload(session.id)
    AuditLog.create_event(current_user.id, "qr_generated", details=f"QR token generated for session {session.id}.")
    return jsonify({
        "session_id": session.id,
        "token": json.dumps(payload, separators=(",", ":"), sort_keys=True),
        "qr_image": generate_qr_image(payload),
        "refresh_seconds": max(current_app.config["QR_TOKEN_TTL_SECONDS"] - 1, 1),
    })


@faculty_bp.route("/correction/<int:correction_id>/review", methods=["POST"])
@login_required
@role_required("faculty")
def review_correction(correction_id):
    correction = AttendanceCorrectionRequest.query.get_or_404(correction_id)
    record = db.session.get(AttendanceRecord, correction.record_id)
    faculty_profile = current_user.faculty_profile
    session = db.session.get(AttendanceSession, record.session_id) if record else None
    if faculty_profile is None or session is None or session.faculty_id != faculty_profile.id:
        flash("You may only review correction requests for your own sessions.", "error")
        return redirect(url_for("faculty.dashboard"))
    decision = request.form.get("decision")
    if correction.status != "pending" or decision not in {"approved", "rejected"}:
        flash("This correction request cannot be updated.", "error")
        return redirect(url_for("faculty.dashboard", _anchor="corrections"))
    correction.status = decision
    db.session.commit()
    AuditLog.create_event(
        current_user.id,
        "attendance_correction_reviewed",
        details=f"Correction request {correction.id} {decision} by assigned faculty.",
        success=True,
    )
    flash("Correction request updated. The verified attendance record was not edited.", "success")
    return redirect(url_for("faculty.dashboard", _anchor="corrections"))


@faculty_bp.route("/attendance.csv")
@login_required
@role_required("faculty")
def export_attendance_csv():
    faculty_profile = current_user.faculty_profile
    if faculty_profile is None:
        return redirect(url_for("faculty.dashboard"))
    query = AttendanceRecord.query.join(AttendanceSession).filter(AttendanceSession.faculty_id == faculty_profile.id)
    subject_filter = request.args.get("subject_id", "")
    if subject_filter.isdigit():
        assigned = FacultySubjectAssignment.query.filter_by(
            faculty_id=faculty_profile.id,
            subject_id=int(subject_filter),
        ).first()
        if assigned:
            query = query.filter(AttendanceSession.subject_id == int(subject_filter))
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
                query = query.filter(AttendanceRecord.checked_in_at >= day)
            else:
                query = query.filter(AttendanceRecord.checked_in_at < day + timedelta(days=1))
    if search:
        query = query.join(StudentProfile).join(User).filter(
            or_(User.full_name.ilike(f"%{search}%"), StudentProfile.student_number.ilike(f"%{search}%"))
        )
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["session", "subject", "student", "register_number", "attendance_status", "checked_in_utc", "verification"])
    for record in query.order_by(AttendanceRecord.checked_in_at.desc()).limit(10000).all():
        writer.writerow(map(safe_csv_cell, [
            record.session.title,
            record.session.subject.code,
            record.student.user.full_name,
            record.student.student_number or "",
            record.status,
            record.checked_in_at.isoformat(timespec="seconds") + "Z",
            "verified" if record.token_id else "legacy/unverified",
        ]))
    response = make_response(output.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = "attachment; filename=faculty-attendance.csv"
    response.headers["Cache-Control"] = "no-store"
    AuditLog.create_event(current_user.id, "faculty_attendance_exported", details="Faculty attendance report exported.")
    return response
