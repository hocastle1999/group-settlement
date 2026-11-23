"""감사 로그 테이블(audit_events)과 그룹의 감사 로그 기준점(audit_seq, audit_head)

Revision ID: c3f995ee41a8
Revises: addd5392cd16
Create Date: 2026-11-24 05:58:46.997302

이미 있는 그룹은 감사 로그 없이(audit_seq 0) 시작한다. 이 버전 이후의 변경부터 기록된다.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision: str = "c3f995ee41a8"
down_revision: str | Sequence[str] | None = "addd5392cd16"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("group_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("seq", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.BigInteger(), nullable=True),
        sa.Column("actor_member_id", sa.BigInteger(), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("prev_hash", sa.BINARY(length=32), nullable=False),
        sa.Column("hash", sa.BINARY(length=32), nullable=False),
        sa.CheckConstraint(
            "action IN ('GROUP_CREATED', 'MEMBER_ADDED', 'EXPENSE_RECORDED',"
            " 'TRANSFER_RECORDED', 'ENTRY_REVERSED', 'SETTLEMENT_CONFIRMED',"
            " 'TRANSFER_COMPLETED')",
            name=op.f("ck_audit_events_action"),
        ),
        sa.CheckConstraint("seq > 0", name=op.f("ck_audit_events_seq_positive")),
        sa.ForeignKeyConstraint(
            ["group_id", "actor_member_id"],
            ["members.group_id", "members.id"],
            name=op.f("fk_audit_events_group_id_actor_member_id_members"),
        ),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["settlement_groups.id"],
            name=op.f("fk_audit_events_group_id_settlement_groups"),
        ),
        sa.PrimaryKeyConstraint("group_id", "seq", name=op.f("pk_audit_events")),
    )
    op.create_index(
        op.f("ix_audit_events_group_id_actor_member_id"),
        "audit_events",
        ["group_id", "actor_member_id"],
        unique=False,
    )
    op.add_column(
        "settlement_groups",
        sa.Column("audit_seq", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.add_column("settlement_groups", sa.Column("audit_head", sa.BINARY(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("settlement_groups", "audit_head")
    op.drop_column("settlement_groups", "audit_seq")
    # autogenerate는 drop_index를 먼저 만든다. 외래 키가 쓰는 인덱스라 오류 1553 -> 테이블째 지운다.
    op.drop_table("audit_events")
