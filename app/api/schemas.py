"""요청과 응답 모델 (Pydantic).

금액은 JSON에서 **문자열**로만 주고받는다 ("12.34"). JSON 숫자는 대부분의 파서가 double(float)로
읽으므로 서버에 오기 전에 이미 오차가 들어갈 수 있다. 숫자로 보내면 422로 거부한다.
요청 모델은 모르는 필드를 거부한다(`extra="forbid"`): 오타 난 필드가 조용히 무시되지 않게.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.ledger import CURRENCIES, EntryKind, Side, Strategy

# 부호 없는 십진수 문자열. 소수 자릿수(통화 단위) 검사는 Money가 한다 (1.5원 -> 422).
AmountStr = Annotated[str, StringConstraints(pattern=r"^\d{1,15}(\.\d{1,4})?$")]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]
Memo = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]
CurrencyCode = Literal[tuple(sorted(CURRENCIES))]  # type: ignore[valid-type]


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- 그룹, 멤버


class GroupCreate(Request):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    currency: CurrencyCode  # type: ignore[valid-type]


class MemberCreate(Request):
    name: Name


class MemberOut(BaseModel):
    id: int
    name: str


class GroupOut(BaseModel):
    id: int
    name: str
    currency: str
    members: list[MemberOut]


class GroupCreated(GroupOut):
    # 이 응답에서만 볼 수 있다. 서버는 해시만 저장하므로 다시 알려 줄 수 없다.
    invite_token: str


# ---------------------------------------------------------------- 분개 요청


class EqualSplitIn(Request):
    type: Literal["equal"]
    member_ids: list[int] = Field(min_length=1)


class WeightIn(Request):
    member_id: int
    weight: int = Field(ge=0)


class RatioSplitIn(Request):
    type: Literal["ratio"]
    weights: list[WeightIn] = Field(min_length=1)


class FixedShareIn(Request):
    member_id: int
    amount: AmountStr


class FixedSplitIn(Request):
    type: Literal["fixed"]
    amounts: list[FixedShareIn] = Field(min_length=1)


# 판별 유니온: "type" 값을 보고 어느 모델로 검증할지 바로 정한다 (모든 모델을 차례로 시도하지 않음).
SplitIn = Annotated[EqualSplitIn | RatioSplitIn | FixedSplitIn, Field(discriminator="type")]


class ExpenseCreate(Request):
    payer_id: int
    amount: AmountStr
    split: SplitIn
    memo: Memo = ""


class TransferCreate(Request):
    sender_id: int
    receiver_id: int
    amount: AmountStr
    memo: Memo = ""


class ReversalCreate(Request):
    memo: Memo = ""


# ---------------------------------------------------------------- 분개, 잔액, 정산 응답


class LineOut(BaseModel):
    member_id: int
    side: Side
    amount: str


class EntryOut(BaseModel):
    id: int
    kind: EntryKind
    memo: str
    reverses_entry_id: int | None
    reversed_by_entry_id: int | None
    created_at: datetime
    lines: list[LineOut]


class EntryPage(BaseModel):
    entries: list[EntryOut]
    # 다음 쪽을 읽으려면 이 값을 before_id로 넘긴다. 더 없으면 null.
    next_before_id: int | None


class BalanceOut(BaseModel):
    member_id: int
    name: str
    amount: str  # 양수: 돌려받을 돈, 음수: 내야 할 돈


class BalancesOut(BaseModel):
    currency: str
    balances: list[BalanceOut]


class TransferOut(BaseModel):
    sender_id: int
    receiver_id: int
    amount: str


class SettlementOut(BaseModel):
    currency: str
    strategy: Strategy
    transfers: list[TransferOut]
