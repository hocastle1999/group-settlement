"""확정 정산(settlements)과 정산 송금(settlement_transfers) 테이블 생성

Revision ID: 1f6e6b65e79c
Revises: 8d41b6e2a9c3
Create Date: 2026-11-24 01:23:22.488003

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1f6e6b65e79c"
down_revision: str | Sequence[str] | None = "8d41b6e2a9c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "settlements",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("strategy", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint(
            "strategy IN ('greedy', 'optimal', 'auto')", name=op.f("ck_settlements_strategy")
        ),
        sa.CheckConstraint("version >= 0", name=op.f("ck_settlements_version_not_negative")),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["settlement_groups.id"],
            name=op.f("fk_settlements_group_id_settlement_groups"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_settlements")),
        sa.UniqueConstraint("group_id", "id", name=op.f("uq_settlements_group_id_id")),
        sa.UniqueConstraint("group_id", "version", name=op.f("uq_settlements_group_id_version")),
    )
    op.create_table(
        "settlement_transfers",
        sa.Column("group_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("settlement_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("line_no", sa.SmallInteger(), autoincrement=False, nullable=False),
        sa.Column("sender_member_id", sa.BigInteger(), nullable=False),
        sa.Column("receiver_member_id", sa.BigInteger(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=19, scale=4), nullable=False),
        sa.CheckConstraint("amount > 0", name=op.f("ck_settlement_transfers_amount_positive")),
        sa.CheckConstraint(
            "sender_member_id <> receiver_member_id",
            name=op.f("ck_settlement_transfers_different_members"),
        ),
        sa.ForeignKeyConstraint(
            ["group_id", "receiver_member_id"],
            ["members.group_id", "members.id"],
            name=op.f("fk_settlement_transfers_group_id_receiver_member_id_members"),
        ),
        sa.ForeignKeyConstraint(
            ["group_id", "sender_member_id"],
            ["members.group_id", "members.id"],
            name=op.f("fk_settlement_transfers_group_id_sender_member_id_members"),
        ),
        sa.ForeignKeyConstraint(
            ["group_id", "settlement_id"],
            ["settlements.group_id", "settlements.id"],
            name=op.f("fk_settlement_transfers_group_id_settlement_id_settlements"),
        ),
        sa.PrimaryKeyConstraint(
            "group_id", "settlement_id", "line_no", name=op.f("pk_settlement_transfers")
        ),
    )
    op.create_index(
        op.f("ix_settlement_transfers_group_id_receiver_member_id"),
        "settlement_transfers",
        ["group_id", "receiver_member_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_settlement_transfers_group_id_sender_member_id"),
        "settlement_transfers",
        ["group_id", "sender_member_id"],
        unique=False,
    )


def downgrade() -> None:
    # autogenerate는 drop_index 다음 drop_table을 만든다. 이 인덱스들은 외래 키가 쓰고 있어서
    # MySQL이 거부한다(오류 1553, docs/troubleshooting.md). 테이블을 지우면 인덱스도 함께 지워진다.
    op.drop_table("settlement_transfers")
    op.drop_table("settlements")
