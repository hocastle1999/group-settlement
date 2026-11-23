"""최소 송금: 잔액을 0으로 만드는 송금 목록 계산.

입력은 원장의 잔액(합이 0)이다. 양수는 돌려받을 사람(채권자), 음수는 내야 할 사람(채무자).
목표는 송금 횟수를 줄이는 것이다. 금액의 합은 어떤 방법이든 같다 (채무 합계 = 채권 합계).

두 가지 방법을 제공한다.

1. `greedy_transfers`: 가장 많이 받을 사람과 가장 많이 낼 사람을 짝지어 둘 중 작은 금액을 보낸다.
   한 번 보낼 때마다 최소 한 명의 잔액이 0이 되므로 송금은 최대 (잔액이 0이 아닌 사람 수 - 1)번이다.
   O(k log k). 항상 최적은 아니다.

2. `optimal_transfers`: 송금 횟수의 진짜 최솟값.
   핵심 관찰: 잔액 합이 0인 묶음 g개로 사람들을 나눌 수 있으면, 묶음마다 (묶음 인원 - 1)번이면 된다.
   그래서 최소 송금 = k - (합이 0인 묶음으로 나눌 수 있는 최대 개수).
   이 문제는 NP-hard(부분집합 합 문제를 포함)라서 비트마스크 DP로 O(2^k x k)에 푼다.
   k(잔액이 0이 아닌 사람 수)가 크면 너무 느려지므로 `max_members`로 막는다.

`settle`은 k가 작으면 최적, 크면 그리디를 쓴다.
"""

from __future__ import annotations

import heapq
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from app.ledger.errors import (
    CurrencyMismatchError,
    TooManyMembersError,
    UnbalancedBalancesError,
)
from app.ledger.money import Currency, Money
from app.ledger.split import MemberId

# 최적 DP를 허용하는 최대 인원. 실측 16명 37ms, 18명 170ms, 20명 870ms (docs/experiments.md).
# 요청 하나가 수십 ms 안에 끝나도록 16명으로 둔다. 넘으면 AUTO는 그리디를 쓴다.
DEFAULT_OPTIMAL_LIMIT = 16


class Strategy(StrEnum):
    GREEDY = "greedy"
    OPTIMAL = "optimal"
    AUTO = "auto"  # 인원이 적으면 최적, 많으면 그리디


@dataclass(frozen=True, slots=True)
class Transfer:
    """`sender`가 `receiver`에게 `amount`를 보낸다."""

    sender: MemberId
    receiver: MemberId
    amount: Money


def _to_minor(balances: Mapping[MemberId, Money]) -> tuple[Currency | None, dict[MemberId, int]]:
    """검증하고 0이 아닌 잔액만 최소 단위 정수로 바꾼다."""
    currencies = {m.currency for m in balances.values()}
    if len(currencies) > 1:
        raise CurrencyMismatchError(f"잔액에 통화가 섞였다: {sorted(c.code for c in currencies)}")
    currency = next(iter(currencies), None)
    minor = {member: m.minor_units for member, m in balances.items() if not m.is_zero()}
    if sum(minor.values()) != 0:
        raise UnbalancedBalancesError(f"잔액 합이 0이 아니다: {sum(minor.values())} (최소 단위)")
    return currency, minor


def _greedy_minor(minor: Mapping[MemberId, int]) -> list[tuple[MemberId, MemberId, int]]:
    """그리디의 본체. 정수 잔액(합 0)을 받아 (보내는 사람, 받는 사람, 금액) 목록을 돌려준다.

    힙에 (-금액, 멤버 ID)를 넣어 가장 큰 금액을 먼저 꺼낸다. 금액이 같으면 ID 순서(결정성).
    """
    creditors = [(-v, m) for m, v in minor.items() if v > 0]
    debtors = [(v, m) for m, v in minor.items() if v < 0]  # v가 음수라 그대로 넣으면 큰 빚이 먼저
    heapq.heapify(creditors)
    heapq.heapify(debtors)

    result: list[tuple[MemberId, MemberId, int]] = []
    while creditors and debtors:
        neg_credit, creditor = heapq.heappop(creditors)
        debt, debtor = heapq.heappop(debtors)
        credit, owe = -neg_credit, -debt
        amount = min(credit, owe)
        result.append((debtor, creditor, amount))
        # 남은 쪽만 다시 넣는다. 매번 적어도 한 명이 0이 되어 빠진다 -> 최대 k-1번.
        if credit > amount:
            heapq.heappush(creditors, (-(credit - amount), creditor))
        if owe > amount:
            heapq.heappush(debtors, (-(owe - amount), debtor))
    # 합이 0이므로 두 힙은 동시에 빈다.
    assert not creditors and not debtors
    return result


def _as_transfers(
    raw: list[tuple[MemberId, MemberId, int]], currency: Currency | None
) -> list[Transfer]:
    if not raw:
        return []
    assert currency is not None
    return [Transfer(s, r, Money.from_minor(a, currency)) for s, r, a in raw]


def greedy_transfers(balances: Mapping[MemberId, Money]) -> list[Transfer]:
    """그리디 정산. 빠르지만 송금 횟수가 최적보다 많을 수 있다."""
    currency, minor = _to_minor(balances)
    return _as_transfers(_greedy_minor(minor), currency)


def zero_sum_groups(minor: Mapping[MemberId, int]) -> list[list[MemberId]]:
    """사람들을 잔액 합이 0인 묶음으로, 묶음 개수가 최대가 되게 나눈다 (비트마스크 DP).

    dp[mask] = mask에 속한 사람들을 한 명씩 추가하는 순서 중, 지나는 길에 합이 0이 되는
    시점(묶음 하나가 닫히는 시점)의 최대 횟수.
        dp[mask] = max(dp[mask - {i}] for i in mask) + (1 if sum[mask] == 0 else 0)
    합이 0이 되는 시점 사이의 사람들이 하나의 묶음이다. dp[전체]가 최대 묶음 수.
    """
    members = sorted(minor)  # 결정성
    k = len(members)
    if k == 0:
        return []
    values = [minor[m] for m in members]
    full = (1 << k) - 1

    # sum[mask]: 가장 낮은 비트 하나를 뺀 mask의 합에 그 사람의 잔액을 더한다. O(2^k)
    sums = [0] * (1 << k)
    for mask in range(1, 1 << k):
        low = mask & -mask
        sums[mask] = sums[mask ^ low] + values[low.bit_length() - 1]

    dp = [0] * (1 << k)
    for mask in range(1, 1 << k):
        best = 0
        rest = mask
        while rest:
            low = rest & -rest
            rest ^= low
            prev = dp[mask ^ low]
            if prev > best:
                best = prev
        dp[mask] = best + (1 if sums[mask] == 0 else 0)

    # 역추적: 전체에서 한 명씩 빼면서 dp가 유지되는 길을 따라간다. 합이 0인 지점이 묶음 경계.
    groups: list[list[MemberId]] = []
    current: list[MemberId] = []
    mask = full
    while mask:
        closes = 1 if sums[mask] == 0 else 0
        rest = mask
        while rest:
            low = rest & -rest
            rest ^= low
            if dp[mask ^ low] == dp[mask] - closes:
                break
        if closes and current:
            # mask의 합이 0이고, 그 바깥(이미 뺀 사람들)도 합이 0 -> current가 하나의 묶음
            groups.append(current)
            current = []
        current.append(members[low.bit_length() - 1])
        mask ^= low
    groups.append(current)
    return groups


def optimal_transfers(
    balances: Mapping[MemberId, Money], max_members: int = DEFAULT_OPTIMAL_LIMIT
) -> list[Transfer]:
    """송금 횟수가 최소인 정산. 잔액이 0이 아닌 사람이 `max_members`명을 넘으면 거부한다."""
    currency, minor = _to_minor(balances)
    if len(minor) > max_members:
        raise TooManyMembersError(
            f"잔액이 남은 사람 {len(minor)}명 > 최적 계산 한도 {max_members}명"
        )
    raw: list[tuple[MemberId, MemberId, int]] = []
    for group in zero_sum_groups(minor):
        # 합이 0인 묶음 안에서는 그리디가 (묶음 인원 - 1)번 이하로 끝낸다.
        # 묶음 수가 최대이므로 전체 송금 수 = k - 묶음 수 = 최솟값.
        raw += _greedy_minor({m: minor[m] for m in group})
    return _as_transfers(raw, currency)


def settle(
    balances: Mapping[MemberId, Money],
    strategy: Strategy = Strategy.AUTO,
    max_members: int = DEFAULT_OPTIMAL_LIMIT,
) -> list[Transfer]:
    """정산 송금 목록. AUTO는 인원이 `max_members` 이하면 최적, 넘으면 그리디."""
    if strategy is Strategy.GREEDY:
        return greedy_transfers(balances)
    if strategy is Strategy.OPTIMAL:
        return optimal_transfers(balances, max_members)
    nonzero = sum(1 for m in balances.values() if not m.is_zero())
    if nonzero <= max_members:
        return optimal_transfers(balances, max_members)
    return greedy_transfers(balances)
