import os
from pathlib import Path
from datetime import timedelta
from dotenv import load_dotenv
from sqlalchemy.engine import make_url

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _normalize_database_url(database_url, ssl_ca=None):
    if not database_url:
        return f"sqlite:///{(BASE_DIR / 'instance' / 'attendance.db').resolve()}"
    if database_url.startswith("mysql://"):
        database_url = "mysql+pymysql://" + database_url[len("mysql://"):]
    url = make_url(database_url)
    if url.drivername in {
        "postgres",
        "postgresql",
        "postgresql+psycopg",
        "postgresql+psycopg2",
    }:
        url = url.set(drivername="postgresql+psycopg")
        query = dict(url.query)
        if ssl_ca:
            query.update({"sslmode": "verify-full", "sslrootcert": ssl_ca})
        else:
            query.setdefault("sslmode", "require")
        database_url = url.set(query=query).render_as_string(hide_password=False)
    if database_url.startswith("sqlite:///") and not database_url.startswith("sqlite:////"):
        relative_path = database_url.replace("sqlite:///", "", 1)
        if not os.path.isabs(relative_path):
            database_url = f"sqlite:///{(BASE_DIR / relative_path).resolve()}"
    return database_url


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY")

    database_ssl_ca = os.getenv("DATABASE_SSL_CA")
    SQLALCHEMY_DATABASE_URI = _normalize_database_url(
        os.getenv("DATABASE_URL"), ssl_ca=database_ssl_ca
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}
    if database_ssl_ca and SQLALCHEMY_DATABASE_URI.startswith("mysql+pymysql://"):
        SQLALCHEMY_ENGINE_OPTIONS["connect_args"] = {"ssl": {"ca": database_ssl_ca}}
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = os.getenv("SESSION_COOKIE_SAMESITE", "Lax")
    SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "False").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    WTF_CSRF_ENABLED = os.getenv("WTF_CSRF_ENABLED", "True").lower() not in {"0", "false", "no"}
    QR_SIGNING_SECRET = os.getenv("QR_SIGNING_SECRET")
    QR_TOKEN_TTL_SECONDS = int(os.getenv("QR_TOKEN_TTL_SECONDS", "30"))
    PERMANENT_SESSION_LIFETIME = timedelta(minutes=int(os.getenv("SESSION_LIFETIME_MINUTES", "30")))
    RATE_LIMIT_DEFAULT = os.getenv("RATE_LIMIT_DEFAULT", "200 per hour")
    RATE_LIMIT_AUTH = os.getenv("RATE_LIMIT_AUTH", "20/minute")
    LOGIN_FAILURE_LIMIT = int(os.getenv("LOGIN_FAILURE_LIMIT", "5"))
    LOGIN_LOCKOUT_MINUTES = int(os.getenv("LOGIN_LOCKOUT_MINUTES", "15"))
    SECURITY_ALERT_THRESHOLD = int(os.getenv("SECURITY_ALERT_THRESHOLD", "10"))
    ATTENDANCE_ELIGIBILITY_THRESHOLD = int(os.getenv("ATTENDANCE_ELIGIBILITY_THRESHOLD", "80"))
    PASSWORD_RESET_TTL_MINUTES = int(os.getenv("PASSWORD_RESET_TTL_MINUTES", "30"))
    MAX_CONTENT_LENGTH = 1 * 1024 * 1024
    DEBUG = os.getenv("FLASK_DEBUG", "False").lower() in {"1", "true", "yes", "on"}

    MAIL_HOST = os.getenv("MAIL_HOST")
    MAIL_PORT = int(os.getenv("MAIL_PORT", "587"))
    MAIL_USERNAME = os.getenv("MAIL_USERNAME")
    MAIL_PASSWORD = os.getenv("MAIL_PASSWORD")
    MAIL_USE_TLS = os.getenv("MAIL_USE_TLS", "True").lower() in {"1", "true", "yes", "on"}
    MAIL_FROM = os.getenv("MAIL_FROM")
    PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")
    RATELIMIT_STORAGE_URI = os.getenv("RATELIMIT_STORAGE_URI", "memory://")
    REQUIRE_DATABASE_URL = os.getenv("REQUIRE_DATABASE_URL", "False").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    TRUST_PROXY = os.getenv("TRUST_PROXY", "False").lower() in {"1", "true", "yes", "on"}
