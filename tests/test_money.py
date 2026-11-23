from decimal import Decimal

import pytest

from app.ledger import CurrencyMismatchError, InvalidAmountError, Money, get_currency

KRW = get_currency("KRW")
USD = get_currency("USD")


def test_of_accepts_str_int_decimal():
    assert Money.of("12.34", "USD").amount == Decimal("12.34")
    assert Money.of(1000, "KRW").amount == Decimal("1000")
    assert Money.of(Decimal("5"), USD).amount == Decimal("5.00")


@pytest.mark.parametrize("bad", [0.1, 1.0, True])
def test_float_and_bool_rejected(bad):
    # 0.1은 이진수로 정확히 표현되지 않는다: Decimal(0.1) == 0.1000000000000000055...
    with pytest.raises(InvalidAmountError):
        Money.of(bad, "USD")


def test_direct_construction_requires_decimal():
    with pytest.raises(InvalidAmountError):
        Money(1000, KRW)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("amount", "code"),
    [("1.5", "KRW"), ("0.001", "USD"), ("10.5", "JPY")],
)
def test_finer_than_minor_unit_rejected(amount, code):
    with pytest.raises(InvalidAmountError):
        Money.of(amount, code)


@pytest.mark.parametrize("bad", ["abc", "", "NaN", "Infinity"])
def test_non_numeric_rejected(bad):
    with pytest.raises(InvalidAmountError):
        Money.of(bad, "KRW")


def test_huge_amount_rejected_cleanly():
    with pytest.raises(InvalidAmountError):
        Money.of("1" + "0" * 40, "USD")


def test_unknown_currency_rejected():
    with pytest.raises(InvalidAmountError):
        Money.of(1, "XYZ")


def test_normalized_representation():
    # 같은 값이면 같은 모양: 1E+3원 == 1000원, 5달러 == 5.00달러
    assert str(Money.of("1E+3", "KRW")) == "1000 KRW"
    assert str(Money.of("5", "USD")) == "5.00 USD"
    assert Money.of("1E+3", "KRW") == Money.of(1000, "KRW")


def test_minor_units_round_trip():
    m = Money.of("12.34", "USD")
    assert m.minor_units == 1234
    assert Money.from_minor(1234, "USD") == m
    assert Money.from_minor(-500, "KRW").amount == Decimal("-500")


def test_arithmetic_and_comparison():
    a, b = Money.of("10.50", "USD"), Money.of("0.25", "USD")
    assert a + b == Money.of("10.75", "USD")
    assert a - b == Money.of("10.25", "USD")
    assert -a == Money.of("-10.50", "USD")
    assert abs(-a) == a
    assert b < a and a > b and a >= a and b <= b


def test_currency_mismatch():
    with pytest.raises(CurrencyMismatchError):
        Money.of(1, "USD") + Money.of(1, "EUR")
    with pytest.raises(CurrencyMismatchError):
        Money.of(1, "USD") < Money.of(1, "EUR")  # noqa: B015


def test_float_classic_error_does_not_happen():
    # float: 0.1 + 0.2 == 0.30000000000000004
    assert Money.of("0.10", "USD") + Money.of("0.20", "USD") == Money.of("0.30", "USD")
