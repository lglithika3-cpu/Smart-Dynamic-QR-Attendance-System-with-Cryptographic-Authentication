"""add student security and profile fields

Revision ID: e7f3a921b6d4
Revises: c4d7e90a1b23, d4e6b8a0c2f1
Create Date: 2026-10-23
"""
from alembic import op
import sqlalchemy as sa


revision = "e7f3a921b6d4"
down_revision = ("c4d7e90a1b23", "d4e6b8a0c2f1")
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("user_sessions"):
        op.create_table(
            "user_sessions",
            sa.Column("session_id", sa.String(length=64), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("auth_version", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("session_id"),
        )
        op.create_index("ix_user_sessions_user_id", "user_sessions", ["user_id"], unique=False)
        op.create_index("ix_user_sessions_expires_at", "user_sessions", ["expires_at"], unique=False)

    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("announcements"):
        op.create_table(
            "announcements",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("title", sa.String(length=120), nullable=False),
            sa.Column("message", sa.String(length=2000), nullable=False),
            sa.Column("starts_at", sa.DateTime(), nullable=False),
            sa.Column("ends_at", sa.DateTime(), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_by_id", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["created_by_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_announcements_starts_at", "announcements", ["starts_at"], unique=False)
        op.create_index("ix_announcements_ends_at", "announcements", ["ends_at"], unique=False)

    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("users"):
        columns = {column["name"] for column in inspector.get_columns("users")}
        with op.batch_alter_table("users") as batch_op:
            if "totp_secret" not in columns:
                batch_op.add_column(sa.Column("totp_secret", sa.Text(), nullable=True))
            if "totp_enabled" not in columns:
                batch_op.add_column(
                    sa.Column("totp_enabled", sa.Boolean(), server_default=sa.false(), nullable=False)
                )
            if "totp_last_counter" not in columns:
                batch_op.add_column(sa.Column("totp_last_counter", sa.Integer(), nullable=True))

    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("student_profiles"):
        columns = {column["name"] for column in inspector.get_columns("student_profiles")}
        with op.batch_alter_table("student_profiles") as batch_op:
            for name, field_type in (
                ("section", sa.String(length=20)),
                ("date_of_birth", sa.Date()),
                ("gender", sa.String(length=40)),
                ("profile_photo", sa.String(length=80)),
            ):
                if name not in columns:
                    batch_op.add_column(sa.Column(name, field_type, nullable=True))


def downgrade():
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("announcements"):
        op.drop_table("announcements")

    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("user_sessions"):
        op.drop_table("user_sessions")

    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("student_profiles"):
        columns = {column["name"] for column in inspector.get_columns("student_profiles")}
        with op.batch_alter_table("student_profiles") as batch_op:
            for name in ("profile_photo", "gender", "date_of_birth", "section"):
                if name in columns:
                    batch_op.drop_column(name)

    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("users"):
        columns = {column["name"] for column in inspector.get_columns("users")}
        with op.batch_alter_table("users") as batch_op:
            for name in ("totp_last_counter", "totp_enabled", "totp_secret"):
                if name in columns:
                    batch_op.drop_column(name)
