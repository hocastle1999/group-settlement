"""송금 완료: 분개가 확정 정산의 송금 한 줄을 가리키는 열 (settlement_id, settlement_line_no)

Revision ID: addd5392cd16
Revises: 572ffdac2a1e
Create Date: 2026-11-24 05:55:16.780238

autogenerate는 열, 인덱스, 외래 키는 찾았지만 CHECK 2개는 놓쳤다(직접 추가).
인덱스를 외래 키보다 먼저 만든다. 없으면 MySQL이 외래 키용 인덱스를 다른 이름으로 만들어
tables.py와 어긋난다. downgrade는 반대로 외래 키를 먼저 지운 뒤 인덱스를 지운다(오류 1553).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "addd5392cd16"
down_revision: str | Sequence[str] | None = "572ffdac2a1e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("journal_entries", sa.Column("settlement_id", sa.BigInteger(), nullable=True))
    op.add_column(
        "journal_entries", sa.Column("settlement_line_no", sa.SmallInteger(), nullable=True)
    )
    op.create_index(
        op.f("ix_journal_entries_group_id_settlement_id_settlement_line_no"),
        "journal_entries",
        ["group_id", "settlement_id", "settlement_line_no"],
        unique=False,
    )
    op.create_foreign_key(
        op.f("fk_journal_entries_group_id_settlement_id_settlement_line_no_settlement_transfers"),
        "journal_entries",
        "settlement_transfers",
        ["group_id", "settlement_id", "settlement_line_no"],
        ["group_id", "settlement_id", "line_no"],
    )
    op.create_check_constraint(
        op.f("ck_journal_entries_settlement_link_both"),
        "journal_entries",
        "(settlement_id IS NULL) = (settlement_line_no IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_journal_entries_settlement_link_transfer"),
        "journal_entries",
        "settlement_id IS NULL OR kind = 'TRANSFER'",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_journal_entries_settlement_link_transfer"), "journal_entries", type_="check"
    )
    op.drop_constraint(
        op.f("ck_journal_entries_settlement_link_both"), "journal_entries", type_="check"
    )
    op.drop_constraint(
        op.f("fk_journal_entries_group_id_settlement_id_settlement_line_no_settlement_transfers"),
        "journal_entries",
        type_="foreignkey",
    )
    op.drop_index(
        op.f("ix_journal_entries_group_id_settlement_id_settlement_line_no"),
        table_name="journal_entries",
    )
    op.drop_column("journal_entries", "settlement_line_no")
    op.drop_column("journal_entries", "settlement_id")
