"""금액 타입.

돈은 float로 다루지 않는다. 0.1 + 0.2 != 0.3 같은 이진 부동소수 오차가
정산 결과를 1원씩 틀리게 만든다.
`Money`는 `Decimal` 금액과 통화를 함께 들고, 통화의 소수 자릿수(원 0자리, 달러 2자리)보다
더 작은 단위를 허용하지 않는다. 반올림이 필요한 계산(나누기)은 여기서 하지 않고
`split.py`가 최소 단위 정수로 나눈 뒤 나머지를 명시적인 규칙으로 배분한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.ledger.errors import CurrencyMismatchError, InvalidAmountError


@dataclass(frozen=True, slots=True)
class Currency:
    """통화. `exponent`는 최소 단위의 소수 자릿수 (ISO 4217의 minor unit)."""

    code: str
    exponent: int

    @property
    def minor_unit(self) -> Decimal:
        """최소 단위 금액. KRW는 1, USD는 0.01."""
        return Decimal(1).scaleb(-self.exponent)


# 지원 통화 (ISO 4217 minor unit). 5단계에서 늘렸다. 바꾸면 DB의 통화 CHECK도 마이그레이션해야 한다.
CURRENCIES: dict[str, Currency] = {
    c.code: c
    for c in (
        Currency("KRW", 0),
        Currency("JPY", 0),
        Currency("VND", 0),
        Currency("USD", 2),
        Currency("EUR", 2),
        Currency("GBP", 2),
        Currency("CNY", 2),
        Currency("THB", 2),
        Currency("KWD", 3),  # 소수 3자리 통화 (1 디나르 = 1000 필스)
    )
}


def get_currency(code: str) -> Currency:
    try:
        return CURRENCIES[code]
    except KeyError:
        raise InvalidAmountError(f"지원하지 않는 통화: {code!r}") from None


@dataclass(frozen=True, slots=True)
class Money:
    """통화가 붙은 금액. 불변 값 객체.

    직접 생성보다 `Money.of("12.34", "USD")`나 `Money.from_minor(1234, usd)`를 쓴다.
    """

    amount: Decimal
    currency: Currency

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal):
            raise InvalidAmountError(f"금액은 Decimal이어야 한다: {self.amount!r}")
        if not self.amount.is_finite():
            raise InvalidAmountError(f"유한한 금액이 아니다: {self.amount}")
        # 통화 단위보다 잘게 쪼갠 금액은 받지 않는다 (예: 1.5원, 0.001달러).
        try:
            quantized = self.amount.quantize(self.currency.minor_unit)
        except InvalidOperation:  # Decimal 정밀도(28자리)를 넘는 거대한 값
            raise InvalidAmountError(f"금액이 너무 크다: {self.amount}") from None
        if self.amount != quantized:
            raise InvalidAmountError(
                f"{self.currency.code}는 소수 {self.currency.exponent}자리까지만 쓴다: "
                f"{self.amount}"
            )
        # 표현을 통일한다: Decimal("1000") 과 Decimal("1E+3") 이 같은 모양으로 보이게.
        object.__setattr__(self, "amount", quantized)

    # --- 생성 ---

    @classmethod
    def of(cls, amount: str | int | Decimal, currency: str | Currency) -> Money:
        """문자열, 정수, Decimal에서 만든다. float는 이미 오차가 들어 있으므로 거부한다."""
        if isinstance(amount, float) or isinstance(amount, bool):
            raise InvalidAmountError(f"float/bool 금액은 받지 않는다: {amount!r}")
        cur = currency if isinstance(currency, Currency) else get_currency(currency)
        try:
            value = Decimal(amount)
        except (InvalidOperation, ValueError):
            raise InvalidAmountError(f"금액 형식이 아니다: {amount!r}") from None
        return cls(value, cur)

    @classmethod
    def from_minor(cls, minor: int, currency: str | Currency) -> Money:
        """최소 단위 정수(센트, 원)에서 만든다."""
        cur = currency if isinstance(currency, Currency) else get_currency(currency)
        return cls(Decimal(minor).scaleb(-cur.exponent), cur)

    @classmethod
    def zero(cls, currency: str | Currency) -> Money:
        return cls.from_minor(0, currency)

    # --- 조회 ---

    @property
    def minor_units(self) -> int:
        """최소 단위 정수. 12.34 USD -> 1234."""
        return int(self.amount.scaleb(self.currency.exponent))

    def is_zero(self) -> bool:
        return self.amount == 0

    def is_positive(self) -> bool:
        return self.amount > 0

    def is_negative(self) -> bool:
        return self.amount < 0

    # --- 연산 (같은 통화끼리만) ---

    def _check_same(self, other: Money) -> None:
        if not isinstance(other, Money):
            raise TypeError(f"Money끼리만 계산한다: {other!r}")
        if other.currency != self.currency:
            raise CurrencyMismatchError(
                f"통화가 다르다: {self.currency.code} vs {other.currency.code}"
            )

    def __add__(self, other: Money) -> Money:
        self._check_same(other)
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._check_same(other)
        return Money(self.amount - other.amount, self.currency)

    def __neg__(self) -> Money:
        return Money(-self.amount, self.currency)

    def __abs__(self) -> Money:
        return Money(abs(self.amount), self.currency)

    def __lt__(self, other: Money) -> bool:
        self._check_same(other)
        return self.amount < other.amount

    def __le__(self, other: Money) -> bool:
        self._check_same(other)
        return self.amount <= other.amount

    def __gt__(self, other: Money) -> bool:
        self._check_same(other)
        return self.amount > other.amount

    def __ge__(self, other: Money) -> bool:
        self._check_same(other)
        return self.amount >= other.amount

    def __str__(self) -> str:
        return f"{self.amount} {self.currency.code}"


def sum_money(items: list[Money] | tuple[Money, ...], currency: Currency) -> Money:
    """같은 통화 금액의 합. 비어 있으면 0. (내장 sum은 시작값 0(int) 때문에 못 쓴다)"""
    total = Money.zero(currency)
    for m in items:
        total = total + m
    return total
