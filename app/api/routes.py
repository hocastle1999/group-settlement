"""REST 엔드포인트.

핸들러는 일부러 `def`(동기)다. DB 드라이버(PyMySQL)가 블로킹이라 `async def` 안에서 부르면
이벤트 루프 전체가 멈춘다. `def` 핸들러는 FastAPI가 스레드 풀에서 실행한다.
"""

from __future__ import annotations

import json
import re
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response, status
from pydantic import BaseModel

from app.api import schemas as s
from app.api.auth import CurrentGroup, EngineDep, hash_token, new_invite_token
from app.db import store
from app.db.store import Group, PostResult, StoredEntry, member_key
from app.ledger import (
    EqualSplit,
    FixedSplit,
    InvalidSplitError,
    Money,
    RatioSplit,
    SplitRule,
    Strategy,
    expense_entry,
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
def add_member(body: s.MemberCreate, group: CurrentGroup, engine: EngineDep) -> s.MemberOut:
    # 멱등성 키 대신 자연 키(그룹 안에서 이름이 유일)로 중복을 막는다. 재시도하면 409.
    member = store.add_member(engine, group.id, body.name)
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
) -> s.EntryOut:
    key = _require_key(idempotency_key)
    currency = group.currency

    def build(_conn, seq: int):
        total = Money.of(body.amount, currency)
        split = _split_rule(body.split, group)
        # 남는 단위를 받을 출발점을 분개 순번만큼 돌린다 (한 사람에게 몰리지 않게)
        return expense_entry(member_key(body.payer_id), total, split, body.memo, rotation=seq)

    result = store.post_entry(engine, group, build, key, _request_hash(request, body))
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
) -> s.EntryOut:
    key = _require_key(idempotency_key)

    def build(_conn, _seq):
        return transfer_entry(
            member_key(body.sender_id),
            member_key(body.receiver_id),
            Money.of(body.amount, group.currency),
            body.memo,
        )

    result = store.post_entry(engine, group, build, key, _request_hash(request, body))
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
) -> s.EntryOut:
    key = _require_key(idempotency_key)
    body = body or s.ReversalCreate()
    build = store.reversal_builder(group, entry_id, body.memo)
    result = store.post_entry(engine, group, build, key, _request_hash(request, body))
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
        transfers=_transfers_out(store.plan_settlement(snap.balances, strategy)),
    )


# ---------------------------------------------------------------- 정산 확정


@router.post(
    "/groups/{group_id}/settlements",
    status_code=status.HTTP_201_CREATED,
    response_model=s.ConfirmedSettlementOut,
)
def confirm_settlement(
    body: s.SettlementConfirm, group: CurrentGroup, engine: EngineDep, response: Response
) -> s.ConfirmedSettlementOut:
    """GET /settlement에서 받은 version으로 확정한다. 그사이 분개가 더 기록됐으면 409.

    멱등성 키를 받지 않는다: (그룹, 버전)이 유일하므로 같은 버전으로 다시 보내면
    처음 확정을 200으로 돌려준다(자연 키).
    """
    settlement, created = store.confirm_settlement(
        engine, group, body.expected_version, body.strategy
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
        transfers=_transfers_out(list(settlement.transfers)),
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


def _split_rule(split: s.EqualSplitIn | s.RatioSplitIn | s.FixedSplitIn, group: Group) -> SplitRule:
    """요청의 분할을 코어 SplitRule로. 검증(중복, 합계, 가중치)은 코어가 한다."""
    if isinstance(split, s.EqualSplitIn):
        return EqualSplit([member_key(m) for m in split.member_ids])
    if isinstance(split, s.RatioSplitIn):
        ids = [w.member_id for w in split.weights]
        _no_duplicates(ids)
        return RatioSplit({member_key(w.member_id): w.weight for w in split.weights})
    ids = [a.member_id for a in split.amounts]
    _no_duplicates(ids)
    return FixedSplit(
        {member_key(a.member_id): Money.of(a.amount, group.currency) for a in split.amounts}
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
        created_at=entry.created_at,
        lines=[
            s.LineOut(member_id=ln.member_id, side=ln.side, amount=str(ln.amount.amount))
            for ln in entry.lines
        ],
    )
