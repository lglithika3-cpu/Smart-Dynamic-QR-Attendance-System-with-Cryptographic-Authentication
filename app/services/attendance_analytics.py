from app.models.models import AttendanceRecord, AttendanceSession, StudentEnrollment


def calculate_attendance_percentage(student_id, subject_id=None):
    sessions = AttendanceSession.query.filter(AttendanceSession.status == "finalized")
    if subject_id:
        sessions = sessions.filter_by(subject_id=subject_id)
    eligible = 0
    attended = 0
    for session in sessions.all():
        if not StudentEnrollment.query.filter_by(student_id=student_id, subject_id=session.subject_id).first():
            continue
        eligible += 1
        if AttendanceRecord.query.filter_by(
            student_id=student_id,
            session_id=session.id,
            status="present",
        ).first():
            attended += 1
    if eligible == 0:
        return 0.0
    return round((attended / eligible) * 100, 2)
