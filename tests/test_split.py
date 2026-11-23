from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.ledger import (
    EqualSplit,
    FixedSplit,
    InvalidSplitError,
    Money,
    RatioSplit,
    allocate,
    sum_money,
)


def krw(n: int) -> Money:
    return Money.of(n, "KRW")


def test_equal_split_remainder_goes_by_member_id():
    # 10,000 / 3 = 3,333.33 -> 남는 1원은 ID 순서가 가장 앞인 a가 낸다
    shares = EqualSplit(["c", "a", "b"]).shares(krw(10_000))
    assert shares == {"a": krw(3_334), "b": krw(3_333), "c": krw(3_333)}


def test_equal_split_input_order_does_not_matter():
    total = krw(10_001)
    assert EqualSplit(["a", "b", "c"]).shares(total) == EqualSplit(["c", "b", "a"]).shares(total)


def test_equal_split_usd_cents():
    shares = EqualSplit(["a", "b", "c"]).shares(Money.of("100.00", "USD"))
    assert [str(v) for v in shares.values()] == ["33.34 USD", "33.33 USD", "33.33 USD"]


def test_ratio_split_largest_remainder():
    # 100원을 1:1:1:... 이 아닌 2:1:1로 -> 50, 25, 25
    assert RatioSplit({"a": 2, "b": 1, "c": 1}).shares(krw(100)) == {
        "a": krw(50),
        "b": krw(25),
        "c": krw(25),
    }
    # 10원을 50:30:20 -> 정확히 5, 3, 2
    assert RatioSplit({"a": 50, "b": 30, "c": 20}).shares(krw(10)) == {
        "a": krw(5),
        "b": krw(3),
        "c": krw(2),
    }


def test_ratio_split_remainder_goes_to_largest_fraction_not_first_id():
    # 7원을 1:2 -> 정확한 몫 2.33, 4.67. 내림 2, 4 후 남는 1원은 소수 부분이 큰 b(0.67)에게
    assert RatioSplit({"a": 1, "b": 2}).shares(krw(7)) == {"a": krw(2), "b": krw(5)}


def test_ratio_split_zero_weight_pays_nothing():
    assert RatioSplit({"a": 1, "b": 0}).shares(krw(100)) == {"a": krw(100), "b": krw(0)}


@pytest.mark.parametrize(
    "weights",
    [{}, {"a": 0, "b": 0}, {"a": -1, "b": 2}, {"a": 1.5}, {"a": True}],
)
def test_ratio_split_invalid_weights(weights):
    with pytest.raises(InvalidSplitError):
        RatioSplit(weights)


def test_ratio_split_copies_input():
    weights = {"a": 1, "b": 1}
    rule = RatioSplit(weights)
    weights["a"] = 100
    assert rule.shares(krw(100)) == {"a": krw(50), "b": krw(50)}


def test_equal_split_invalid():
    with pytest.raises(InvalidSplitError):
        EqualSplit([])
    with pytest.raises(InvalidSplitError):
        EqualSplit(["a", "a"])


def test_fixed_split_must_match_total():
    rule = FixedSplit({"a": krw(7_000), "b": krw(3_000)})
    assert rule.shares(krw(10_000)) == {"a": krw(7_000), "b": krw(3_000)}
    with pytest.raises(InvalidSplitError):
        rule.shares(krw(10_001))  # 차액 1원을 누군가에게 몰래 얹지 않는다


def test_fixed_split_rejects_negative_and_other_currency():
    with pytest.raises(InvalidSplitError):
        FixedSplit({"a": krw(-1), "b": krw(1)})
    with pytest.raises(InvalidSplitError):
        FixedSplit({"a": Money.of("1.00", "USD")}).shares(krw(1))


def test_allocate_negative_total_mirrors_positive():
    pos = allocate(krw(10), {"a": 1, "b": 1, "c": 1})
    neg = allocate(krw(-10), {"a": 1, "b": 1, "c": 1})
    assert neg == {m: -v for m, v in pos.items()}


# --- property-based: 무작위 입력 수백 개에서 불변식이 지켜지는지 ---

member_ids = st.text(alphabet="abcdefghij", min_size=1, max_size=3)
weights_st = st.dictionaries(member_ids, st.integers(0, 1_000), min_size=1, max_size=12).filter(
    lambda w: sum(w.values()) > 0
)
currency_st = st.sampled_from(["KRW", "USD", "JPY"])


@given(total=st.integers(-(10**12), 10**12), weights=weights_st, code=currency_st)
def test_allocate_sum_is_exact(total, weights, code):
    money = Money.from_minor(total, code)
    shares = allocate(money, weights)
    assert sum_money(list(shares.values()), money.currency) == money
    assert set(shares) == set(weights)


@given(total=st.integers(0, 10**12), weights=weights_st)
def test_allocate_each_share_within_one_unit_of_exact(total, weights):
    shares = allocate(Money.from_minor(total, "KRW"), weights)
    weight_sum = sum(weights.values())
    for member, share in shares.items():
        exact = Fraction(total * weights[member], weight_sum)
        # 최대 나머지 방식은 각자 몫을 정확한 값의 내림 또는 올림으로만 만든다
        assert exact - 1 < share.minor_units < exact + 1


@given(total=st.integers(0, 10**9), weights=weights_st)
def test_allocate_zero_weight_gets_zero(total, weights):
    shares = allocate(Money.from_minor(total, "KRW"), weights)
    for member, w in weights.items():
        if w == 0:
            assert shares[member].is_zero()


@given(total=st.integers(0, 10**9), weights=weights_st, data=st.data())
def test_allocate_is_deterministic_regardless_of_input_order(total, weights, data):
    shuffled_keys = data.draw(st.permutations(list(weights)))
    shuffled = {k: weights[k] for k in shuffled_keys}
    money = Money.from_minor(total, "KRW")
    assert allocate(money, weights) == allocate(money, shuffled)


# --- 남는 단위의 출발점 돌리기 (5단계) ---


def test_rotation_moves_who_pays_the_leftover():
    split = EqualSplit(["a", "b", "c"])
    payers = [
        next(m for m, v in split.shares(krw(10_000), rotation=r).items() if v == krw(3_334))
        for r in range(4)
    ]
    assert payers == ["a", "b", "c", "a"]  # 0번째부터 돌고 n마다 반복


@given(
    total=st.integers(1, 10**9),
    members=st.sets(member_ids, min_size=1, max_size=10),
    data=st.data(),
)
def test_rotation_spreads_leftover_evenly_over_a_cycle(total, members, data):
    """균등 분할을 rotation 0..n-1로 n번 하면, 남는 단위를 모두가 정확히 같은 횟수만큼 받는다."""
    members = sorted(members)
    n = len(members)
    leftover = total % n
    received = {m: 0 for m in members}
    for r in range(n):
        shares = allocate(Money.from_minor(total, "KRW"), {m: 1 for m in members}, r)
        for m, share in shares.items():
            received[m] += share.minor_units - total // n
    assert set(received.values()) == {leftover}
    rotation = data.draw(st.integers(-(10**6), 10**6))
    money = Money.from_minor(total, "KRW")
    weights = {m: 1 for m in members}
    assert allocate(money, weights, rotation) == allocate(money, weights, rotation + n)


@given(total=st.integers(-(10**12), 10**12), weights=weights_st, rotation=st.integers(0, 10**6))
def test_allocate_with_rotation_keeps_sum_and_bounds(total, weights, rotation):
    money = Money.from_minor(total, "KRW")
    shares = allocate(money, weights, rotation)
    assert sum_money(list(shares.values()), money.currency) == money
    weight_sum = sum(weights.values())
    for member, share in shares.items():
        exact = Fraction(abs(total) * weights[member], weight_sum)
        assert exact - 1 < abs(share.minor_units) < exact + 1
