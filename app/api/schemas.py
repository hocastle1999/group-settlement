"""요청과 응답 모델 (Pydantic).

금액은 JSON에서 **문자열**로만 주고받는다 ("12.34"). JSON 숫자는 대부분의 파서가 double(float)로
읽으므로 서버에 오기 전에 이미 오차가 들어갈 수 있다. 숫자로 보내면 422로 거부한다.
요청 모델은 모르는 필드를 거부한다(`extra="forbid"`): 오타 난 필드가 조용히 무시되지 않게.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.ledger import CURRENCIES, EntryKind, Side, Strategy

# 부호 없는 십진수 문자열. 소수 자릿수(통화 단위) 검사는 Money가 한다 (1.5원 -> 422).
AmountStr = Annotated[str, StringConstraints(pattern=r"^\d{1,15}(\.\d{1,4})?$")]
# 환율: 원래 통화 1단위당 기준 통화 단위. 정수 10자리, 소수 10자리까지 (그 이상은 자르지 않고 거부).
RateStr = Annotated[str, StringConstraints(pattern=r"^\d{1,10}(\.\d{1,10})?$")]
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
    amount: AmountStr  # currency 통화의 금액. 정액 분할 금액도 같은 통화
    split: SplitIn
    memo: Memo = ""
    # 지출 통화. 없으면 그룹 기준 통화. 다르면 fx_rate가 필요하고, 같으면 fx_rate를 보내지 않는다.
    currency: CurrencyCode | None = None  # type: ignore[valid-type]
    fx_rate: RateStr | None = None


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


class OriginalOut(BaseModel):
    """외화 지출의 원래 금액과 환율. 줄(lines)은 그룹 기준 통화로 환산된 금액이다."""

    currency: str
    amount: str
    fx_rate: str


class EntryOut(BaseModel):
    id: int
    seq: int  # 그룹 안 순번 (1, 2, 3, ...)
    kind: EntryKind
    memo: str
    reverses_entry_id: int | None
    reversed_by_entry_id: int | None
    original: OriginalOut | None
    # 확정 정산의 송금을 완료한 기록이면 (정산 id, 줄 번호). 아니면 null.
    settlement_id: int | None
    settlement_line_no: int | None
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
    # 이 계획을 계산한 원장 버전(그 시점의 마지막 분개 순번). 확정할 때 expected_version으로 보낸다.
    version: int
    transfers: list[TransferOut]


class SettlementConfirm(Request):
    expected_version: int = Field(ge=0)
    strategy: Strategy = Strategy.AUTO


class ConfirmedTransferOut(TransferOut):
    line_no: int  # 완료 처리할 때 쓰는 줄 번호 (1부터)
    # 이 줄을 완료한 송금 분개 id. 아직 안 보냈거나 완료를 취소했으면 null.
    completed_entry_id: int | None


class CompletionCreate(Request):
    memo: Memo = ""


class ConfirmedSettlementOut(BaseModel):
    id: int
    version: int
    # 지금 원장 버전. version보다 크면 확정 뒤에 분개가 더 기록됐다는 뜻이다.
    current_version: int
    currency: str
    strategy: Strategy
    created_at: datetime
    transfers: list[ConfirmedTransferOut]
    # 모든 송금 줄이 완료됐는지 (송금이 하나도 없는 정산은 처음부터 완료)
    completed: bool


class AuditEventOut(BaseModel):
    seq: int
    action: str
    target_id: int | None
    actor_member_id: int | None  # 요청자가 밝힌 멤버 (인증 아님)
    detail: dict[str, Any]
    created_at: datetime
    hash: str  # 16진수. 직전 행 hash + 이 행 내용의 SHA-256


class AuditPage(BaseModel):
    events: list[AuditEventOut]
    next_before_seq: int | None


class AuditVerifyOut(BaseModel):
    valid: bool
    events: int
    broken_at_seq: int | None
    reason: str | None


class ConfirmedSettlementList(BaseModel):
    settlements: list[ConfirmedSettlementOut]
