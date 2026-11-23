"""그룹, 멤버, 분개, 분개 줄, 멱등성 키 테이블 생성

Revision ID: 5c2e8a1f4b70
Revises:
Create Date: 2026-11-24 01:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5c2e8a1f4b70"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.text("CURRENT_TIMESTAMP")


def upgrade() -> None:
    # CHECK 제약은 autogenerate가 내용을 비교하지 못하므로 직접 쓴다.
    # 이름은 op.f()로 감싼다: 이름 규칙(ck_%(table_name)s_...)이 접두어를 한 번 더 붙이지 않게.
    op.create_table(
        "settlement_groups",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("invite_token_hash", sa.BINARY(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=NOW, nullable=False),
        sa.CheckConstraint(
            "currency IN ('EUR', 'JPY', 'KRW', 'USD')", name=op.f("ck_settlement_groups_currency")
        ),
        sa.CheckConstraint("name <> ''", name=op.f("ck_settlement_groups_name_not_empty")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_settlement_groups")),
        sa.UniqueConstraint(
            "invite_token_hash", name=op.f("uq_settlement_groups_invite_token_hash")
        ),
    )
    op.create_table(
        "members",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=50), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=NOW, nullable=False),
        sa.CheckConstraint("name <> ''", name=op.f("ck_members_name_not_empty")),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["settlement_groups.id"],
            name=op.f("fk_members_group_id_settlement_groups"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_members")),
        sa.UniqueConstraint("group_id", "id", name=op.f("uq_members_group_id_id")),
        sa.UniqueConstraint("group_id", "name", name=op.f("uq_members_group_id_name")),
    )
    op.create_table(
        "journal_entries",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("memo", sa.String(length=200), server_default="", nullable=False),
        sa.Column("reverses_entry_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=NOW, nullable=False),
        sa.CheckConstraint(
            "kind IN ('EXPENSE', 'TRANSFER', 'REVERSAL')", name=op.f("ck_journal_entries_kind")
        ),
        sa.CheckConstraint(
            "(kind = 'REVERSAL') = (reverses_entry_id IS NOT NULL)",
            name=op.f("ck_journal_entries_reversal_has_target"),
        ),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["settlement_groups.id"],
            name=op.f("fk_journal_entries_group_id_settlement_groups"),
        ),
        sa.ForeignKeyConstraint(
            ["group_id", "reverses_entry_id"],
            ["journal_entries.group_id", "journal_entries.id"],
            name=op.f("fk_journal_entries_group_id_reverses_entry_id_journal_entries"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_journal_entries")),
        sa.UniqueConstraint("group_id", "id", name=op.f("uq_journal_entries_group_id_id")),
        sa.UniqueConstraint("reverses_entry_id", name=op.f("uq_journal_entries_reverses_entry_id")),
    )
    op.create_table(
        "entry_lines",
        sa.Column("group_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("entry_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("line_no", sa.SmallInteger(), autoincrement=False, nullable=False),
        sa.Column("member_id", sa.BigInteger(), nullable=False),
        sa.Column("side", sa.String(length=6), nullable=False),
        sa.Column("amount", sa.Numeric(precision=19, scale=4), nullable=False),
        sa.CheckConstraint("amount > 0", name=op.f("ck_entry_lines_amount_positive")),
        sa.CheckConstraint("side IN ('DEBIT', 'CREDIT')", name=op.f("ck_entry_lines_side")),
        sa.ForeignKeyConstraint(
            ["group_id", "entry_id"],
            ["journal_entries.group_id", "journal_entries.id"],
            name=op.f("fk_entry_lines_group_id_entry_id_journal_entries"),
        ),
        sa.ForeignKeyConstraint(
            ["group_id", "member_id"],
            ["members.group_id", "members.id"],
            name=op.f("fk_entry_lines_group_id_member_id_members"),
        ),
        sa.PrimaryKeyConstraint("group_id", "entry_id", "line_no", name=op.f("pk_entry_lines")),
    )
    op.create_index(
        op.f("ix_entry_lines_group_id_member_id"),
        "entry_lines",
        ["group_id", "member_id"],
        unique=False,
    )
    op.create_table(
        "idempotency_keys",
        sa.Column("group_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("idem_key", sa.String(length=64, collation="ascii_bin"), nullable=False),
        sa.Column("request_hash", sa.BINARY(length=32), nullable=False),
        sa.Column("entry_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=NOW, nullable=False),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["settlement_groups.id"],
            name=op.f("fk_idempotency_keys_group_id_settlement_groups"),
        ),
        sa.ForeignKeyConstraint(
            ["group_id", "entry_id"],
            ["journal_entries.group_id", "journal_entries.id"],
            name=op.f("fk_idempotency_keys_group_id_entry_id_journal_entries"),
        ),
        sa.PrimaryKeyConstraint("group_id", "idem_key", name=op.f("pk_idempotency_keys")),
    )
    op.create_index(
        op.f("ix_idempotency_keys_group_id_entry_id"),
        "idempotency_keys",
        ["group_id", "entry_id"],
        unique=False,
    )


def downgrade() -> None:
    # 만든 순서의 반대로 지운다 (외래 키가 가리키는 테이블을 나중에).
    # 인덱스를 먼저 drop_index하지 않는다: 외래 키가 쓰는 인덱스라 MySQL이 거부한다(오류 1553).
    # 테이블을 지우면 그 테이블의 인덱스도 함께 지워진다 (docs/troubleshooting.md).
    op.drop_table("idempotency_keys")
    op.drop_table("entry_lines")
    op.drop_table("journal_entries")
    op.drop_table("members")
    op.drop_table("settlement_groups")
