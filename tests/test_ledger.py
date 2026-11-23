import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.ledger import (
    AlreadyReversedError,
    CurrencyMismatchError,
    EntryKind,
    EntryNotFoundError,
    EqualSplit,
    InvalidAmountError,
    InvalidSplitError,
    JournalEntry,
    Ledger,
    Line,
    Money,
    RatioSplit,
    Side,
    UnbalancedEntryError,
    UnknownMemberError,
    expense_entry,
    get_currency,
    total_balance,
    transfer_entry,
)

KRW = get_currency("KRW")


def krw(n: int) -> Money:
    return Money.of(n, "KRW")


@pytest.fixture
def ledger() -> Ledger:
    return Ledger(KRW, ["a", "b", "c"])


def test_expense_creates_balanced_entry(ledger):
    entry = expense_entry("a", krw(30_000), EqualSplit(["a", "b", "c"]), "저녁")
    assert entry.total(Side.DEBIT) == entry.total(Side.CREDIT) == krw(30_000)
    ledger.post(entry)
    assert ledger.balances() == {"a": krw(20_000), "b": krw(-10_000), "c": krw(-10_000)}


def test_transfers_settle_balances_to_zero(ledger):
    ledger.post(expense_entry("a", krw(30_000), EqualSplit(["a", "b", "c"])))
    ledger.post(transfer_entry("b", "a", krw(10_000)))
    ledger.post(transfer_entry("c", "a", krw(10_000)))
    assert all(v.is_zero() for v in ledger.balances().values())


def test_payer_not_participating(ledger):
    # a가 b와 c의 택시비만 대신 냄
    ledger.post(expense_entry("a", krw(10_000), EqualSplit(["b", "c"])))
    assert ledger.balances() == {"a": krw(10_000), "b": krw(-5_000), "c": krw(-5_000)}


def test_zero_shares_produce_no_lines():
    # 2원을 3명이 나누면 한 명은 0원 -> 그 사람 줄은 없다 (줄 금액은 항상 양수)
    entry = expense_entry("a", krw(2), EqualSplit(["a", "b", "c"]))
    debit_members = {line.member for line in entry.lines if line.side is Side.DEBIT}
    assert debit_members == {"a", "b"}


def test_reversal_restores_previous_balances(ledger):
    first = ledger.post(expense_entry("a", krw(9_000), EqualSplit(["a", "b", "c"])))
    before = ledger.balances()
    wrong = ledger.post(expense_entry("b", krw(50_000), RatioSplit({"a": 1, "c": 3})))
    rev = ledger.reverse(wrong.entry_id, "금액 잘못 입력")

    assert ledger.balances() == before
    assert rev.entry.kind is EntryKind.REVERSAL
    assert rev.entry.reverses == wrong.entry_id
    # 원래 분개는 지워지지 않고 남는다 (append-only)
    assert [p.entry_id for p in ledger.entries] == [first.entry_id, wrong.entry_id, rev.entry_id]
    assert ledger.is_reversed(wrong.entry_id)


def test_cannot_reverse_twice_or_reverse_a_reversal(ledger):
    posted = ledger.post(transfer_entry("a", "b", krw(1_000)))
    rev = ledger.reverse(posted.entry_id)
    with pytest.raises(AlreadyReversedError):
        ledger.reverse(posted.entry_id)
    with pytest.raises(AlreadyReversedError):
        ledger.reverse(rev.entry_id)


def test_forged_reversal_rejected(ledger):
    posted = ledger.post(transfer_entry("a", "b", krw(1_000)))
    # 줄이 원래 분개를 정확히 뒤집지 않은 "역분개"
    fake = JournalEntry(
        EntryKind.REVERSAL,
        (Line("a", Side.DEBIT, krw(500)), Line("b", Side.CREDIT, krw(500))),
        reverses=posted.entry_id,
    )
    with pytest.raises(UnbalancedEntryError):
        ledger.post(fake)
    assert not ledger.is_reversed(posted.entry_id)
    assert len(ledger.entries) == 1


def test_unbalanced_entry_cannot_be_created():
    with pytest.raises(UnbalancedEntryError):
        JournalEntry(
            EntryKind.EXPENSE,
            (Line("a", Side.CREDIT, krw(100)), Line("b", Side.DEBIT, krw(99))),
        )
    with pytest.raises(UnbalancedEntryError):
        JournalEntry(EntryKind.EXPENSE, (Line("a", Side.CREDIT, krw(100)),))
    with pytest.raises(UnbalancedEntryError):
        # 역분개가 아닌데 reverses가 있음
        JournalEntry(
            EntryKind.EXPENSE,
            (Line("a", Side.CREDIT, krw(1)), Line("b", Side.DEBIT, krw(1))),
            reverses=1,
        )


def test_line_amount_must_be_positive():
    with pytest.raises(InvalidAmountError):
        Line("a", Side.DEBIT, krw(0))


def test_mixed_currency_entry_rejected():
    with pytest.raises(CurrencyMismatchError):
        JournalEntry(
            EntryKind.TRANSFER,
            (Line("a", Side.CREDIT, krw(1)), Line("b", Side.DEBIT, Money.of("1.00", "USD"))),
        )


def test_post_rejects_wrong_currency_and_unknown_member(ledger):
    with pytest.raises(CurrencyMismatchError):
        ledger.post(transfer_entry("a", "b", Money.of("1.00", "USD")))
    with pytest.raises(UnknownMemberError):
        ledger.post(expense_entry("a", krw(100), EqualSplit(["a", "zed"])))
    assert ledger.entries == ()  # 실패하면 아무것도 기록되지 않는다


def test_invalid_expense_and_transfer():
    with pytest.raises(InvalidAmountError):
        expense_entry("a", krw(0), EqualSplit(["a"]))
    with pytest.raises(InvalidSplitError):
        transfer_entry("a", "a", krw(1))
    with pytest.raises(InvalidAmountError):
        transfer_entry("a", "b", krw(-1))


def test_entry_not_found(ledger):
    with pytest.raises(EntryNotFoundError):
        ledger.get(1)
    with pytest.raises(EntryNotFoundError):
        ledger.reverse(0)


def test_member_without_entries_has_zero_balance(ledger):
    ledger.post(transfer_entry("a", "b", krw(500)))
    assert ledger.balances()["c"] == krw(0)


# --- property-based: 무작위 거래 순서에서도 잔액 합은 0 ---

MEMBERS = ["a", "b", "c", "d", "e"]
member_st = st.sampled_from(MEMBERS)
amount_st = st.integers(1, 10**9)

expense_op = st.tuples(
    st.just("expense"),
    member_st,
    amount_st,
    st.lists(member_st, min_size=1, max_size=5, unique=True),
)
ratio_op = st.tuples(
    st.just("ratio"),
    member_st,
    amount_st,
    st.dictionaries(member_st, st.integers(0, 100), min_size=1).filter(
        lambda w: sum(w.values()) > 0
    ),
)
transfer_op = st.tuples(st.just("transfer"), member_st, member_st, amount_st).filter(
    lambda op: op[1] != op[2]
)
reverse_op = st.tuples(st.just("reverse"), st.integers(1, 50))
ops_st = st.lists(st.one_of(expense_op, ratio_op, transfer_op, reverse_op), max_size=40)


def apply_ops(ledger: Ledger, ops) -> None:
    for op in ops:
        match op:
            case ("expense", payer, amount, participants):
                ledger.post(expense_entry(payer, krw(amount), EqualSplit(participants)))
            case ("ratio", payer, amount, weights):
                ledger.post(expense_entry(payer, krw(amount), RatioSplit(weights)))
            case ("transfer", sender, receiver, amount):
                ledger.post(transfer_entry(sender, receiver, krw(amount)))
            case ("reverse", entry_id):
                # 없는 번호, 이미 취소됨, 역분개 취소는 거부돼야 하고 원장은 그대로여야 한다
                count = len(ledger.entries)
                try:
                    ledger.reverse(entry_id)
                except (EntryNotFoundError, AlreadyReversedError):
                    assert len(ledger.entries) == count


@given(ops=ops_st)
def test_balances_always_sum_to_zero(ops):
    ledger = Ledger(KRW, MEMBERS)
    apply_ops(ledger, ops)
    assert total_balance(ledger.balances(), KRW).is_zero()


@given(ops=ops_st)
def test_reversing_everything_returns_to_zero(ops):
    ledger = Ledger(KRW, MEMBERS)
    apply_ops(ledger, ops)
    for posted in ledger.entries:
        if posted.entry.kind is not EntryKind.REVERSAL and not ledger.is_reversed(posted.entry_id):
            ledger.reverse(posted.entry_id)
    assert all(v.is_zero() for v in ledger.balances().values())


@given(ops=ops_st)
def test_every_posted_entry_is_balanced(ops):
    ledger = Ledger(KRW, MEMBERS)
    apply_ops(ledger, ops)
    for posted in ledger.entries:
        assert posted.entry.total(Side.DEBIT) == posted.entry.total(Side.CREDIT)
