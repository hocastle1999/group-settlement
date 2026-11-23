"""다중 통화: 지원 통화 확대(통화 CHECK 교체)와 외화 지출 증빙 열(원래 통화, 원래 금액, 환율)

Revision ID: 572ffdac2a1e
Revises: b37a5d0c1e92
Create Date: 2026-11-24 05:00:00.000000

autogenerate는 열 3개만 찾았다. 새 CHECK 5개와 settlement_groups.currency CHECK의 값 변경은
감지하지 못한다(CHECK 내용을 비교하지 않는다). 모두 직접 쓴다.

통화 목록은 app.ledger.CURRENCIES를 import하지 않고 이 파일에 적는다. 마이그레이션은
"그 시점의 스키마"를 기록하는 것이라, 앱 상수가 나중에 바뀌어도 이 파일의 결과는 같아야 한다.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "572ffdac2a1e"
down_revision: str | Sequence[str] | None = "b37a5d0c1e92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_CURRENCIES = "'EUR', 'JPY', 'KRW', 'USD'"
NEW_CURRENCIES = "'CNY', 'EUR', 'GBP', 'JPY', 'KRW', 'KWD', 'THB', 'USD', 'VND'"


def upgrade() -> None:
    # CHECK는 고칠 수 없어서 지우고 다시 만든다. 그 사이(DDL 두 번)에는 검사가 없다.
    op.drop_constraint(op.f("ck_settlement_groups_currency"), "settlement_groups", type_="check")
    op.create_check_constraint(
        op.f("ck_settlement_groups_currency"),
        "settlement_groups",
        f"currency IN ({NEW_CURRENCIES})",
    )

    op.add_column(
        "journal_entries", sa.Column("original_currency", sa.String(length=3), nullable=True)
    )
    op.add_column(
        "journal_entries",
        sa.Column("original_amount", sa.Numeric(precision=19, scale=4), nullable=True),
    )
    op.add_column(
        "journal_entries", sa.Column("fx_rate", sa.Numeric(precision=20, scale=10), nullable=True)
    )
    for name, condition in [
        ("original_currency", f"original_currency IN ({NEW_CURRENCIES})"),
        ("original_amount_positive", "original_amount > 0"),
        ("fx_rate_positive", "fx_rate > 0"),
        (
            "fx_all_or_none",
            "(original_currency IS NULL) = (original_amount IS NULL)"
            " AND (original_amount IS NULL) = (fx_rate IS NULL)",
        ),
        ("fx_only_expense", "original_currency IS NULL OR kind = 'EXPENSE'"),
    ]:
        op.create_check_constraint(op.f(f"ck_journal_entries_{name}"), "journal_entries", condition)


def downgrade() -> None:
    # 외화 분개가 있으면 되돌리면서 증빙(원래 금액, 환율)을 잃는다. 원장 금액(줄)은 남는다.
    for name in [
        "fx_only_expense",
        "fx_all_or_none",
        "fx_rate_positive",
        "original_amount_positive",
        "original_currency",
    ]:
        op.drop_constraint(op.f(f"ck_journal_entries_{name}"), "journal_entries", type_="check")
    op.drop_column("journal_entries", "fx_rate")
    op.drop_column("journal_entries", "original_amount")
    op.drop_column("journal_entries", "original_currency")

    # 새 통화로 만든 그룹이 있으면 옛 CHECK를 만들 수 없다(기존 행도 검사한다). 먼저 확인한다.
    op.drop_constraint(op.f("ck_settlement_groups_currency"), "settlement_groups", type_="check")
    op.create_check_constraint(
        op.f("ck_settlement_groups_currency"),
        "settlement_groups",
        f"currency IN ({OLD_CURRENCIES})",
    )
