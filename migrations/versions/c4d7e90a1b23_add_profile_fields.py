"""add user and student profile fields

Revision ID: c4d7e90a1b23
Revises: 8222663c8f90
Create Date: 2026-10-17

"""
from alembic import op
import sqlalchemy as sa


revision = "c4d7e90a1b23"
down_revision = "8222663c8f90"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("users"):
        columns = {column["name"] for column in inspector.get_columns("users")}
        with op.batch_alter_table("users") as batch_op:
            if "phone" not in columns:
                batch_op.add_column(sa.Column("phone", sa.String(length=40), nullable=True))
            if "location" not in columns:
                batch_op.add_column(sa.Column("location", sa.String(length=120), nullable=True))
            if "bio" not in columns:
                batch_op.add_column(sa.Column("bio", sa.Text(), nullable=True))
            if "privacy_level" not in columns:
                batch_op.add_column(
                    sa.Column("privacy_level", sa.String(length=20), server_default="standard", nullable=False)
                )

    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("student_profiles"):
        columns = {column["name"] for column in inspector.get_columns("student_profiles")}
        with op.batch_alter_table("student_profiles") as batch_op:
            for name, field_type in (
                ("address", sa.String(length=255)),
                ("guardian_name", sa.String(length=255)),
                ("guardian_contact", sa.String(length=40)),
                ("emergency_contact", sa.String(length=40)),
            ):
                if name not in columns:
                    batch_op.add_column(sa.Column(name, field_type, nullable=True))


def downgrade():
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("student_profiles"):
        columns = {column["name"] for column in inspector.get_columns("student_profiles")}
        with op.batch_alter_table("student_profiles") as batch_op:
            for name in ("emergency_contact", "guardian_contact", "guardian_name", "address"):
                if name in columns:
                    batch_op.drop_column(name)

    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("users"):
        columns = {column["name"] for column in inspector.get_columns("users")}
        with op.batch_alter_table("users") as batch_op:
            for name in ("privacy_level", "bio", "location", "phone"):
                if name in columns:
                    batch_op.drop_column(name)