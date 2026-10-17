"""Phase 4: password recovery via one-use emailed passcodes

Revision ID: 0003_password_resets
Revises: 0002_auth_hardening
Create Date: 2026-10-17
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_password_resets"
down_revision = "0002_auth_hardening"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "password_resets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("code_hash", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_password_resets_user_time", "password_resets", ["user_id", "created_at"])
    op.create_index("ix_password_resets_code_hash", "password_resets", ["code_hash"])


def downgrade() -> None:
    op.drop_table("password_resets")
