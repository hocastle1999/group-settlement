from itertools import combinations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.ledger import (
    CurrencyMismatchError,
    EqualSplit,
    Ledger,
    Money,
    expense_entry,
    get_currency,
    transfer_entry,
)
from app.ledger.settle import (
    Strategy,
    TooManyMembersError,
    Transfer,
    UnbalancedBalancesError,
    greedy_transfers,
    optimal_transfers,
    settle,
    zero_sum_groups,
)


def krw(n: int) -> Money:
    return Money.of(n, "KRW")


def balances_of(**values: int) -> dict[str, Money]:
    return {m: krw(v) for m, v in values.items()}


def apply(balances: dict[str, Money], transfers: list[Transfer]) -> dict[str, Money]:
    """송금을 반영한 잔액. 보낸 사람은 받을 돈이 늘고(빚이 줄고), 받은 사람은 줄어든다."""
    result = dict(balances)
    for t in transfers:
        result[t.sender] = result[t.sender] + t.amount
        result[t.receiver] = result[t.receiver] - t.amount
    return result


# 그리디가 최적이 아닌 가장 작은 예 (scripts/compare_settlement.py로 찾음)
#   최적: {a, c}(-3 + 3)와 {b, d, e}(2 + 2 - 4) 두 묶음 -> 1 + 2 = 3번
#   그리디: 가장 큰 채권자 c(3)와 가장 큰 채무자 e(-4)를 먼저 짝지어 묶음이 섞인다 -> 4번
GREEDY_WORSE = balances_of(a=-3, b=2, c=3, d=2, e=-4)


def test_simple_dinner():
    # A가 30,000원을 내고 셋이 균등 -> B, C가 각각 A에게 10,000원
    balances = balances_of(a=20_000, b=-10_000, c=-10_000)
    expected = [Transfer("b", "a", krw(10_000)), Transfer("c", "a", krw(10_000))]
    assert greedy_transfers(balances) == expected
    assert optimal_transfers(balances) == expected


def test_greedy_is_not_always_optimal():
    greedy = greedy_transfers(GREEDY_WORSE)
    optimal = optimal_transfers(GREEDY_WORSE)
    assert len(greedy) == 4
    assert len(optimal) == 3
    assert all(v.is_zero() for v in apply(GREEDY_WORSE, greedy).values())
    assert all(v.is_zero() for v in apply(GREEDY_WORSE, optimal).values())


def test_zero_sum_groups_example():
    minor = {m: v.minor_units for m, v in GREEDY_WORSE.items()}
    groups = sorted(sorted(g) for g in zero_sum_groups(minor))
    assert groups == [["a", "c"], ["b", "d", "e"]]


def test_all_settled_means_no_transfers():
    assert greedy_transfers(balances_of(a=0, b=0)) == []
    assert optimal_transfers(balances_of(a=0, b=0)) == []
    assert settle({}) == []


def test_unbalanced_input_rejected():
    with pytest.raises(UnbalancedBalancesError):
        greedy_transfers(balances_of(a=100, b=-99))
    with pytest.raises(UnbalancedBalancesError):
        optimal_transfers(balances_of(a=100, b=-99))


def test_mixed_currency_rejected():
    with pytest.raises(CurrencyMismatchError):
        greedy_transfers({"a": krw(1), "b": Money.of("-1.00", "USD")})


def test_optimal_limit_and_auto_fallback():
    many = {f"m{i:02}": krw(1 if i % 2 else -1) for i in range(20)}
    with pytest.raises(TooManyMembersError):
        optimal_transfers(many, max_members=16)
    # AUTO는 한도를 넘으면 그리디로 내려간다
    assert settle(many, Strategy.AUTO, max_members=16) == greedy_transfers(many)
    assert settle(GREEDY_WORSE) == optimal_transfers(GREEDY_WORSE)
    assert settle(GREEDY_WORSE, Strategy.GREEDY) == greedy_transfers(GREEDY_WORSE)


def test_usd_cents():
    balances = {"a": Money.of("10.01", "USD"), "b": Money.of("-10.01", "USD")}
    assert optimal_transfers(balances) == [Transfer("b", "a", Money.of("10.01", "USD"))]


def test_settling_a_real_ledger():
    ledger = Ledger(get_currency("KRW"), ["a", "b", "c", "d"])
    ledger.post(expense_entry("a", krw(40_000), EqualSplit(["a", "b", "c", "d"]), "숙소"))
    ledger.post(expense_entry("b", krw(12_000), EqualSplit(["a", "b", "c"]), "택시"))
    ledger.post(expense_entry("d", krw(9_001), EqualSplit(["c", "d"]), "커피"))
    for t in settle(ledger.balances()):
        ledger.post(transfer_entry(t.sender, t.receiver, t.amount, "정산"))
    assert all(v.is_zero() for v in ledger.balances().values())


# --- property-based ---


def brute_force_min_transfers(values: list[int]) -> int:
    """DP와 독립적인 검증용 정답: 합이 0인 묶음으로 나누는 모든 방법을 재귀로 시도한다.

    작은 k에서만 쓴다. 첫 사람이 들어갈 묶음(합 0인 부분집합)을 고르고 나머지를 재귀.
    """
    values = [v for v in values if v != 0]

    def max_groups(rest: tuple[int, ...]) -> int:
        if not rest:
            return 0
        first, others = rest[0], rest[1:]
        best = 0
        for size in range(len(others) + 1):
            for idx in combinations(range(len(others)), size):
                if first + sum(others[i] for i in idx) == 0:
                    remaining = tuple(v for i, v in enumerate(others) if i not in idx)
                    best = max(best, 1 + max_groups(remaining))
        return best

    return len(values) - max_groups(tuple(values))


def balances_st(max_members: int, max_abs: int):
    """합이 0인 무작위 잔액. 마지막 사람이 나머지를 맞춘다."""
    return st.lists(st.integers(-max_abs, max_abs), min_size=1, max_size=max_members - 1).map(
        lambda vs: {f"m{i:02}": krw(v) for i, v in enumerate([*vs, -sum(vs)])}
    )


@given(balances=balances_st(max_members=12, max_abs=50))
def test_both_strategies_settle_everything(balances):
    for transfers in (greedy_transfers(balances), optimal_transfers(balances)):
        assert all(v.is_zero() for v in apply(balances, transfers).values())
        for t in transfers:
            assert t.amount.is_positive()
            # 채무자만 보내고 채권자만 받는다 (한 사람이 보내기도 하고 받기도 하지 않는다)
            assert balances[t.sender].is_negative()
            assert balances[t.receiver].is_positive()


@given(balances=balances_st(max_members=12, max_abs=50))
def test_optimal_never_worse_than_greedy_and_greedy_bounded(balances):
    k = sum(1 for v in balances.values() if not v.is_zero())
    greedy = greedy_transfers(balances)
    optimal = optimal_transfers(balances)
    assert len(optimal) <= len(greedy) <= max(k - 1, 0)


@given(balances=balances_st(max_members=8, max_abs=20))
def test_optimal_matches_brute_force(balances):
    expected = brute_force_min_transfers([v.minor_units for v in balances.values()])
    assert len(optimal_transfers(balances)) == expected


@given(balances=balances_st(max_members=10, max_abs=100), data=st.data())
def test_result_independent_of_input_order(balances, data):
    keys = data.draw(st.permutations(list(balances)))
    shuffled = {k: balances[k] for k in keys}
    assert greedy_transfers(balances) == greedy_transfers(shuffled)
    assert optimal_transfers(balances) == optimal_transfers(shuffled)
