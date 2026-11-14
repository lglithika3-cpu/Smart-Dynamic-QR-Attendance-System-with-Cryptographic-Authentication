import ssl
from io import BytesIO

import pytest
from PIL import Image
from sqlalchemy import Column, ForeignKey, Integer, MetaData, String, Table, create_engine, insert, make_url, select

from app import create_app
from app.extensions import db
from app.config import (
    _database_url_from_environment,
    _normalize_database_url,
    _sqlalchemy_engine_options,
)
from app.models.models import FacultyProfile, StudentEnrollment, StudentProfile, Subject, User
from scripts.migrate_sqlite_to_database import (
    _reset_postgresql_sequences,
    copy_database_data,
)
from scripts.seed_demo_data import seed_demo_data


def test_sqlite_to_database_copy_preserves_rows_and_leaves_migration_version_alone(tmp_path):
    source_engine = create_engine(f"sqlite:///{tmp_path / 'source.db'}")
    target_engine = create_engine(f"sqlite:///{tmp_path / 'target.db'}")
    metadata = MetaData()
    users = Table(
        "users",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("email", String(255), nullable=False),
    )
    attendance = Table(
        "attendance",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("user_id", Integer, ForeignKey("users.id"), nullable=False),
    )
    versions = Table("alembic_version", metadata, Column("version_num", String(32), primary_key=True))
    metadata.create_all(source_engine)
    metadata.create_all(target_engine)
    with source_engine.begin() as connection:
        connection.execute(insert(users), {"id": 7, "email": "student@example.com"})
        connection.execute(insert(attendance), {"id": 11, "user_id": 7})
        connection.execute(insert(versions), {"version_num": "sqlite-head"})
    with target_engine.begin() as connection:
        connection.execute(insert(versions), {"version_num": "mysql-head"})

    copied = copy_database_data(source_engine, target_engine)

    with target_engine.connect() as connection:
        assert connection.execute(select(users.c.id, users.c.email)).all() == [
            (7, "student@example.com")
        ]
        assert connection.execute(select(attendance.c.id, attendance.c.user_id)).all() == [(11, 7)]
        assert connection.execute(select(versions.c.version_num)).scalar_one() == "mysql-head"
    assert copied == {"attendance": 1, "users": 1}
    source_engine.dispose()
    target_engine.dispose()


def test_sqlite_to_database_copy_refuses_nonempty_target(tmp_path):
    source_engine = create_engine(f"sqlite:///{tmp_path / 'source.db'}")
    target_engine = create_engine(f"sqlite:///{tmp_path / 'target.db'}")
    metadata = MetaData()
    users = Table(
        "users",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("email", String(255), nullable=False),
    )
    metadata.create_all(source_engine)
    metadata.create_all(target_engine)
    with target_engine.begin() as connection:
        connection.execute(insert(users), {"id": 1, "email": "existing@example.com"})

    with pytest.raises(ValueError, match="not empty"):
        copy_database_data(source_engine, target_engine)

    with target_engine.connect() as connection:
        assert connection.execute(select(users.c.email)).scalar_one() == "existing@example.com"
    source_engine.dispose()
    target_engine.dispose()


def test_mysql_database_url_uses_pymysql_driver():
    url = _normalize_database_url("mysql://user:password@db.example/app?charset=utf8mb4")
    assert url == "mysql+pymysql://user:password@db.example/app?charset=utf8mb4"


def test_mysql_environment_url_encodes_credentials(monkeypatch):
    monkeypatch.setenv("DB_HOST", "mysql.example")
    monkeypatch.setenv("DB_PORT", "27887")
    monkeypatch.setenv("DB_NAME", "defaultdb")
    monkeypatch.setenv("DB_USER", "avnadmin")
    monkeypatch.setenv("DB_PASSWORD", "p@ss:/?#word")

    url = make_url(_database_url_from_environment())

    assert url.drivername == "mysql+pymysql"
    assert url.host == "mysql.example"
    assert url.port == 27887
    assert url.database == "defaultdb"
    assert url.password == "p@ss:/?#word"
    assert url.query["charset"] == "utf8mb4"


def test_mysql_engine_uses_verified_aiven_ca(monkeypatch, tmp_path):
    ca_file = tmp_path / "aiven-ca.pem"
    ca_file.write_text("test certificate path")
    create_default_context = ssl.create_default_context
    loaded_ca_files = []

    def create_context(*, cafile):
        loaded_ca_files.append(cafile)
        return create_default_context()

    monkeypatch.setattr("app.config.ssl.create_default_context", create_context)
    options = _sqlalchemy_engine_options(
        "mysql+pymysql://avnadmin@mysql.example/defaultdb",
        ssl_ca=str(ca_file),
    )

    ssl_context = options["connect_args"]["ssl"]
    assert loaded_ca_files == [str(ca_file)]
    assert ssl_context.verify_mode == ssl.CERT_REQUIRED
    assert ssl_context.check_hostname is True


def test_mysql_engine_refuses_missing_ca_or_disabled_ssl(tmp_path):
    database_url = "mysql+pymysql://avnadmin@mysql.example/defaultdb"
    with pytest.raises(RuntimeError, match="DATABASE_SSL_CA"):
        _sqlalchemy_engine_options(database_url)
    with pytest.raises(RuntimeError, match="DB_SSL_MODE=REQUIRED"):
        _sqlalchemy_engine_options(database_url, ssl_ca=str(tmp_path), ssl_mode="DISABLED")


def test_postgresql_database_url_uses_psycopg_and_tls():
    url = _normalize_database_url("postgres://USER:PASSWORD@db.example/app")
    assert url == (
        "postgresql+psycopg://USER:PASSWORD@db.example/app?sslmode=require"
    )


def test_postgresql_database_url_uses_ca_verification():
    url = _normalize_database_url(
        "postgresql://USER:PASSWORD@db.example/app?sslmode=require",
        ssl_ca="C:\\certs\\aiven-ca.pem",
    )
    assert "sslmode=verify-full" in url
    assert "sslrootcert=C%3A%5Ccerts%5Caiven-ca.pem" in url


def test_postgresql_sequence_is_advanced_after_import():
    users = Table(
        "users",
        MetaData(),
        Column("id", Integer, primary_key=True),
    )

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

        def scalar_one(self):
            return self.value

    class Connection:
        def __init__(self):
            self.calls = []

        def execute(self, statement, params=None):
            sql = str(statement)
            self.calls.append((sql, params))
            if "pg_get_serial_sequence" in sql:
                return Result("public.users_id_seq")
            if "max(" in sql.lower():
                return Result(17)
            return Result(None)

    connection = Connection()
    _reset_postgresql_sequences(connection, [users])

    sequence_update = next(call for call in connection.calls if "setval" in call[0])
    assert sequence_update[1] == {
        "sequence_name": "public.users_id_seq",
        "last_value": 17,
    }


@pytest.fixture
def app_client():
    app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "WTF_CSRF_ENABLED": False,
    })
    with app.app_context():
        from app import db
        db.create_all()
    yield app.test_client()


def test_login_page_loads(app_client):
    response = app_client.get("/login")
    assert response.status_code == 200
    assert b"Smart Dynamic Attendance System" in response.data
    assert b"Secure and Efficient Attendance Management" in response.data
    assert b"Save your login details in a secure password manager." in response.data
    assert b"images/login-campus.jpg" in response.data
    assert app_client.get("/static/images/login-campus.jpg").status_code == 200
    assert app_client.get("/static/images/login-study-fallback.jpg").status_code == 200
    assert b"Dashboard" not in response.data


def test_student_registration_creates_pending_account(app_client):
    response = app_client.post("/register", data={
        "full_name": "New Student",
        "email": "new.student@example.com",
        "password": "StudentPassword123!",
        "confirm_password": "StudentPassword123!",
    })
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")

    with app_client.application.app_context():
        user = User.query.filter_by(email="new.student@example.com").one()
        assert user.role == "student"
        assert user.student_profile is not None
        assert user.student_profile.approved is False


def test_admin_dashboard_hides_attendance_records_and_shows_login_guidance():
    app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "WTF_CSRF_ENABLED": False,
    })
    with app.app_context():
        db.create_all()
        admin = User(email="dashboard.admin@example.com", full_name="Dashboard Admin", role="admin")
        admin.password = "AdminPassword123!"
        db.session.add(admin)
        db.session.commit()

    client = app.test_client()
    login = client.post("/login", data={"email": "dashboard.admin@example.com", "password": "AdminPassword123!"})
    assert login.status_code == 302

    dashboard = client.get("/admin/dashboard")
    assert dashboard.status_code == 200
    assert b"Recent attendance records" not in dashboard.data
    assert b"Attendance records" not in dashboard.data
    assert b"attendance-records" not in dashboard.data
    assert b"Use the account email as the login username." in dashboard.data
    assert b"demo.student001@example.com" in dashboard.data
    assert b"Student123!" in dashboard.data
    assert b"faculty01@example.com" in dashboard.data
    assert b"Faculty123!" in dashboard.data
    assert b"dashboard.admin@example.com" in dashboard.data
    assert b"ADMIN_PASSWORD" in dashboard.data


def test_student_scan_page_explains_secure_camera_requirements():
    app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "WTF_CSRF_ENABLED": False,
    })
    with app.app_context():
        db.create_all()
        student = User(email="scan.student@example.com", full_name="Scan Student", role="student")
        student.password = "StudentPassword123!"
        db.session.add(student)
        db.session.flush()
        db.session.add(StudentProfile(user_id=student.id, approved=True, status="approved"))
        db.session.commit()

    client = app.test_client()
    login = client.post("/login", data={"email": "scan.student@example.com", "password": "StudentPassword123!"})
    assert login.status_code == 302
    scan_page = client.get("/student/scan")
    assert scan_page.status_code == 200
    assert b"Camera access requires HTTPS or localhost." in scan_page.data
    assert b"allow camera permission in your browser." in scan_page.data
    assert scan_page.data.count(b"/static/js/app.js") == 1


def test_login_by_student_or_faculty_id_redirects_to_role_workspace():
    app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "WTF_CSRF_ENABLED": False,
    })
    with app.app_context():
        db.create_all()
        student = User(email="identifier.student@example.com", full_name="Student ID", role="student")
        student.password = "Student123!"
        faculty = User(email="identifier.faculty@example.com", full_name="Faculty ID", role="faculty")
        faculty.password = "Faculty123!"
        admin = User(email="identifier.admin@example.com", full_name="Admin ID", role="admin")
        admin.password = "AdminPassword123!"
        db.session.add_all([student, faculty, admin])
        db.session.flush()
        student_profile = StudentProfile(user_id=student.id, student_number="REG-4401", approved=True, status="approved")
        faculty_profile = FacultyProfile(user_id=faculty.id, employee_id="FAC-4401")
        db.session.add_all([student_profile, faculty_profile])
        db.session.commit()

    client = app.test_client()
    student_login = client.post("/login", data={"email": "reg-4401", "password": "Student123!"})
    assert student_login.status_code == 302
    assert student_login.headers["Location"].endswith("/student/dashboard")

    client.post("/logout")
    faculty_login = client.post("/login", data={"email": "fac-4401", "password": "Faculty123!"})
    assert faculty_login.status_code == 302
    assert faculty_login.headers["Location"].endswith("/faculty/dashboard")

    client.post("/logout")
    admin_login = client.post("/login", data={"email": "identifier.admin@example.com", "password": "AdminPassword123!"})
    assert admin_login.status_code == 302
    assert admin_login.headers["Location"].endswith("/admin/dashboard")


def test_root_redirects_to_login():
    app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "WTF_CSRF_ENABLED": False,
    })
    with app.app_context():
        from app import db
        db.create_all()
    client = app.test_client()
    response = client.get("/", follow_redirects=False)
    assert response.status_code in (302, 307)


def test_health_endpoint_checks_database_connection(app_client):
    response = app_client.get("/attendance/health")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok", "database": "ok"}


def test_seed_demo_data_creates_safe_demo_roster():
    app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "WTF_CSRF_ENABLED": False,
    })
    with app.app_context():
        db.create_all()
        seed_demo_data(app=app)

        total_users = db.session.query(User).count()
        student_profiles = db.session.query(StudentProfile).count()
        faculty_users = db.session.query(User).filter_by(role="faculty").count()
        admin_users = db.session.query(User).filter_by(role="admin").count()
        subjects = db.session.query(Subject).count()
        enrollments = db.session.query(StudentEnrollment).count()

    assert student_profiles == 63
    assert faculty_users == 8
    assert admin_users >= 1
    assert total_users >= 72
    assert subjects == 10
    assert enrollments == 63 * 10

    with app.app_context():
        seed_demo_data(app=app)
        student_profiles_after = db.session.query(StudentProfile).count()
        faculty_users_after = db.session.query(User).filter_by(role="faculty").count()

    assert student_profiles_after == 63
    assert faculty_users_after == 8


def test_student_profile_photo_is_private_and_owner_scoped():
    app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "WTF_CSRF_ENABLED": False,
    })
    with app.app_context():
        db.create_all()
        user = User(email="photo.student@example.com", full_name="Photo Student", role="student")
        user.password = "StudentPassword123!"
        db.session.add(user)
        db.session.flush()
        profile = StudentProfile(user_id=user.id, student_number="REG-PHOTO", approved=True, status="approved")
        db.session.add(profile)
        outsider = User(email="photo.outsider@example.com", full_name="Other Student", role="student")
        outsider.password = "OtherPassword123!"
        db.session.add(outsider)
        db.session.flush()
        db.session.add(StudentProfile(user_id=outsider.id, student_number="REG-OTHER", approved=True, status="approved"))
        db.session.commit()
        profile_id = profile.id
        user_id = user.id

    client = app.test_client()
    login = client.post("/login", data={"email": "photo.student@example.com", "password": "StudentPassword123!"})
    assert login.status_code == 302
    image_data = BytesIO()
    Image.new("RGB", (2, 2), color=(20, 80, 160)).save(image_data, format="PNG")
    image_data.seek(0)
    uploaded = client.post(
        "/profile/photo",
        data={"profile_photo": (image_data, "portrait.png")},
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 302
    image_response = client.get(f"/profile/photo/{user_id}")
    assert image_response.status_code == 200
    assert image_response.content_type == "image/jpeg"
    assert image_response.headers["Cache-Control"] == "private, no-store"

    with app.app_context():
        profile = db.session.get(StudentProfile, profile_id)
        assert profile.profile_photo.endswith(".jpg")
        assert "static" not in profile.profile_photo


def test_profile_page_persists_role_details_and_privacy_settings():
    app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "WTF_CSRF_ENABLED": False,
    })
    with app.app_context():
        db.create_all()
        user = User(email="profile.student@example.com", full_name="Profile Student", role="student")
        user.password = "Student123!"
        user.is_active = True
        db.session.add(user)
        db.session.flush()
        profile = StudentProfile(user_id=user.id, department_id=None, student_number="REG-9001", year="Year 2", approved=True, status="approved")
        db.session.add(profile)
        db.session.commit()

    client = app.test_client()
    login = client.post("/login", data={"email": "profile.student@example.com", "password": "Student123!"})
    assert login.status_code == 302

    response = client.get("/profile")
    assert response.status_code == 200

    update = client.post(
        "/profile",
        data={
            "full_name": "Profile Student Updated",
            "phone": "+254700000001",
            "location": "Nairobi",
            "bio": "Computer science student focused on secure systems.",
            "privacy_level": "limited",
            "student_number": "REG-9001",
            "year": "Year 3",
            "section": "C",
            "date_of_birth": "2004-02-29",
            "gender": "Female",
            "address": "Westlands, Nairobi",
            "guardian_name": "Jane Student",
            "guardian_contact": "+254700000002",
            "emergency_contact": "+254700000003",
        },
    )
    assert update.status_code == 302

    with app.app_context():
        refreshed = User.query.filter_by(email="profile.student@example.com").one()
        assert refreshed.full_name == "Profile Student Updated"
        assert refreshed.phone == "+254700000001"
        assert refreshed.location == "Nairobi"
        assert refreshed.privacy_level == "limited"
        assert refreshed.student_profile.student_number == "REG-9001"
        assert refreshed.student_profile.year == "Year 2"
        assert refreshed.student_profile.section == "C"
        assert refreshed.student_profile.date_of_birth.isoformat() == "2004-02-29"
        assert refreshed.student_profile.gender == "Female"
        assert refreshed.student_profile.address == "Westlands, Nairobi"
        assert refreshed.student_profile.emergency_contact == "+254700000003"
