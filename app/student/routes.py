from datetime import date, datetime, timedelta
from io import BytesIO

from flask import Blueprint, current_app, flash, jsonify, make_response, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy import or_
from xml.sax.saxutils import escape

from app.extensions import db, limiter
from app.models.models import (
    Announcement,
    AttendanceCorrectionRequest,
    AttendanceRecord,
    AttendanceSession,
    AuditLog,
    StudentEnrollment,
)
from app.services.attendance_analytics import calculate_attendance_percentage
from app.services.qr_service import validate_student_attendance

student_bp = Blueprint("student", __name__)


@student_bp.route("/dashboard")
@login_required
def dashboard():
    if current_user.role != "student":
        flash("Only students can access this page.", "error")
        return redirect(url_for("index"))

    profile = current_user.student_profile
    enrollments = StudentEnrollment.query.filter_by(student_id=profile.id).all() if profile else []
    subject_ids = [e.subject_id for e in enrollments]
    sessions = AttendanceSession.query.filter(
        AttendanceSession.subject_id.in_(subject_ids),
        AttendanceSession.status == "active",
        AttendanceSession.end_time.is_(None),
    ).order_by(AttendanceSession.start_time.desc()).all() if subject_ids else []
    records = (
        AttendanceRecord.query.filter_by(student_id=profile.id)
        .order_by(AttendanceRecord.checked_in_at.desc())
        .all()
        if profile
        else []
    )
    percentage = calculate_attendance_percentage(profile.id) if profile else 0.0
    completed_sessions = []
    if subject_ids:
        completed_sessions = AttendanceSession.query.filter(
            AttendanceSession.subject_id.in_(subject_ids),
            AttendanceSession.status == "finalized",
        ).all()
    completed_session_ids = [session.id for session in completed_sessions]
    attended_records = (
        AttendanceRecord.query.filter_by(student_id=profile.id)
        .filter(AttendanceRecord.session_id.in_(completed_session_ids))
        .filter(AttendanceRecord.status == "present")
        .all()
        if profile and completed_session_ids
        else []
    )
    subject_summaries = []
    for enrollment in enrollments:
        subject_sessions = [item for item in completed_sessions if item.subject_id == enrollment.subject_id]
        subject_session_ids = {item.id for item in subject_sessions}
        attended = sum(1 for record in attended_records if record.session_id in subject_session_ids)
        total = len(subject_sessions)
        assignments = enrollment.subject.faculty_assignments
        faculty_names = ", ".join(
            assignment.faculty.user.full_name
            for assignment in assignments
            if assignment.faculty and assignment.faculty.user
        )
        subject_summaries.append({
            "enrollment": enrollment,
            "conducted": total,
            "attended": attended,
            "missed": max(total - attended, 0),
            "percentage": round(attended * 100 / total, 2) if total else 0.0,
            "faculty_names": faculty_names or "Not assigned",
        })
    latest_attendance = records[0] if records else None
    attendance_threshold = current_app.config["ATTENDANCE_ELIGIBILITY_THRESHOLD"]
    now = datetime.utcnow()
    announcements = Announcement.query.filter_by(is_active=True).filter(
        Announcement.starts_at <= now + timedelta(days=30),
        or_(Announcement.ends_at.is_(None), Announcement.ends_at > now),
    ).order_by(Announcement.starts_at.asc()).all()
    notifications = []
    if completed_sessions and percentage < attendance_threshold:
        notifications.append({
            "kind": "warning",
            "message": f"Attendance is below the {attendance_threshold}% eligibility threshold.",
        })
    for record in records[:5]:
        if record.checked_in_at and (datetime.utcnow() - record.checked_in_at).days < 1:
            notifications.append({
                "kind": "success",
                "message": f"Attendance marked for {record.session.subject.name} ({record.session.title}).",
            })

    selected_month = (request.args.get("month") or "").strip()
    monthly_total = monthly_attended = 0
    if selected_month:
        try:
            year, month = (int(part) for part in selected_month.split("-", 1))
            month_start = date(year, month, 1)
            month_end = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
        except (ValueError, TypeError):
            flash("Choose a valid month for the monthly summary.", "error")
            selected_month = ""
        else:
            for item in completed_sessions:
                session_date = item.start_time or item.created_at
                if session_date and month_start <= session_date.date() < month_end:
                    monthly_total += 1
                    if any(
                        record.session_id == item.id and record.status == "present"
                        for record in attended_records
                    ):
                        monthly_attended += 1
    monthly_percentage = round(monthly_attended * 100 / monthly_total, 2) if monthly_total else 0.0

    correction_requests = (
        AttendanceCorrectionRequest.query.filter_by(requester_id=current_user.id)
        .order_by(AttendanceCorrectionRequest.created_at.desc())
        .all()
    )
    security_events = (
        AuditLog.query.filter(
            AuditLog.user_id == current_user.id,
            AuditLog.event_type.in_(["qr_invalid", "qr_expired", "attendance_duplicate"]),
        )
        .order_by(AuditLog.created_at.desc())
        .limit(20)
        .all()
    )
    security_alerts = (
        AuditLog.query.filter(
            AuditLog.user_id == current_user.id,
            AuditLog.event_type.in_(["password_changed", "password_reset_completed", "login_failure", "totp_disabled"]),
        )
        .order_by(AuditLog.created_at.desc())
        .limit(5)
        .all()
    )
    return render_template(
        "student/dashboard.html",
        enrollments=enrollments,
        sessions=sessions,
        records=records,
        attendance_percentage=percentage,
        total_classes=len(completed_sessions),
        classes_attended=len(attended_records),
        classes_missed=max(len(completed_sessions) - len(attended_records), 0),
        subject_summaries=subject_summaries,
        correction_requests=correction_requests,
        security_events=security_events,
        latest_attendance=latest_attendance,
        attendance_threshold=attendance_threshold,
        attendance_eligible=bool(completed_sessions) and percentage >= attendance_threshold,
        notifications=notifications,
        selected_month=selected_month,
        monthly_total=monthly_total,
        monthly_attended=monthly_attended,
        monthly_percentage=monthly_percentage,
        security_alerts=security_alerts,
        announcements=announcements,
        now=now,
        security_protected=(
            current_user.is_active
            and current_user.password_hash.startswith("scrypt:")
            and current_user.totp_enabled
            and current_app.config["SESSION_COOKIE_SECURE"]
            and request.is_secure
        ),
    )


@student_bp.route("/subject/<int:subject_id>")
@login_required
def subject_history(subject_id):
    if current_user.role != "student" or current_user.student_profile is None:
        flash("Only students can view subject attendance.", "error")
        return redirect(url_for("index"))
    profile = current_user.student_profile
    enrollment = StudentEnrollment.query.filter_by(
        student_id=profile.id,
        subject_id=subject_id,
    ).first_or_404()
    sessions = (
        AttendanceSession.query.filter_by(subject_id=subject_id, status="finalized")
        .order_by(AttendanceSession.start_time.desc(), AttendanceSession.created_at.desc())
        .all()
    )
    records_by_session = {
        record.session_id: record
        for record in AttendanceRecord.query.filter_by(student_id=profile.id)
        .filter(AttendanceRecord.session_id.in_([item.id for item in sessions] or [-1]))
        .all()
    }
    history = [
        {
            "session": attendance_session,
            "record": records_by_session.get(attendance_session.id),
            "status": "Present" if records_by_session.get(attendance_session.id) and records_by_session[attendance_session.id].status == "present" else "Absent",
        }
        for attendance_session in sessions
    ]
    present_count = sum(item["status"] == "Present" for item in history)
    percentage = round(present_count * 100 / len(history), 2) if history else 0.0
    faculty_names = ", ".join(
        assignment.faculty.user.full_name
        for assignment in enrollment.subject.faculty_assignments
        if assignment.faculty and assignment.faculty.user
    ) or "Not assigned"
    return render_template(
        "student/subject_history.html",
        subject=enrollment.subject,
        history=history,
        attended=present_count,
        absent=max(len(history) - present_count, 0),
        percentage=percentage,
        faculty_names=faculty_names,
    )


@student_bp.route("/report.pdf")
@login_required
def attendance_report():
    if current_user.role != "student" or current_user.student_profile is None:
        flash("Only students can download attendance reports.", "error")
        return redirect(url_for("index"))
    profile = current_user.student_profile
    month = (request.args.get("month") or "").strip()
    month_start = month_end = None
    if month:
        try:
            year, month_number = (int(part) for part in month.split("-", 1))
            month_start = date(year, month_number, 1)
            month_end = date(year + (month_number == 12), 1 if month_number == 12 else month_number + 1, 1)
        except (ValueError, TypeError):
            flash("Choose a valid month for the report.", "error")
            return redirect(url_for("student.dashboard"))

    enrollments = StudentEnrollment.query.filter_by(student_id=profile.id).all()
    subject_ids = [enrollment.subject_id for enrollment in enrollments]
    sessions = (
        AttendanceSession.query.filter(
            AttendanceSession.subject_id.in_(subject_ids),
            AttendanceSession.status == "finalized",
        ).order_by(AttendanceSession.start_time, AttendanceSession.created_at).all()
        if subject_ids
        else []
    )
    records = {
        record.session_id: record
        for record in AttendanceRecord.query.filter_by(student_id=profile.id).all()
    }
    if month_start:
        sessions = [
            item for item in sessions
            if (item.start_time or item.created_at)
            and month_start <= (item.start_time or item.created_at).date() < month_end
        ]

    buffer = BytesIO()
    document = SimpleDocTemplate(buffer, pagesize=landscape(letter), rightMargin=28, leftMargin=28, topMargin=28, bottomMargin=28)
    styles = getSampleStyleSheet()
    safe = lambda value: Paragraph(escape(str(value or "")), styles["BodyText"])
    rows = [[safe(value) for value in ("Date", "Subject", "Code", "Faculty", "Class / Period", "Status", "Marked at", "QR verification")]]
    for item in sessions:
        record = records.get(item.id)
        checked_in = record.checked_in_at.strftime("%Y-%m-%d %H:%M UTC") if record else "-"
        faculty_name = item.faculty.user.full_name if item.faculty and item.faculty.user else "Not assigned"
        rows.append([
            safe((item.start_time or item.created_at).strftime("%Y-%m-%d") if (item.start_time or item.created_at) else "-"),
            safe(item.subject.name),
            safe(item.subject.code),
            safe(faculty_name),
            safe(item.title),
            safe(record.status.title() if record else "Absent"),
            safe(checked_in),
            safe("Successful" if record and record.token_id else ("Legacy record" if record else "-")),
        ])
    story = [
        Paragraph("Student Attendance Report", styles["Title"]),
        Paragraph(escape(f"{current_user.full_name} · {profile.student_number or 'No register number'} · {month or 'All available records'}"), styles["Normal"]),
        Spacer(1, 12),
    ]
    summary_rows = [[safe(value) for value in ("Subject", "Code", "Conducted", "Attended", "Absent", "Percentage")]]
    for enrollment in enrollments:
        subject_sessions = [item for item in sessions if item.subject_id == enrollment.subject_id]
        subject_attended = sum(
            1
            for item in subject_sessions
            if records.get(item.id) and records[item.id].status == "present"
        )
        subject_total = len(subject_sessions)
        subject_percentage = round(subject_attended * 100 / subject_total, 2) if subject_total else 0
        summary_rows.append([
            safe(enrollment.subject.name),
            safe(enrollment.subject.code),
            safe(subject_total),
            safe(subject_attended),
            safe(subject_total - subject_attended),
            safe(f"{subject_percentage}%"),
        ])
    overall_attended = sum(
        1 for item in sessions
        if records.get(item.id) and records[item.id].status == "present"
    )
    overall_percentage = round(overall_attended * 100 / len(sessions), 2) if sessions else 0
    story.append(Paragraph(
        escape(f"Overall: {len(sessions)} classes · {overall_attended} attended · "
               f"{max(len(sessions) - overall_attended, 0)} absent · {overall_percentage}%"),
        styles["Heading2"],
    ))
    if len(summary_rows) > 1:
        summary_table = Table(summary_rows, repeatRows=1, colWidths=[2.3*inch, 0.8*inch, 0.9*inch, 0.9*inch, 0.8*inch, 1.0*inch])
        summary_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#102b4e")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#dce6ee")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f3f7fa")]),
        ]))
        story.extend([summary_table, Spacer(1, 12)])
    if len(rows) == 1:
        story.append(Paragraph("No finalized attendance sessions were found for this report period.", styles["BodyText"]))
    else:
        table = Table(rows, repeatRows=1, colWidths=[0.8*inch, 1.3*inch, 0.65*inch, 1.25*inch, 1.3*inch, 0.65*inch, 1.2*inch, 1.1*inch])
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#102b4e")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#dce6ee")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f3f7fa")]),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(table)
    document.build(story)
    response = make_response(buffer.getvalue())
    response.headers["Content-Type"] = "application/pdf"
    response.headers["Content-Disposition"] = "attachment; filename=student-attendance-report.pdf"
    response.headers["Cache-Control"] = "private, no-store"
    return response


@student_bp.route("/scan")
@login_required
def scan():
    if current_user.role != "student":
        flash("Only students can scan attendance codes.", "error")
        return redirect(url_for("index"))
    return render_template("attendance/scan.html")


@student_bp.route("/submit-attendance", methods=["POST"])
@login_required
@limiter.limit("30 per minute")
def submit_attendance():
    if current_user.role != "student":
        return jsonify({"ok": False, "message": "Only students can submit attendance."}), 403
    payload = request.get_json(silent=True) or request.form
    qr_value = (payload.get("token") if isinstance(payload, dict) else None) or request.form.get("token")

    if not qr_value:
        return jsonify({"ok": False, "message": "Missing QR code data."}), 400

    try:
        status, record = validate_student_attendance(current_user, qr_value)
        if status == "recorded":
            AuditLog.create_event(current_user.id, "attendance_recorded", details=f"Attendance recorded for session {record.session_id}.")
            return jsonify({"ok": True, "message": "Attendance recorded successfully."})
        if status == "already-recorded":
            AuditLog.create_event(
                current_user.id,
                "attendance_duplicate",
                details=f"Duplicate attendance attempt for session {record.session_id}.",
                success=False,
            )
            return jsonify({"ok": True, "message": "Attendance already recorded for this session."})
        return jsonify({"ok": False, "message": "Attendance rejected."}), 400
    except ValueError as exc:
        message = str(exc)
        event_type = "qr_expired" if "expir" in message.lower() else "qr_invalid"
        AuditLog.create_event(current_user.id, event_type, details="QR attendance verification failed.", success=False)
        return jsonify({"ok": False, "message": str(exc)}), 400


@student_bp.route("/attendance/<int:record_id>/correction", methods=["POST"])
@login_required
def request_attendance_correction(record_id):
    if current_user.role != "student":
        flash("Only students can request attendance corrections.", "error")
        return redirect(url_for("index"))

    profile = current_user.student_profile
    record = (
        AttendanceRecord.query.filter_by(id=record_id, student_id=profile.id).first_or_404()
        if profile
        else None
    )
    reason = (request.form.get("reason") or "").strip()
    if not reason or len(reason) > 500:
        flash("Enter a correction reason of up to 500 characters.", "error")
        return redirect(url_for("student.dashboard", _anchor="history"))

    pending = AttendanceCorrectionRequest.query.filter_by(
        record_id=record.id,
        requester_id=current_user.id,
        status="pending",
    ).first()
    if pending is None:
        correction = AttendanceCorrectionRequest(
            record_id=record.id,
            requester_id=current_user.id,
            reason=reason,
            status="pending",
        )
        db.session.add(correction)
        db.session.commit()
        AuditLog.create_event(
            current_user.id,
            "attendance_correction_requested",
            details=f"Correction requested for attendance record {record.id}.",
            success=True,
        )
        flash("Correction request submitted for review.", "success")
    else:
        flash("A correction request for this record is already pending.", "info")
    return redirect(url_for("student.dashboard", _anchor="history"))
