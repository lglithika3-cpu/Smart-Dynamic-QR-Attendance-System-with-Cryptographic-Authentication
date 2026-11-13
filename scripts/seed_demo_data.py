import os
from datetime import datetime

from flask import has_app_context

from app import create_app
from app.extensions import db
from app.models.models import (
    Course,
    Department,
    FacultyProfile,
    FacultySubjectAssignment,
    StudentEnrollment,
    StudentProfile,
    Subject,
    User,
)


def seed_demo_data(app=None, *, student_count=63, faculty_count=8):
    app = app or create_app()
    context = None
    if not has_app_context():
        context = app.app_context()
        context.push()

    try:
        department = Department.query.filter_by(code="CS").first() or Department(name="Computer Science", code="CS")
        if department.id is None:
            db.session.add(department)
            db.session.flush()

        course = Course.query.filter_by(code="BSC-CS").first() or Course(name="BSc Computer Science", code="BSC-CS", department_id=department.id)
        if course.id is None:
            db.session.add(course)
            db.session.flush()

        subject_names = [
            ("CS101", "Cyber Security"),
            ("CS102", "Java Programming"),
            ("CS103", "Computer Networks"),
            ("CS104", "Data Structures and Algorithms"),
            ("CS105", "Database Management Systems"),
            ("CS106", "Operating Systems"),
            ("CS107", "Software Engineering"),
            ("CS108", "Web Development"),
            ("CS109", "Cryptography"),
            ("CS110", "Cloud Computing"),
        ]
        subjects = []
        for code, name in subject_names:
            subject = Subject.query.filter_by(code=code).first() or Subject(code=code, name=name, course_id=course.id)
            if subject.id is None:
                db.session.add(subject)
                db.session.flush()
            subjects.append(subject)

        admin_email = os.getenv("ADMIN_EMAIL", "admin@example.com").strip().lower()
        admin_user = User.query.filter_by(email=admin_email).first()
        if admin_user is None:
            admin_user = User(email=admin_email, full_name="System Administrator", role="admin")
            admin_password = os.getenv("ADMIN_PASSWORD", "ChangeMe123!")
            admin_user.password = admin_password
            admin_user.is_active = True
            db.session.add(admin_user)
            db.session.flush()

        # Ensure the required faculty count exists without overwriting existing accounts.
        for index in range(1, faculty_count + 1):
            email = f"faculty{index:02d}@example.com"
            user = User.query.filter_by(email=email).first()
            if user is None:
                user = User(email=email, full_name=f"Demo Faculty {index:02d}", role="faculty")
                user.password = "Faculty123!"
                user.is_active = True
                db.session.add(user)
                db.session.flush()

            profile = user.faculty_profile or FacultyProfile(user_id=user.id, department_id=department.id)
            profile.department_id = department.id
            profile.is_active = True
            profile.designation = f"Demo Faculty {index:02d}"
            if not profile.employee_id:
                profile.employee_id = f"FAC-{index:03d}"
            if user.id and profile.id is None:
                db.session.add(profile)
            db.session.flush()

            subject = subjects[(index - 1) % len(subjects)]
            if not FacultySubjectAssignment.query.filter_by(faculty_id=profile.id, subject_id=subject.id).first():
                db.session.add(FacultySubjectAssignment(faculty_id=profile.id, subject_id=subject.id))

        # Ensure the required student count exists without overwriting existing records.
        used_student_numbers = {profile.student_number for profile in StudentProfile.query.filter(StudentProfile.student_number.isnot(None)).all()}
        for index in range(1, student_count + 1):
            email = f"demo.student{index:03d}@example.com"
            user = User.query.filter_by(email=email).first()
            if user is None:
                user = User(email=email, full_name=f"Demo Student {index:03d}", role="student")
                user.password = "Student123!"
                user.is_active = True
                db.session.add(user)
                db.session.flush()

            profile = user.student_profile or StudentProfile(user_id=user.id, department_id=department.id)
            profile.department_id = department.id
            profile.approved = True
            profile.status = "approved"
            student_number = f"REG-{index:05d}"
            if not profile.student_number:
                profile.student_number = student_number
            elif profile.student_number in used_student_numbers and profile.student_number != student_number:
                profile.student_number = student_number
            used_student_numbers.add(profile.student_number)
            if user.id and profile.id is None:
                db.session.add(profile)
            db.session.flush()

            for subject in subjects:
                if not StudentEnrollment.query.filter_by(student_id=profile.id, subject_id=subject.id).first():
                    db.session.add(StudentEnrollment(student_id=profile.id, subject_id=subject.id))

        db.session.commit()
        print(f"Demo data loaded successfully: {student_count} students, {faculty_count} faculty, and one admin.")
    finally:
        if context is not None:
            context.pop()


if __name__ == "__main__":
    seed_demo_data()
