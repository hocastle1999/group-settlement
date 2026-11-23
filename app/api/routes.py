"""REST 엔드포인트.

핸들러는 일부러 `def`(동기)다. DB 드라이버(PyMySQL)가 블로킹이라 `async def` 안에서 부르면
이벤트 루프 전체가 멈춘다. `def` 핸들러는 FastAPI가 스레드 풀에서 실행한다.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response, status
from pydantic import BaseModel

from app.api import schemas as s
from app.api.auth import (
    ActorDep,
    CurrentGroup,
    EngineDep,
    SettingsDep,
    hash_token,
    new_invite_token,
)
from app.db import audit, report, store
from app.db.store import Group, PostResult, StoredEntry, member_key
from app.ledger import (
    Currency,
    EqualSplit,
    FixedSplit,
    InvalidExchangeRateError,
    InvalidSplitError,
    Money,
    RatioSplit,
    SplitRule,
    Strategy,
    convert,
    expense_entry,
    foreign_expense_entry,
    get_currency,
    transfer_entry,
)

router = APIRouter(prefix="/api")

# 멱등성 키: UUID 등. 영문, 숫자, -, _ 만 1 ~ 64자.
IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


# ---------------------------------------------------------------- 그룹, 멤버


@router.post("/groups", status_code=status.HTTP_201_CREATED, response_model=s.GroupCreated)
def create_group(body: s.GroupCreate, engine: EngineDep) -> s.GroupCreated:
    # 멱등성 키를 받지 않는다: 재시도로 빈 그룹이 하나 더 생겨도 해가 없고,
    # 토큰 원문을 저장하지 않으므로 "이전 응답 다시 주기"로 같은 토큰을 돌려줄 수도 없다.
    token = new_invite_token()
    group = store.create_group(engine, body.name, get_currency(body.currency), hash_token(token))
    return s.GroupCreated(
        id=group.id,
        name=group.name,
        currency=group.currency.code,
        members=[],
        invite_token=token,
    )


@router.get("/groups/{group_id}", response_model=s.GroupOut)
def get_group(group: CurrentGroup, engine: EngineDep) -> s.GroupOut:
    return _group_out(group, store.get_members(engine, group.id))


@router.post(
    "/groups/{group_id}/members",
    status_code=status.HTTP_201_CREATED,
    response_model=s.MemberOut,
)
def add_member(
    body: s.MemberCreate, group: CurrentGroup, engine: EngineDep, actor: ActorDep = None
) -> s.MemberOut:
    # 멱등성 키 대신 자연 키(그룹 안에서 이름이 유일)로 중복을 막는다. 재시도하면 409.
    member = store.add_member(engine, group.id, body.name, actor)
    return s.MemberOut(id=member.id, name=member.name)


# ---------------------------------------------------------------- 분개 쓰기


IdempotencyKeyHeader = Annotated[str | None, Header(alias="Idempotency-Key")]


@router.post(
    "/groups/{group_id}/expenses",
    status_code=status.HTTP_201_CREATED,
    response_model=s.EntryOut,
)
def record_expense(
    body: s.ExpenseCreate,
    group: CurrentGroup,
    engine: EngineDep,
    request: Request,
    response: Response,
    idempotency_key: IdempotencyKeyHeader = None,
    actor: ActorDep = None,
) -> s.EntryOut:
    key = _require_key(idempotency_key)
    # 금액, 환율, 분할 규칙은 그룹 잠금을 잡기 전에 만들어 검사한다(잘못된 요청이 잠금을 잡지 않게).
    # 잠금 안에서는 순번(seq)이 필요한 일만 한다: 남는 단위를 받을 출발점을 순번만큼 돌린다.
    currency = get_currency(body.currency) if body.currency else group.currency
    amount = Money.of(body.amount, currency)
    split = _split_rule(body.split, currency)
    payer = member_key(body.payer_id)

    if currency == group.currency:
        if body.fx_rate is not None:
            raise InvalidExchangeRateError("그룹 기준 통화 지출에는 fx_rate를 보내지 않는다")

        def build(_conn, seq: int):
            return expense_entry(payer, amount, split, body.memo, rotation=seq)
    else:
        if body.fx_rate is None:
            raise InvalidExchangeRateError(
                f"{currency.code} 지출은 fx_rate(1 {currency.code}당 {group.currency.code})"
                "가 필요하다"
            )
        rate = Decimal(body.fx_rate)
        convert(amount, rate, group.currency)  # 환율 범위, 환산 금액 크기를 잠금 전에 검사

        def build(_conn, seq: int):
            return foreign_expense_entry(
                payer, amount, rate, group.currency, split, body.memo, rotation=seq
            )

    result = store.post_entry(
        engine, group, build, key, _request_hash(request, body), actor_id=actor
    )
    return _entry_response(result, response)


@router.post(
    "/groups/{group_id}/transfers",
    status_code=status.HTTP_201_CREATED,
    response_model=s.EntryOut,
)
def record_transfer(
    body: s.TransferCreate,
    group: CurrentGroup,
    engine: EngineDep,
    request: Request,
    response: Response,
    idempotency_key: IdempotencyKeyHeader = None,
    actor: ActorDep = None,
) -> s.EntryOut:
    key = _require_key(idempotency_key)

    def build(_conn, _seq):
        return transfer_entry(
            member_key(body.sender_id),
            member_key(body.receiver_id),
            Money.of(body.amount, group.currency),
            body.memo,
        )

    result = store.post_entry(
        engine, group, build, key, _request_hash(request, body), actor_id=actor
    )
    return _entry_response(result, response)


@router.post(
    "/groups/{group_id}/entries/{entry_id}/reversal",
    status_code=status.HTTP_201_CREATED,
    response_model=s.EntryOut,
)
def reverse_entry(
    entry_id: int,
    group: CurrentGroup,
    engine: EngineDep,
    request: Request,
    response: Response,
    body: s.ReversalCreate | None = None,
    idempotency_key: IdempotencyKeyHeader = None,
    actor: ActorDep = None,
) -> s.EntryOut:
    key = _require_key(idempotency_key)
    body = body or s.ReversalCreate()
    build = store.reversal_builder(group, entry_id, body.memo)
    result = store.post_entry(
        engine, group, build, key, _request_hash(request, body), actor_id=actor
    )
    return _entry_response(result, response)


@router.post(
    "/groups/{group_id}/settlements/{settlement_id}/transfers/{line_no}/complete",
    status_code=status.HTTP_201_CREATED,
    response_model=s.EntryOut,
)
def complete_transfer(
    settlement_id: int,
    line_no: int,
    group: CurrentGroup,
    engine: EngineDep,
    request: Request,
    response: Response,
    body: s.CompletionCreate | None = None,
    idempotency_key: IdempotencyKeyHeader = None,
    actor: ActorDep = None,
) -> s.EntryOut:
    """확정 정산의 송금 한 줄을 "보냈다"로 기록한다 = 그 금액의 송금 분개를 남긴다.

    잘못 눌렀으면 응답의 분개를 취소(reversal)한다. 그러면 그 줄은 다시 미완료가 된다.
    """
    key = _require_key(idempotency_key)
    body = body or s.CompletionCreate()
    build = store.completion_builder(group, settlement_id, line_no, body.memo)
    result = store.post_entry(
        engine,
        group,
        build,
        key,
        _request_hash(request, body),
        link=(settlement_id, line_no),
        actor_id=actor,
    )
    return _entry_response(result, response)


# ---------------------------------------------------------------- 조회


@router.get("/groups/{group_id}/entries", response_model=s.EntryPage)
def list_entries(
    group: CurrentGroup,
    engine: EngineDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    before_id: Annotated[int | None, Query(ge=1)] = None,
) -> s.EntryPage:
    entries = store.list_entries(engine, group.id, limit, before_id)
    next_before = entries[-1].id if len(entries) == limit else None
    return s.EntryPage(entries=[_entry_out(e) for e in entries], next_before_id=next_before)


@router.get("/groups/{group_id}/balances", response_model=s.BalancesOut)
def get_balances(group: CurrentGroup, engine: EngineDep) -> s.BalancesOut:
    names = {m.id: m.name for m in store.get_members(engine, group.id)}
    balances = store.balances(engine, group)
    return s.BalancesOut(
        currency=group.currency.code,
        balances=[
            s.BalanceOut(member_id=m, name=names[m], amount=str(v.amount))
            for m, v in balances.items()
            if m in names
        ],
    )


@router.get("/groups/{group_id}/settlement", response_model=s.SettlementOut)
def get_settlement(
    group: CurrentGroup,
    engine: EngineDep,
    settings: SettingsDep,
    strategy: Strategy = Strategy.AUTO,
) -> s.SettlementOut:
    """지금 잔액을 0으로 만드는 송금 목록. 계산만 하고 기록하지 않는다 (확정은 POST /settlements).

    버전과 잔액을 같은 스냅숏에서 읽으므로 응답의 version은 이 계획과 정확히 짝이 맞는다.
    """
    snap = store.snapshot(engine, group)
    return s.SettlementOut(
        currency=group.currency.code,
        strategy=strategy,
        version=snap.version,
        transfers=_transfers_out(
            store.plan_settlement(snap.balances, strategy, settings.settle_optimal_limit)
        ),
    )


# ---------------------------------------------------------------- 정산 확정


@router.post(
    "/groups/{group_id}/settlements",
    status_code=status.HTTP_201_CREATED,
    response_model=s.ConfirmedSettlementOut,
)
def confirm_settlement(
    body: s.SettlementConfirm,
    group: CurrentGroup,
    engine: EngineDep,
    settings: SettingsDep,
    response: Response,
    actor: ActorDep = None,
) -> s.ConfirmedSettlementOut:
    """GET /settlement에서 받은 version으로 확정한다. 그사이 분개가 더 기록됐으면 409.

    멱등성 키를 받지 않는다: (그룹, 버전)이 유일하므로 같은 버전으로 다시 보내면
    처음 확정을 200으로 돌려준다(자연 키).
    """
    settlement, created = store.confirm_settlement(
        engine, group, body.expected_version, body.strategy, settings.settle_optimal_limit, actor
    )
    if not created:
        response.status_code = status.HTTP_200_OK
        response.headers["Idempotent-Replayed"] = "true"
    return _confirmed_out(group, settlement, store.current_version(engine, group))


@router.get("/groups/{group_id}/settlements", response_model=s.ConfirmedSettlementList)
def list_settlements(
    group: CurrentGroup,
    engine: EngineDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> s.ConfirmedSettlementList:
    current = store.current_version(engine, group)
    items = store.list_settlements(engine, group, limit)
    return s.ConfirmedSettlementList(settlements=[_confirmed_out(group, x, current) for x in items])


@router.get(
    "/groups/{group_id}/settlements/{settlement_id}", response_model=s.ConfirmedSettlementOut
)
def get_confirmed_settlement(
    settlement_id: int, group: CurrentGroup, engine: EngineDep
) -> s.ConfirmedSettlementOut:
    settlement = store.get_settlement(engine, group, settlement_id)
    return _confirmed_out(group, settlement, store.current_version(engine, group))


# ---------------------------------------------------------------- 리포트


@router.get("/groups/{group_id}/report", response_model=s.ReportOut)
def get_report(
    group: CurrentGroup,
    engine: EngineDep,
    tz_offset: Annotated[s.TzOffset, Query()] = "+00:00",
) -> s.ReportOut:
    """멤버별 요약, 날짜별 지출과 누적, 통화별 지출. 세 쿼리를 같은 스냅숏에서 읽는다."""
    r = report.build_report(engine, group, tz_offset)
    return s.ReportOut(
        version=r.version,
        currency=group.currency.code,
        tz_offset=tz_offset,
        members=[
            s.MemberSummaryOut(
                member_id=m.member_id,
                name=m.name,
                paid=str(m.paid.amount),
                share=str(m.share.amount),
                sent=str(m.sent.amount),
                received=str(m.received.amount),
                balance=str(m.balance.amount),
            )
            for m in r.members
        ],
        daily=[
            s.DailySpendingOut(
                day=d.day,
                expenses=d.expenses,
                total=str(d.total.amount),
                cumulative=str(d.cumulative.amount),
            )
            for d in r.daily
        ],
        by_currency=[
            s.CurrencySpendingOut(
                currency=c.currency,
                expenses=c.expenses,
                original_total=str(c.original_total.amount),
                converted_total=str(c.converted_total.amount),
            )
            for c in r.by_currency
        ],
    )


# ---------------------------------------------------------------- 감사 로그


@router.get("/groups/{group_id}/audit", response_model=s.AuditPage)
def list_audit(
    group: CurrentGroup,
    engine: EngineDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before_seq: Annotated[int | None, Query(ge=1)] = None,
) -> s.AuditPage:
    events = audit.list_events(engine, group.id, limit, before_seq)
    return s.AuditPage(
        events=[
            s.AuditEventOut(
                seq=e.seq,
                action=e.action,
                target_id=e.target_id,
                actor_member_id=e.actor_member_id,
                detail=e.detail,
                created_at=e.created_at,
                hash=e.hash.hex(),
            )
            for e in events
        ],
        next_before_seq=events[-1].seq if len(events) == limit else None,
    )


@router.get("/groups/{group_id}/audit/verify", response_model=s.AuditVerifyOut)
def verify_audit(group: CurrentGroup, engine: EngineDep) -> s.AuditVerifyOut:
    """해시 체인을 처음부터 다시 계산해 고쳐지거나 지워진 행이 있는지 본다."""
    result = audit.verify(engine, group.id)
    return s.AuditVerifyOut(
        valid=result.valid,
        events=result.events,
        broken_at_seq=result.broken_at_seq,
        reason=result.reason,
    )


# ---------------------------------------------------------------- 도우미


def _transfers_out(transfers: list[store.SettlementTransfer]) -> list[s.TransferOut]:
    return [
        s.TransferOut(sender_id=t.sender_id, receiver_id=t.receiver_id, amount=str(t.amount.amount))
        for t in transfers
    ]


def _confirmed_out(
    group: Group, settlement: store.StoredSettlement, current: int
) -> s.ConfirmedSettlementOut:
    return s.ConfirmedSettlementOut(
        id=settlement.id,
        version=settlement.version,
        current_version=current,
        currency=group.currency.code,
        strategy=settlement.strategy,
        created_at=settlement.created_at,
        transfers=[
            s.ConfirmedTransferOut(
                line_no=no,
                sender_id=t.sender_id,
                receiver_id=t.receiver_id,
                amount=str(t.amount.amount),
                completed_entry_id=settlement.completions.get(no),
            )
            for no, t in enumerate(settlement.transfers, start=1)
        ],
        completed=settlement.completed,
    )


def _group_out(group: Group, members: list[store.Member]) -> s.GroupOut:
    return s.GroupOut(
        id=group.id,
        name=group.name,
        currency=group.currency.code,
        members=[s.MemberOut(id=m.id, name=m.name) for m in members],
    )


def _require_key(key: str | None) -> str:
    """돈이 오가는 쓰기는 멱등성 키가 필수다. 네트워크가 끊겨 응답을 못 받은 클라이언트가
    다시 보내도 같은 지출이 두 번 기록되지 않아야 하기 때문이다."""
    if key is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Idempotency-Key 헤더가 필요하다")
    if not IDEMPOTENCY_KEY.fullmatch(key):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key는 영문, 숫자, -, _ 1 ~ 64자여야 한다",
        )
    return key


def _request_hash(request: Request, body: BaseModel) -> bytes:
    """같은 키로 "같은 요청"이 왔는지 판단할 지문. 경로(어느 그룹의 어느 작업) + 검증된 본문.

    원본 바이트가 아니라 검증된 모델을 정렬된 JSON으로 만들어 해시한다.
    필드 순서나 공백만 다른 재시도는 같은 요청으로 본다.
    """
    canonical = json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return store.hash_bytes(f"{request.method} {request.url.path}\n{canonical}".encode())


def _split_rule(
    split: s.EqualSplitIn | s.RatioSplitIn | s.FixedSplitIn, currency: Currency
) -> SplitRule:
    """요청의 분할을 코어 SplitRule로. 검증(중복, 합계, 가중치)은 코어가 한다.

    정액 분할 금액은 지출 통화(`currency`)다. 외화 지출이면 코어가 환산 총액을 그 비율로 나눈다.
    """
    if isinstance(split, s.EqualSplitIn):
        return EqualSplit([member_key(m) for m in split.member_ids])
    if isinstance(split, s.RatioSplitIn):
        ids = [w.member_id for w in split.weights]
        _no_duplicates(ids)
        return RatioSplit({member_key(w.member_id): w.weight for w in split.weights})
    ids = [a.member_id for a in split.amounts]
    _no_duplicates(ids)
    return FixedSplit(
        {member_key(a.member_id): Money.of(a.amount, currency) for a in split.amounts}
    )


def _no_duplicates(ids: list[int]) -> None:
    # 목록을 dict로 바꾸면 중복이 조용히 덮어써진다. 그 전에 거부한다.
    if len(set(ids)) != len(ids):
        raise InvalidSplitError("참여자가 중복됐다")


def _entry_response(result: PostResult, response: Response) -> s.EntryOut:
    if result.replayed:
        # 재시도에 처음과 같은 응답을 준다. 새로 기록하지 않았다는 표시만 헤더로 붙인다.
        response.headers["Idempotent-Replayed"] = "true"
    return _entry_out(result.entry)


def _entry_out(entry: StoredEntry) -> s.EntryOut:
    return s.EntryOut(
        id=entry.id,
        seq=entry.seq,
        kind=entry.kind,
        memo=entry.memo,
        reverses_entry_id=entry.reverses_entry_id,
        reversed_by_entry_id=entry.reversed_by_entry_id,
        original=(
            None
            if entry.original is None
            else s.OriginalOut(
                currency=entry.original.amount.currency.code,
                amount=str(entry.original.amount.amount),
                fx_rate=str(entry.original.rate.normalize()),
            )
        ),
        settlement_id=entry.settlement_link[0] if entry.settlement_link else None,
        settlement_line_no=entry.settlement_link[1] if entry.settlement_link else None,
        created_at=entry.created_at,
        lines=[
            s.LineOut(member_id=ln.member_id, side=ln.side, amount=str(ln.amount.amount))
            for ln in entry.lines
        ],
    )
