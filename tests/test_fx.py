"""환율과 환산 테스트 (5단계, 순수 파이썬)."""

from decimal import Decimal
from fractions import Fraction

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from app.ledger import (
    CurrencyMismatchError,
    EntryKind,
    EqualSplit,
    FixedSplit,
    ForeignAmount,
    InvalidAmountError,
    InvalidExchangeRateError,
    InvalidSplitError,
    JournalEntry,
    Ledger,
    Line,
    Money,
    RatioSplit,
    Side,
    UnbalancedEntryError,
    convert,
    foreign_expense_entry,
    get_currency,
    reversal_of,
    sum_money,
)

KRW = get_currency("KRW")
USD = get_currency("USD")
JPY = get_currency("JPY")
KWD = get_currency("KWD")


def half_even(value: Fraction) -> int:
    """정확한 분수 값을 가장 가까운 정수로, 정확히 가운데면 짝수로 (파이썬 round의 규칙)."""
    return round(value)


# ---------------------------------------------------------------- 환산


def test_convert_examples():
    assert convert(Money.of(3000, "JPY"), Decimal("9.1234"), KRW) == Money.of(27370, "KRW")
    assert convert(Money.of("10.00", "USD"), Decimal("1400.5"), KRW) == Money.of(14005, "KRW")
    assert convert(Money.of(10_000, "KRW"), Decimal("0.00071234"), USD) == Money.of("7.12", "USD")
    assert convert(Money.of("1.234", "KWD"), Decimal("3.25"), USD) == Money.of("4.01", "USD")


@pytest.mark.parametrize(
    ("cents", "expected"),
    [(1, 0), (3, 2), (5, 2), (7, 4)],  # 0.5 -> 0, 1.5 -> 2, 2.5 -> 2, 3.5 -> 4
)
def test_convert_rounds_half_to_even(cents, expected):
    # 0.01달러 x 50 = 0.5원 같은 정확한 가운데 값
    assert convert(Money.from_minor(cents, "USD"), Decimal(50), KRW) == Money.of(expected, "KRW")


@pytest.mark.parametrize(
    "rate",
    [
        Decimal(0),
        Decimal(-1),
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("1.00000000001"),
        Decimal("10000000000"),
    ],
)
def test_invalid_rates_are_rejected(rate):
    with pytest.raises(InvalidExchangeRateError):
        convert(Money.of(1, "USD"), rate, KRW)


def test_float_rate_and_same_currency_are_rejected():
    with pytest.raises(InvalidExchangeRateError):
        convert(Money.of(1, "USD"), 1.5, KRW)  # type: ignore[arg-type]
    with pytest.raises(CurrencyMismatchError):
        convert(Money.of(1, "KRW"), Decimal(1), KRW)


def test_trailing_zeros_do_not_count_as_extra_decimals():
    assert convert(Money.of(1, "USD"), Decimal("1400.000000000000"), KRW) == Money.of(1400, "KRW")


def test_converted_amount_that_does_not_fit_is_rejected():
    with pytest.raises(InvalidAmountError):
        convert(Money.of(10**14, "USD"), Decimal("9999"), KRW)


@given(
    minor=st.integers(1, 10**13),
    rate_units=st.integers(1, 10**14),
    rate_exp=st.integers(0, 10),
    pair=st.sampled_from([("USD", "KRW"), ("JPY", "KRW"), ("KRW", "USD"), ("KWD", "JPY")]),
)
def test_convert_equals_exact_half_even_rounding(minor, rate_units, rate_exp, pair):
    """큰 금액과 긴 환율에서도 정확한 분수 계산을 반올림한 값과 같다 (중간 정밀도 손실 없음)."""
    src, dst = (get_currency(c) for c in pair)
    rate = Decimal(rate_units).scaleb(-rate_exp)
    assume(rate < 10**10)  # 환율 정수부는 10자리까지 (그 이상은 거부 테스트가 따로 있다)
    amount = Money.from_minor(minor, src)
    exact = Fraction(amount.amount) * Fraction(rate) * 10**dst.exponent
    expected = half_even(exact)
    if abs(expected) >= 10 ** (15 + dst.exponent):
        with pytest.raises(InvalidAmountError):
            convert(amount, rate, dst)
        return
    assert convert(amount, rate, dst) == Money.from_minor(expected, dst)


# ---------------------------------------------------------------- 외화 지출


def test_foreign_expense_converts_total_once_then_splits():
    entry = foreign_expense_entry(
        "a", Money.of(3000, "JPY"), Decimal("9.1234"), KRW, EqualSplit(["a", "b", "c"]), "라멘"
    )
    assert entry.source == ForeignAmount(Money.of(3000, "JPY"), Decimal("9.1234"))
    assert entry.total(Side.CREDIT) == entry.total(Side.DEBIT) == Money.of(27370, "KRW")
    debits = sorted(ln.amount.minor_units for ln in entry.lines if ln.side is Side.DEBIT)
    assert debits == [9123, 9123, 9124]


def test_foreign_fixed_split_is_given_in_original_currency():
    """영수증 금액(엔)으로 정액 분할. 각자 따로 환산하지 않고 환산 총액을 비율대로 배분한다."""
    split = FixedSplit({m: Money.of(1000, "JPY") for m in ("a", "b", "c")})
    entry = foreign_expense_entry("a", Money.of(3000, "JPY"), Decimal("9.1235"), KRW, split)
    shares = {ln.member: ln.amount.minor_units for ln in entry.lines if ln.side is Side.DEBIT}
    # 각자 환산하면 1000 x 9.1235 = 9123.5 -> 9124 씩, 합 27372.
    # 총액은 3000 x 9.1235 = 27370.5 -> 27370. 2원이 어긋난다.
    each = convert(Money.of(1000, "JPY"), Decimal("9.1235"), KRW)
    assert each.minor_units * 3 == 27372
    # 환산 총액을 비율(1:1:1)대로 나누면 합이 정확히 총액이다
    assert entry.total(Side.CREDIT) == Money.of(27370, "KRW")
    assert shares == {"a": 9124, "b": 9123, "c": 9123}


def test_foreign_fixed_split_must_add_up_in_original_currency():
    split = FixedSplit({"a": Money.of(1000, "JPY"), "b": Money.of(1000, "JPY")})
    with pytest.raises(InvalidSplitError):
        foreign_expense_entry("a", Money.of(3000, "JPY"), Decimal("9.1"), KRW, split)


def test_amount_that_converts_to_zero_is_rejected():
    with pytest.raises(InvalidAmountError):
        foreign_expense_entry(
            "a", Money.of("0.001", "KWD"), Decimal("0.0001"), KRW, EqualSplit(["a", "b"])
        )


def test_source_must_match_recorded_amount():
    good = foreign_expense_entry(
        "a", Money.of(10, "USD"), Decimal(1400), KRW, EqualSplit(["a", "b"])
    )
    with pytest.raises(UnbalancedEntryError):
        JournalEntry(
            EntryKind.EXPENSE,
            good.lines,
            source=ForeignAmount(Money.of(10, "USD"), Decimal(1401)),  # 증빙과 기록이 다르다
        )
    with pytest.raises(UnbalancedEntryError):
        JournalEntry(
            EntryKind.TRANSFER,
            (
                Line("a", Side.CREDIT, Money.of(14000, "KRW")),
                Line("b", Side.DEBIT, Money.of(14000, "KRW")),
            ),
            source=good.source,
        )


def test_reversing_a_foreign_expense_restores_balances():
    ledger = Ledger(KRW, ["a", "b", "c"])
    posted = ledger.post(
        foreign_expense_entry(
            "a", Money.of("12.34", "USD"), Decimal("1388.77"), KRW, EqualSplit(["a", "b", "c"])
        )
    )
    reversal = reversal_of(posted)
    assert reversal.source is None  # 증빙은 원래 분개에만
    ledger.post(reversal)
    assert all(v.is_zero() for v in ledger.balances().values())


members_st = st.lists(st.sampled_from("abcdefgh"), min_size=1, max_size=8, unique=True)


@given(
    minor=st.integers(1, 10**9),
    rate_units=st.integers(1, 10**8),
    rate_exp=st.integers(0, 8),
    members=members_st,
    kind=st.sampled_from(["equal", "ratio", "fixed"]),
    rotation=st.integers(0, 1000),
    data=st.data(),
)
def test_foreign_expense_always_balances(
    minor, rate_units, rate_exp, members, kind, rotation, data
):
    """어떤 금액, 환율, 분할이든: 대변 = 차변 = 환산 총액, 정액 몫은 비율 값의 내림 또는 올림."""
    rate = Decimal(rate_units).scaleb(-rate_exp)
    original = Money.from_minor(minor, "USD")
    if kind == "equal":
        split = EqualSplit(members)
    elif kind == "ratio":
        split = RatioSplit({m: data.draw(st.integers(1, 9)) for m in members})
    else:
        cuts = sorted(
            data.draw(
                st.lists(
                    st.integers(0, minor), min_size=len(members) - 1, max_size=len(members) - 1
                )
            )
        )
        parts = [b - a for a, b in zip([0, *cuts], [*cuts, minor], strict=True)]
        split = FixedSplit(
            {m: Money.from_minor(p, "USD") for m, p in zip(members, parts, strict=True)}
        )
    try:
        total = convert(original, rate, KRW)
    except InvalidAmountError:
        return
    if not total.is_positive():
        with pytest.raises(InvalidAmountError):
            foreign_expense_entry(members[0], original, rate, KRW, split, rotation=rotation)
        return
    entry = foreign_expense_entry(members[0], original, rate, KRW, split, rotation=rotation)
    debits = [ln.amount for ln in entry.lines if ln.side is Side.DEBIT]
    assert entry.total(Side.CREDIT) == total
    assert sum_money(debits, KRW) == total
    if kind == "fixed":
        for ln in entry.lines:
            if ln.side is Side.DEBIT:
                exact = Fraction(total.minor_units) * Fraction(
                    split.amounts[ln.member].minor_units, minor
                )
                assert exact - 1 < ln.amount.minor_units < exact + 1
