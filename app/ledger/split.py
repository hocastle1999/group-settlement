"""지출을 참여자에게 나누는 규칙.

핵심 문제: 10,000원을 3명이 나누면 3,333.33...원이 되는데 원은 소수가 없다.
반올림을 각자 하면 3,333 x 3 = 9,999원으로 1원이 사라진다.
그래서 최소 단위 정수로 내림해 나눈 뒤, 남은 단위(여기서는 1원)를 정해진 규칙으로 한 명씩 더 준다.
나눈 금액의 합은 언제나 원래 금액과 정확히 같다 (테스트의 핵심 불변식).

배분 방식은 최대 나머지 방식(largest remainder, Hamilton 방식)이다.
- 각자의 몫 = 전체 x 가중치 / 가중치 합 을 내림한다.
- 남은 단위는 내림으로 잃은 소수 부분이 큰 사람부터 1단위씩 준다.
- 소수 부분이 같으면 멤버 ID 순서로 준다. 입력 순서와 상관없이 결과가 같아야 하기 때문이다(결정성).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field

from app.ledger.errors import InvalidSplitError
from app.ledger.money import Money, sum_money

MemberId = str


def allocate(total: Money, weights: Mapping[MemberId, int]) -> dict[MemberId, Money]:
    """`total`을 정수 가중치 비율로 나눈다. 결과의 합은 정확히 `total`이다.

    가중치가 0인 멤버도 결과에 0으로 들어간다.
    """
    if not weights:
        raise InvalidSplitError("나눌 대상이 없다")
    for member, w in weights.items():
        # bool은 int의 하위 타입이라 따로 막는다 (True가 가중치 1로 들어가는 실수 방지).
        if isinstance(w, bool) or not isinstance(w, int):
            raise InvalidSplitError(f"가중치는 정수여야 한다: {member}={w!r}")
        if w < 0:
            raise InvalidSplitError(f"가중치는 음수일 수 없다: {member}={w}")
    weight_sum = sum(weights.values())
    if weight_sum == 0:
        raise InvalidSplitError("가중치 합이 0이다")

    # 음수 금액(환불 등)은 절댓값으로 나눈 뒤 부호를 되돌린다. 나머지 배분 규칙을 하나로 유지하려고.
    sign = -1 if total.is_negative() else 1
    units = abs(total.minor_units)

    members = sorted(weights)  # 결정성: 입력 순서가 아니라 ID 순서
    base: dict[MemberId, int] = {}
    remainders: dict[MemberId, int] = {}
    for m in members:
        # 정수 나눗셈만 쓴다. 소수 부분은 나머지(remainder)로 비교하므로 오차가 없다.
        q, r = divmod(units * weights[m], weight_sum)
        base[m] = q
        remainders[m] = r

    leftover = units - sum(base.values())
    # 나머지가 큰 순서, 같으면 ID 순서. sorted는 안정 정렬이고 members가 이미 ID 순이다.
    order = sorted(members, key=lambda m: -remainders[m])
    for m in order[:leftover]:
        base[m] += 1

    return {m: Money.from_minor(sign * base[m], total.currency) for m in members}


class SplitRule(ABC):
    """지출 하나를 누가 얼마나 부담하는지 정하는 규칙."""

    @abstractmethod
    def shares(self, total: Money) -> dict[MemberId, Money]:
        """멤버별 부담액. 합은 반드시 `total`과 같다."""

    @abstractmethod
    def members(self) -> frozenset[MemberId]:
        """이 규칙에 등장하는 멤버."""


@dataclass(frozen=True)
class EqualSplit(SplitRule):
    """균등 분할. 남는 단위는 멤버 ID 순서로 한 명씩 더 낸다."""

    participants: frozenset[MemberId]

    def __init__(self, participants: frozenset[MemberId] | set[MemberId] | list[MemberId]):
        unique = frozenset(participants)
        if len(unique) != len(list(participants)):
            raise InvalidSplitError("참여자가 중복됐다")
        if not unique:
            raise InvalidSplitError("참여자가 없다")
        object.__setattr__(self, "participants", unique)

    def shares(self, total: Money) -> dict[MemberId, Money]:
        return allocate(total, {m: 1 for m in self.participants})

    def members(self) -> frozenset[MemberId]:
        return self.participants


@dataclass(frozen=True)
class RatioSplit(SplitRule):
    """정수 비율 분할. 예: {"a": 2, "b": 1, "c": 1} -> 50%, 25%, 25%.

    퍼센트는 {"a": 50, "b": 30, "c": 20}처럼 정수로 준다. 가중치 0인 멤버는 부담하지 않는다.
    """

    weights: Mapping[MemberId, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # 지출을 기록할 때가 아니라 규칙을 만들 때 실패하도록 allocate와 같은 검증을 미리 한다.
        # 밖에서 넘긴 dict를 나중에 바꿔도 규칙이 바뀌지 않게 복사해 둔다.
        weights = dict(self.weights)
        object.__setattr__(self, "weights", weights)
        if not weights:
            raise InvalidSplitError("참여자가 없다")
        for member, w in weights.items():
            if isinstance(w, bool) or not isinstance(w, int) or w < 0:
                raise InvalidSplitError(f"가중치는 0 이상 정수여야 한다: {member}={w!r}")
        if sum(weights.values()) == 0:
            raise InvalidSplitError("가중치 합이 0이다")

    def shares(self, total: Money) -> dict[MemberId, Money]:
        return allocate(total, self.weights)

    def members(self) -> frozenset[MemberId]:
        return frozenset(self.weights)


@dataclass(frozen=True)
class FixedSplit(SplitRule):
    """정액 분할. 각자 낼 금액을 직접 정한다. 합이 지출 총액과 정확히 같아야 한다.

    맞지 않을 때 차액을 누군가에게 몰래 얹지 않고 거부한다.
    사용자가 입력한 금액이 기록의 근거이기 때문이다.
    """

    amounts: Mapping[MemberId, Money] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.amounts:
            raise InvalidSplitError("참여자가 없다")
        object.__setattr__(self, "amounts", dict(self.amounts))
        for member, amount in self.amounts.items():
            if amount.is_negative():
                raise InvalidSplitError(f"부담액은 음수일 수 없다: {member}={amount}")

    def shares(self, total: Money) -> dict[MemberId, Money]:
        for member, amount in self.amounts.items():
            if amount.currency != total.currency:
                raise InvalidSplitError(f"통화가 다르다: {member}={amount}, 총액 {total}")
        actual = sum_money(list(self.amounts.values()), total.currency)
        if actual != total:
            raise InvalidSplitError(f"정액 합계 {actual}가 총액 {total}과 다르다")
        return {m: self.amounts[m] for m in sorted(self.amounts)}

    def members(self) -> frozenset[MemberId]:
        return frozenset(self.amounts)
