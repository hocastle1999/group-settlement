"""환율과 환산 (5단계).

원장은 그룹의 기준 통화 하나로만 기록한다. 외화 지출은 지출 시점의 환율로 기준 통화로 바꿔 기록하고,
원래 금액과 환율은 증빙으로 분개에 함께 남긴다(`ForeignAmount`).

환율의 뜻: **원래 통화 1단위 = 기준 통화 rate 단위** (예: JPY -> KRW 9.1234 이면 1엔 = 9.1234원).

반올림:
- 환산은 지출 총액에 **한 번만** 한다. 사람마다 따로 환산하면 반올림이 사람 수만큼 생겨
  합이 어긋난다. 나누기는 환산한 총액을 기존 최대 나머지 방식으로 한다.
- 방식은 ROUND_HALF_EVEN(은행가 반올림): 정확히 0.5일 때 짝수 쪽으로 보낸다. 늘 올리면(HALF_UP)
  지출이 쌓일수록 위로 치우친다. 파이썬 Decimal의 기본값이기도 하다.

정밀도: Decimal의 기본 문맥은 유효 숫자 28자리다. 금액(정수 15자리 + 소수 4자리)과
환율(정수 10자리 + 소수 10자리)을 곱하면 39자리까지 나와, 기본 문맥에서는 곱하는 순간
몰래 반올림된다. 넉넉한 문맥에서 곱한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from app.ledger.errors import CurrencyMismatchError, InvalidAmountError, InvalidExchangeRateError
from app.ledger.money import Currency, Money

# 환율은 소수 10자리까지 (DB: DECIMAL(20, 10)). 더 길면 자르지 않고 거부한다.
RATE_MAX_DECIMALS = 10
RATE_MAX_INTEGER_DIGITS = 10
# 기록할 수 있는 금액의 정수부 자릿수 (DB: DECIMAL(19, 4)의 정수부 15자리)
AMOUNT_MAX_INTEGER_DIGITS = 15


def check_rate(rate: Decimal) -> Decimal:
    if isinstance(rate, float) or not isinstance(rate, Decimal):
        raise InvalidExchangeRateError(f"환율은 Decimal이어야 한다: {rate!r}")
    if not rate.is_finite() or rate <= 0:
        raise InvalidExchangeRateError(f"환율은 양수여야 한다: {rate}")
    exponent = rate.normalize().as_tuple().exponent
    if isinstance(exponent, int) and -exponent > RATE_MAX_DECIMALS:
        raise InvalidExchangeRateError(f"환율은 소수 {RATE_MAX_DECIMALS}자리까지: {rate}")
    if rate >= Decimal(10) ** RATE_MAX_INTEGER_DIGITS:
        raise InvalidExchangeRateError(f"환율이 너무 크다: {rate}")
    return rate


def convert(amount: Money, rate: Decimal, to: Currency) -> Money:
    """`amount`를 `to` 통화로 환산한다. `to`의 최소 단위로 ROUND_HALF_EVEN 반올림."""
    check_rate(rate)
    if amount.currency == to:
        raise CurrencyMismatchError(f"같은 통화는 환산하지 않는다: {to.code}")
    with localcontext() as ctx:
        ctx.prec = 60  # 곱셈 중간값이 잘리지 않게 (모듈 설명 참고)
        value = (amount.amount * rate).quantize(to.minor_unit, rounding=ROUND_HALF_EVEN)
    if abs(value) >= Decimal(10) ** AMOUNT_MAX_INTEGER_DIGITS:
        raise InvalidAmountError(f"환산 금액이 너무 크다: {value} {to.code}")
    return Money(value, to)


@dataclass(frozen=True, slots=True)
class ForeignAmount:
    """외화 지출의 원래 금액과 환율. 분개의 증빙이다(잔액 계산에는 쓰지 않는다)."""

    amount: Money
    rate: Decimal

    def __post_init__(self) -> None:
        check_rate(self.rate)
        if not self.amount.is_positive():
            raise InvalidAmountError(f"원래 금액은 양수여야 한다: {self.amount}")

    def converted(self, to: Currency) -> Money:
        return convert(self.amount, self.rate, to)
