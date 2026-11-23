"""이중기입(복식부기) 원장.

모든 거래(분개, journal entry)는 차변(debit) 줄과 대변(credit) 줄로 이루어지고,
한 분개 안에서 차변 합계와 대변 합계가 항상 같다. 이 규칙 하나 덕분에
"모든 멤버 잔액의 합은 0"이 어떤 거래 순서에서도 깨지지 않는다.

계정은 멤버마다 하나다. 부호 규칙:
- 대변(CREDIT): 그 멤버가 모임에 돈을 댔다 (먼저 계산함, 송금함) -> 받을 돈 증가
- 차변(DEBIT): 그 멤버가 모임 돈으로 혜택을 받았다 (자기 몫, 송금을 받음) -> 받을 돈 감소
- 잔액 = 대변 합계 - 차변 합계. 양수면 돌려받을 사람, 음수면 내야 할 사람.

예: A가 30,000원을 내고 A, B, C가 똑같이 나눈 식사
    대변 A 30,000 / 차변 A 10,000, B 10,000, C 10,000
    잔액 A +20,000, B -10,000, C -10,000 (합 0)

원장은 추가만 한다(append-only). 잘못 기록한 분개는 고치거나 지우지 않고,
차변과 대변을 뒤집은 역분개(reversal)를 추가해 효과를 없앤다. 그래서 "언제 누가 무엇을 바꿨는지"가
기록 자체에 남는다. 3단계에서 DB 테이블도 같은 규칙(UPDATE/DELETE 없음)을 따른다.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from app.ledger.errors import (
    AlreadyReversedError,
    CurrencyMismatchError,
    EntryNotFoundError,
    InvalidAmountError,
    InvalidSplitError,
    UnbalancedEntryError,
    UnknownMemberError,
)
from app.ledger.money import Currency, Money, sum_money
from app.ledger.split import MemberId, SplitRule


class Side(StrEnum):
    DEBIT = "DEBIT"
    CREDIT = "CREDIT"

    def opposite(self) -> Side:
        return Side.CREDIT if self is Side.DEBIT else Side.DEBIT


class EntryKind(StrEnum):
    EXPENSE = "EXPENSE"  # 누군가 먼저 계산한 지출
    TRANSFER = "TRANSFER"  # 멤버 사이의 송금 (정산)
    REVERSAL = "REVERSAL"  # 다른 분개의 취소


@dataclass(frozen=True, slots=True)
class Line:
    """분개의 한 줄. 금액은 항상 양수이고 방향은 `side`로 나타낸다.

    부호 있는 금액 하나로 표현할 수도 있지만(+는 대변, -는 차변), 차변/대변을 분리하면
    DB에서 CHECK (amount > 0)으로 잘못된 값을 막을 수 있고 회계 용어와 그대로 대응한다.
    """

    member: MemberId
    side: Side
    amount: Money

    def __post_init__(self) -> None:
        if not self.amount.is_positive():
            raise InvalidAmountError(f"분개 금액은 양수여야 한다: {self.member} {self.amount}")


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """분개 하나. 만들 때 균형(차변 합 = 대변 합)을 검사하므로 균형이 깨진 분개는 존재할 수 없다."""

    kind: EntryKind
    lines: tuple[Line, ...]
    memo: str = ""
    reverses: int | None = None  # REVERSAL이면 취소하는 분개 번호

    def __post_init__(self) -> None:
        if len(self.lines) < 2:
            raise UnbalancedEntryError("분개는 줄이 2개 이상이어야 한다")
        currencies = {line.amount.currency for line in self.lines}
        if len(currencies) != 1:
            raise CurrencyMismatchError(
                f"한 분개에 통화가 섞였다: {sorted(c.code for c in currencies)}"
            )
        if not any(line.side is Side.DEBIT for line in self.lines) or not any(
            line.side is Side.CREDIT for line in self.lines
        ):
            raise UnbalancedEntryError("차변과 대변이 모두 있어야 한다")
        if self.total(Side.DEBIT) != self.total(Side.CREDIT):
            raise UnbalancedEntryError(
                f"차변 합계 {self.total(Side.DEBIT)} != 대변 합계 {self.total(Side.CREDIT)}"
            )
        if (self.kind is EntryKind.REVERSAL) != (self.reverses is not None):
            raise UnbalancedEntryError("역분개만, 그리고 역분개는 반드시 원래 분개 번호를 가진다")

    @property
    def currency(self) -> Currency:
        return self.lines[0].amount.currency

    def total(self, side: Side) -> Money:
        return sum_money([line.amount for line in self.lines if line.side is side], self.currency)

    def members(self) -> frozenset[MemberId]:
        return frozenset(line.member for line in self.lines)


@dataclass(frozen=True, slots=True)
class PostedEntry:
    """원장에 기록된 분개. 번호는 원장이 1부터 순서대로 매긴다."""

    entry_id: int
    entry: JournalEntry


# --- 분개를 만드는 함수 ---


def expense_entry(
    payer: MemberId, total: Money, split: SplitRule, memo: str = "", rotation: int = 0
) -> JournalEntry:
    """`payer`가 `total`을 먼저 내고 `split` 규칙으로 나눈 지출.

    부담액이 0인 멤버(예: 2원을 3명이 나눔, 가중치 0)는 줄을 만들지 않는다.
    `rotation`은 남는 단위를 받을 순서의 출발점(`allocate` 참고). DB는 분개 순번을 넘긴다.
    """
    if not total.is_positive():
        raise InvalidAmountError(f"지출 금액은 양수여야 한다: {total}")
    shares = split.shares(total, rotation)
    lines = [Line(payer, Side.CREDIT, total)]
    lines += [Line(m, Side.DEBIT, amount) for m, amount in shares.items() if amount.is_positive()]
    return JournalEntry(EntryKind.EXPENSE, tuple(lines), memo)


def transfer_entry(
    sender: MemberId, receiver: MemberId, amount: Money, memo: str = ""
) -> JournalEntry:
    """`sender`가 `receiver`에게 `amount`를 보냈다 (정산 송금)."""
    if sender == receiver:
        raise InvalidSplitError("자기 자신에게 송금할 수 없다")
    if not amount.is_positive():
        raise InvalidAmountError(f"송금액은 양수여야 한다: {amount}")
    return JournalEntry(
        EntryKind.TRANSFER,
        (Line(sender, Side.CREDIT, amount), Line(receiver, Side.DEBIT, amount)),
        memo,
    )


def reversal_of(posted: PostedEntry, memo: str = "") -> JournalEntry:
    """`posted`의 모든 줄을 반대 방향으로 뒤집은 분개. 기록하면 원래 분개의 효과가 사라진다."""
    lines = tuple(
        Line(line.member, line.side.opposite(), line.amount) for line in posted.entry.lines
    )
    return JournalEntry(EntryKind.REVERSAL, lines, memo, reverses=posted.entry_id)


# --- 기록 전 검사 ---
# 메모리 원장(`Ledger`)과 DB 저장 계층(`app.db.store`)이 같은 함수를 쓴다.
# 규칙을 한 곳에만 두어, 한쪽만 고쳐서 두 구현이 어긋나는 일을 막는다.


def check_postable(entry: JournalEntry, currency: Currency, members: Iterable[MemberId]) -> None:
    """분개를 이 원장(통화 `currency`, 멤버 `members`)에 기록할 수 있는지 검사한다."""
    if entry.currency != currency:
        raise CurrencyMismatchError(
            f"원장 통화는 {currency.code}인데 분개는 {entry.currency.code}다"
        )
    unknown = entry.members() - frozenset(members)
    if unknown:
        raise UnknownMemberError(f"원장에 없는 멤버: {sorted(unknown)}")


def check_reversible(target: PostedEntry, already_reversed: bool) -> None:
    """`target`을 취소할 수 있는지 검사한다. `already_reversed`는 이미 역분개가 있는지."""
    if target.entry.kind is EntryKind.REVERSAL:
        # 취소의 취소는 허용하지 않는다. 다시 필요하면 원래 거래를 새로 기록한다.
        # (취소와 재취소가 얽힌 이력보다 읽기 쉽다)
        raise AlreadyReversedError(f"역분개 {target.entry_id}는 취소할 수 없다")
    if already_reversed:
        raise AlreadyReversedError(f"분개 {target.entry_id}는 이미 취소됐다")


def check_reversal(entry: JournalEntry, target: PostedEntry, already_reversed: bool) -> None:
    """역분개 `entry`가 `target`을 정확히 뒤집은 것이고 취소가 허용되는지 검사한다."""
    check_reversible(target, already_reversed)
    if entry.reverses != target.entry_id or entry.lines != reversal_of(target).lines:
        raise UnbalancedEntryError("역분개의 줄이 원래 분개를 정확히 뒤집은 것이 아니다")


# --- 원장 ---


class Ledger:
    """모임 하나의 원장. 통화 하나(모임의 기준 통화)만 쓴다. 다중 통화는 5단계.

    순수 메모리 객체다. DB 저장(3단계)은 이 객체의 규칙을 그대로 테이블로 옮긴다.
    """

    def __init__(self, currency: Currency, members: Iterable[MemberId] = ()):
        self.currency = currency
        self._members: set[MemberId] = set()
        self._entries: list[PostedEntry] = []
        self._reversed: set[int] = set()  # 이미 취소된 분개 번호
        for m in members:
            self.add_member(m)

    # --- 멤버 ---

    def add_member(self, member: MemberId) -> None:
        if not member:
            raise UnknownMemberError("멤버 ID가 비었다")
        self._members.add(member)

    @property
    def members(self) -> frozenset[MemberId]:
        return frozenset(self._members)

    # --- 기록 ---

    def post(self, entry: JournalEntry) -> PostedEntry:
        """분개를 검사하고 원장 끝에 추가한다. 실패하면 원장은 바뀌지 않는다."""
        check_postable(entry, self.currency, self._members)
        if entry.kind is EntryKind.REVERSAL:
            target = self.get(entry.reverses)  # type: ignore[arg-type]
            check_reversal(entry, target, self.is_reversed(target.entry_id))
            self._reversed.add(target.entry_id)

        posted = PostedEntry(len(self._entries) + 1, entry)
        self._entries.append(posted)
        return posted

    def reverse(self, entry_id: int, memo: str = "") -> PostedEntry:
        """분개 `entry_id`를 취소하는 역분개를 기록한다."""
        target = self.get(entry_id)
        check_reversible(target, self.is_reversed(entry_id))
        return self.post(reversal_of(target, memo))

    # --- 조회 ---

    def get(self, entry_id: int) -> PostedEntry:
        if not 1 <= entry_id <= len(self._entries):
            raise EntryNotFoundError(f"분개 {entry_id}가 없다")
        return self._entries[entry_id - 1]

    @property
    def entries(self) -> tuple[PostedEntry, ...]:
        return tuple(self._entries)

    def is_reversed(self, entry_id: int) -> bool:
        return entry_id in self._reversed

    def balances(self) -> dict[MemberId, Money]:
        """멤버별 잔액 (대변 합계 - 차변 합계). 기록이 없는 멤버는 0. 합은 항상 0이다.

        매번 전체 분개를 다시 더한다. DB도 같은 방식(SUM)을 쓴다. 잔액 테이블과의 비교는
        docs/experiments.md 4단계.
        """
        minor: dict[MemberId, int] = {m: 0 for m in sorted(self._members)}
        for posted in self._entries:
            for line in posted.entry.lines:
                delta = line.amount.minor_units
                minor[line.member] += delta if line.side is Side.CREDIT else -delta
        return {m: Money.from_minor(v, self.currency) for m, v in minor.items()}


def total_balance(balances: Mapping[MemberId, Money], currency: Currency) -> Money:
    """잔액의 합. 원장이 올바르면 항상 0이다."""
    return sum_money(list(balances.values()), currency)
