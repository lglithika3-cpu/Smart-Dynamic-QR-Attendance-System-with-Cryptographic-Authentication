"""add faculty employee identifier

Revision ID: d4e6b8a0c2f1
Revises: c4d7e90a1b23
Create Date: 2026-10-17

"""
from alembic import op
import sqlalchemy as sa


revision = "d4e6b8a0c2f1"
down_revision = "c4d7e90a1b23"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("faculty_profiles"):
        return

    columns = {column["name"] for column in inspector.get_columns("faculty_profiles")}
    indexes = {index["name"] for index in inspector.get_indexes("faculty_profiles")}
    with op.batch_alter_table("faculty_profiles") as batch_op:
        if "employee_id" not in columns:
            batch_op.add_column(sa.Column("employee_id", sa.String(length=40), nullable=True))
        if "ix_faculty_profiles_employee_id" not in indexes:
            batch_op.create_index("ix_faculty_profiles_employee_id", ["employee_id"], unique=True)


def downgrade():
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("faculty_profiles"):
        return

    columns = {column["name"] for column in inspector.get_columns("faculty_profiles")}
    indexes = {index["name"] for index in inspector.get_indexes("faculty_profiles")}
    with op.batch_alter_table("faculty_profiles") as batch_op:
        if "ix_faculty_profiles_employee_id" in indexes:
            batch_op.drop_index("ix_faculty_profiles_employee_id")
        if "employee_id" in columns:
            batch_op.drop_column("employee_id")