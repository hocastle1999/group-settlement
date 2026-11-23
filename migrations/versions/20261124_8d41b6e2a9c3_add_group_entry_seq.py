"""그룹별 분개 순번(seq)과 원장 버전(last_seq) 추가, 기존 분개에 순번 채우기

Revision ID: 8d41b6e2a9c3
Revises: 5c2e8a1f4b70
Create Date: 2026-11-24 03:00:00.000000

이미 있는 분개에도 순번이 필요하므로 단계를 나눈다.
1. seq를 NULL 허용으로 추가한다 (NOT NULL로 바로 추가하면 기존 행에 넣을 값이 없다).
2. 그룹마다 id 순서로 1, 2, 3, ... 을 채운다 (ROW_NUMBER 윈도 함수).
3. NOT NULL, 유니크, CHECK를 건다.
4. 그룹의 last_seq를 그 그룹 분개 수로 맞춘다.

MySQL DDL은 트랜잭션으로 되돌릴 수 없다. 중간에 실패하면 alembic current와 실제 열을 먼저 확인한다.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8d41b6e2a9c3"
down_revision: str | Sequence[str] | None = "5c2e8a1f4b70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "settlement_groups",
        sa.Column("last_seq", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_settlement_groups_last_seq_not_negative"), "settlement_groups", "last_seq >= 0"
    )

    op.add_column("journal_entries", sa.Column("seq", sa.BigInteger(), nullable=True))
    # 같은 테이블을 읽어서 UPDATE한다. 윈도 함수가 든 파생 테이블은 먼저 통째로 만들어
    # 두므로(materialize) "UPDATE 대상 테이블을 FROM에서 읽을 수 없다"(오류 1093)에 걸리지 않는다.
    op.execute(
        """
        UPDATE journal_entries AS j
        JOIN (
            SELECT id, ROW_NUMBER() OVER (PARTITION BY group_id ORDER BY id) AS rn
            FROM journal_entries
        ) AS numbered ON numbered.id = j.id
        SET j.seq = numbered.rn
        """
    )
    op.alter_column("journal_entries", "seq", existing_type=sa.BigInteger(), nullable=False)
    op.create_unique_constraint(
        op.f("uq_journal_entries_group_id_seq"), "journal_entries", ["group_id", "seq"]
    )
    op.create_check_constraint(
        op.f("ck_journal_entries_seq_positive"), "journal_entries", "seq > 0"
    )

    op.execute(
        """
        UPDATE settlement_groups AS g
        JOIN (
            SELECT group_id, MAX(seq) AS max_seq FROM journal_entries GROUP BY group_id
        ) AS latest ON latest.group_id = g.id
        SET g.last_seq = latest.max_seq
        """
    )


def downgrade() -> None:
    # CHECK가 참조하는 열은 바로 지울 수 없으므로 제약부터 지운다.
    op.drop_constraint(op.f("ck_journal_entries_seq_positive"), "journal_entries", type_="check")
    op.drop_constraint(op.f("uq_journal_entries_group_id_seq"), "journal_entries", type_="unique")
    op.drop_column("journal_entries", "seq")
    op.drop_constraint(
        op.f("ck_settlement_groups_last_seq_not_negative"), "settlement_groups", type_="check"
    )
    op.drop_column("settlement_groups", "last_seq")
