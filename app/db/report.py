"""정산 리포트 SQL (6단계).

세 쿼리를 **한 트랜잭션(같은 스냅숏)** 에서 읽어, 서로 다른 시점의 숫자가 섞이지 않게 한다.

1. 멤버별 요약: 낸 돈, 부담한 몫, 보낸 송금, 받은 송금, 잔액
   - 조건부 집계 SUM(CASE ...)로 한 번에 훑는다 (종류마다 쿼리를 따로 하지 않는다).
   - 취소(역분개)의 줄은 "원래 분개의 종류"로 묶고 부호를 뒤집는다. 지출을 취소하면 그 지출의
     "낸 돈"과 "몫"이 빠진다. 역분개를 REVERSAL로 따로 세면 낸 돈과 몫이 부풀어 보인다.
2. 날짜별 지출과 누적합: 윈도 함수 SUM(...) OVER (ORDER BY day). 취소된 지출은 빠진다.
   날짜는 요청한 시간대(+09:00 등)로 나눈다. DB에는 UTC로 저장돼 있다.
3. 통화별 지출: 외화 지출은 원래 통화와 원래 금액으로, 기준 통화 지출은 그대로.

ORM이 아니라 SQL 문자열(text) + 바인드 파라미터를 쓴다. 집계 쿼리는 SQL 그대로가 읽기 쉽고,
값은 모두 :파라미터로 넘겨 SQL 인젝션을 막는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import Engine, text

from app.db.store import Group
from app.ledger import Money

# 역분개 줄은 원래 종류로 묶고, 방향을 되돌리고, 부호를 뒤집는다.
_EFFECTIVE_LINES = """
WITH effective AS (
    SELECT
        l.member_id,
        COALESCE(original.kind, e.kind) AS kind,
        CASE WHEN e.kind = 'REVERSAL'
             THEN CASE l.side WHEN 'DEBIT' THEN 'CREDIT' ELSE 'DEBIT' END
             ELSE l.side END AS side,
        CASE WHEN e.kind = 'REVERSAL' THEN -l.amount ELSE l.amount END AS amount
    FROM entry_lines l
    JOIN journal_entries e ON e.group_id = l.group_id AND e.id = l.entry_id
    LEFT JOIN journal_entries original
        ON original.group_id = e.group_id AND original.id = e.reverses_entry_id
    WHERE l.group_id = :group_id
)
"""

# 줄을 멤버별로 먼저 집계(per_member)한 뒤 멤버 목록에 붙인다.
# "멤버 LEFT JOIN 줄 -> GROUP BY"로 쓰면 옵티마이저가 멤버마다 그룹의 분개 전체를 다시 훑는 계획
# (loops = 멤버 수)을 고를 때가 있다. 줄 1만 개에서 같은 쿼리가 21ms와 600ms를 오갔다
# (docs/troubleshooting.md). 먼저 집계하면 줄은 한 번만 읽힌다.
MEMBER_SUMMARY = (
    _EFFECTIVE_LINES
    + """
, per_member AS (
    SELECT member_id,
        SUM(CASE WHEN kind = 'EXPENSE' AND side = 'CREDIT' THEN amount END) AS paid,
        SUM(CASE WHEN kind = 'EXPENSE' AND side = 'DEBIT' THEN amount END) AS share,
        SUM(CASE WHEN kind = 'TRANSFER' AND side = 'CREDIT' THEN amount END) AS sent,
        SUM(CASE WHEN kind = 'TRANSFER' AND side = 'DEBIT' THEN amount END) AS received
    FROM effective
    GROUP BY member_id
)
SELECT m.id AS member_id, m.name,
       COALESCE(p.paid, 0) AS paid, COALESCE(p.share, 0) AS share,
       COALESCE(p.sent, 0) AS sent, COALESCE(p.received, 0) AS received
FROM members m
LEFT JOIN per_member p ON p.member_id = m.id
WHERE m.group_id = :group_id
ORDER BY m.id
"""
)

# 취소되지 않은 지출 = 역분개가 가리키지 않는 EXPENSE. 지출의 대변 줄은 결제자 한 줄(= 총액)이다.
_LIVE_EXPENSES = """
FROM journal_entries e
JOIN entry_lines l ON l.group_id = e.group_id AND l.entry_id = e.id AND l.side = 'CREDIT'
LEFT JOIN journal_entries r ON r.reverses_entry_id = e.id
WHERE e.group_id = :group_id AND e.kind = 'EXPENSE' AND r.id IS NULL
"""

DAILY_SPENDING = (
    """
WITH daily AS (
    SELECT DATE(CONVERT_TZ(e.created_at, '+00:00', :tz_offset)) AS day,
           COUNT(*) AS expenses,
           SUM(l.amount) AS total
"""
    + _LIVE_EXPENSES
    + """
    GROUP BY day
)
SELECT day, expenses, total, SUM(total) OVER (ORDER BY day) AS cumulative
FROM daily
ORDER BY day
"""
)

BY_CURRENCY = (
    """
SELECT COALESCE(e.original_currency, :base_currency) AS currency,
       COUNT(*) AS expenses,
       SUM(COALESCE(e.original_amount, l.amount)) AS original_total,
       SUM(l.amount) AS converted_total
"""
    + _LIVE_EXPENSES
    + """
GROUP BY currency
ORDER BY converted_total DESC, currency
"""
)


@dataclass(frozen=True, slots=True)
class MemberSummary:
    member_id: int
    name: str
    paid: Money  # 먼저 계산한 지출 합계
    share: Money  # 자기 몫으로 부담한 합계
    sent: Money  # 보낸 송금
    received: Money  # 받은 송금

    @property
    def balance(self) -> Money:
        """받을 돈(+) / 낼 돈(-). 원장 잔액(대변 - 차변)과 같아야 한다."""
        return self.paid - self.share + self.sent - self.received


@dataclass(frozen=True, slots=True)
class DailySpending:
    day: date
    expenses: int
    total: Money
    cumulative: Money


@dataclass(frozen=True, slots=True)
class CurrencySpending:
    currency: str
    expenses: int
    original_total: Money  # 원래 통화 금액의 합
    converted_total: Money  # 기준 통화로 기록된 금액의 합


@dataclass(frozen=True, slots=True)
class Report:
    version: int
    members: list[MemberSummary]
    daily: list[DailySpending]
    by_currency: list[CurrencySpending]


def build_report(engine: Engine, group: Group, tz_offset: str = "+00:00") -> Report:
    """`tz_offset`은 "+09:00" 같은 UTC 차이. 이름("Asia/Seoul")은 MySQL 시간대 테이블이 있어야 해서
    받지 않는다(Windows의 MySQL에는 기본으로 없다)."""
    cur = group.currency
    params = {"group_id": group.id, "tz_offset": tz_offset, "base_currency": cur.code}
    with engine.connect() as conn:
        version = conn.execute(
            text("SELECT last_seq FROM settlement_groups WHERE id = :group_id"), params
        ).scalar_one()
        members = [
            MemberSummary(
                r.member_id,
                r.name,
                Money.of(r.paid, cur),
                Money.of(r.share, cur),
                Money.of(r.sent, cur),
                Money.of(r.received, cur),
            )
            for r in conn.execute(text(MEMBER_SUMMARY), params)
        ]
        daily = [
            DailySpending(r.day, r.expenses, Money.of(r.total, cur), Money.of(r.cumulative, cur))
            for r in conn.execute(text(DAILY_SPENDING), params)
        ]
        by_currency = [
            CurrencySpending(
                r.currency,
                r.expenses,
                Money.of(r.original_total, r.currency),
                Money.of(r.converted_total, cur),
            )
            for r in conn.execute(text(BY_CURRENCY), params)
        ]
    return Report(version, members, daily, by_currency)
