"""잔액 SUM용 커버링 인덱스로 교체: entry_lines (group_id, member_id, side, amount)

Revision ID: b37a5d0c1e92
Revises: 1f6e6b65e79c
Create Date: 2026-11-24 04:00:00.000000

기존 인덱스 (group_id, member_id)는 멤버 외래 키가 쓰고 있다. 먼저 지우면 오류 1553이다.
새 인덱스를 먼저 만들면(앞부분이 같아 외래 키가 쓸 수 있다) 그다음 기존 인덱스를 지울 수 있다.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b37a5d0c1e92"
down_revision: str | Sequence[str] | None = "1f6e6b65e79c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        op.f("ix_entry_lines_group_id_member_id_side_amount"),
        "entry_lines",
        ["group_id", "member_id", "side", "amount"],
        unique=False,
    )
    op.drop_index(op.f("ix_entry_lines_group_id_member_id"), table_name="entry_lines")


def downgrade() -> None:
    # 거꾸로도 "만들고 나서 지운다" 순서를 지킨다.
    op.create_index(
        op.f("ix_entry_lines_group_id_member_id"),
        "entry_lines",
        ["group_id", "member_id"],
        unique=False,
    )
    op.drop_index(op.f("ix_entry_lines_group_id_member_id_side_amount"), table_name="entry_lines")
