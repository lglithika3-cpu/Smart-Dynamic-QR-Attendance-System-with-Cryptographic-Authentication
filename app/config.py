import os
import ssl
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.engine import URL, make_url

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


def _database_url_from_environment():
    host = os.getenv("DB_HOST", "").strip()
    if not host:
        return os.getenv("DATABASE_URL")

    required = {
        "DB_NAME": os.getenv("DB_NAME", "").strip(),
        "DB_USER": os.getenv("DB_USER", "").strip(),
        "DB_PASSWORD": os.getenv("DB_PASSWORD"),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(
            "Set the following MySQL environment variables: " + ", ".join(missing)
        )

    try:
        port = int(os.getenv("DB_PORT", "3306"))
    except ValueError as exc:
        raise RuntimeError("DB_PORT must be a valid port number.") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError("DB_PORT must be between 1 and 65535.")

    return URL.create(
        "mysql+pymysql",
        username=required["DB_USER"],
        password=required["DB_PASSWORD"],
        host=host,
        port=port,
        database=required["DB_NAME"],
        query={"charset": "utf8mb4"},
    ).render_as_string(hide_password=False)


def _sqlalchemy_engine_options(database_url, ssl_ca=None, ssl_mode="REQUIRED"):
    options = {"pool_pre_ping": True}
    drivername = make_url(database_url).drivername
    if drivername.startswith("mysql"):
        if ssl_mode.strip().upper() != "REQUIRED":
            raise RuntimeError("MySQL connections require DB_SSL_MODE=REQUIRED.")
        if not ssl_ca:
            raise RuntimeError(
                "Set DATABASE_SSL_CA to the Aiven CA certificate path for MySQL."
            )

        ca_path = Path(ssl_ca).expanduser()
        if not ca_path.is_file():
            raise RuntimeError(
                "DATABASE_SSL_CA must point to an existing Aiven CA certificate file."
            )
        try:
            ssl_context = ssl.create_default_context(cafile=str(ca_path))
        except (OSError, ssl.SSLError) as exc:
            raise RuntimeError("The Aiven CA certificate could not be loaded.") from exc
        options["connect_args"] = {"ssl": ssl_context}
    return options


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY")

    DATABASE_SSL_CA = os.getenv("DATABASE_SSL_CA")
    DB_SSL_MODE = os.getenv("DB_SSL_MODE", "REQUIRED")
    SQLALCHEMY_DATABASE_URI = _normalize_database_url(
        _database_url_from_environment(), ssl_ca=DATABASE_SSL_CA
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = _sqlalchemy_engine_options(
        SQLALCHEMY_DATABASE_URI,
        ssl_ca=DATABASE_SSL_CA,
        ssl_mode=DB_SSL_MODE,
    )
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
