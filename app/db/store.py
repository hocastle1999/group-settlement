"""원장 저장 계층. 규칙 검사는 코어(`app.ledger`)에 맡기고, 여기서는 읽고 쓰기만 한다.

쓰기 흐름 (분개 하나 = 트랜잭션 하나):
1. 코어 함수(`expense_entry`, `transfer_entry`, `reversal_of`)로 `JournalEntry`를 만든다.
   만드는 순간 균형(차변 합 = 대변 합)이 검사된다.
2. `check_postable`, `check_reversal`로 통화, 멤버, 취소 가능 여부를 검사한다.
   메모리 원장(`Ledger`)과 같은 함수다. SQL로 같은 규칙을 다시 쓰지 않는다.
3. 분개, 줄, 멱등성 키를 INSERT하고 커밋한다. 하나라도 실패하면 전부 롤백된다.

DB 제약(CHECK, 유니크, 복합 외래 키)은 코어를 거치지 않은 쓰기나 동시 요청에 대한 마지막 방어선이다.

원장 테이블에는 UPDATE와 DELETE를 하지 않는다(append-only). 취소는 역분개 INSERT다.

코어의 멤버 ID(`MemberId`, 문자열)는 DB 멤버 id를 문자열로 바꾼 값이다. 분할의 동점 처리가
"멤버 ID 순서"인데, 문자열 순서("10" < "9")라도 같은 입력에 같은 결과라는 결정성은 지켜진다.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Connection, Engine, case, func, select, update
from sqlalchemy.exc import IntegrityError

from app.db.tables import (
    entry_lines,
    idempotency_keys,
    journal_entries,
    members,
    settlement_groups,
    settlement_transfers,
    settlements,
)
from app.ledger import (
    AlreadyReversedError,
    Currency,
    EntryKind,
    EntryNotFoundError,
    ForeignAmount,
    JournalEntry,
    Line,
    Money,
    PostedEntry,
    Side,
    Strategy,
    UnbalancedBalancesError,
    check_postable,
    check_reversal,
    get_currency,
    reversal_of,
    settle,
    total_balance,
)

# MySQL 오류 번호: 유니크 키 중복
ER_DUP_ENTRY = 1062


# ---------------------------------------------------------------- 예외 (원장 규칙이 아닌 것)


class StoreError(Exception):
    """저장 계층 예외의 공통 부모. 원장 규칙 위반은 `LedgerError`다."""


class DuplicateMemberError(StoreError):
    """그룹에 같은 이름의 멤버가 이미 있다."""


class IdempotencyKeyReusedError(StoreError):
    """같은 멱등성 키로 다른 요청이 왔다."""


# ---------------------------------------------------------------- 읽기 모델


@dataclass(frozen=True, slots=True)
class Group:
    id: int
    name: str
    currency: Currency


@dataclass(frozen=True, slots=True)
class Member:
    id: int
    name: str


@dataclass(frozen=True, slots=True)
class StoredLine:
    member_id: int
    side: Side
    amount: Money


@dataclass(frozen=True, slots=True)
class StoredEntry:
    id: int
    seq: int  # 그룹 안 순번 (1부터, 빈 번호 없음)
    kind: EntryKind
    memo: str
    reverses_entry_id: int | None
    reversed_by_entry_id: int | None  # 이 분개를 취소한 역분개 id
    original: ForeignAmount | None  # 외화 지출이면 원래 금액과 환율 (줄은 기준 통화)
    created_at: datetime
    lines: tuple[StoredLine, ...]


@dataclass(frozen=True, slots=True)
class PostResult:
    entry: StoredEntry
    replayed: bool  # True면 새로 기록하지 않고 같은 멱등성 키의 이전 결과를 돌려줬다


def member_key(member_id: int) -> str:
    """DB 멤버 id -> 코어 MemberId."""
    return str(member_id)


def hash_bytes(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


# ---------------------------------------------------------------- 그룹, 멤버


def create_group(engine: Engine, name: str, currency: Currency, token_hash: bytes) -> Group:
    with engine.begin() as conn:
        result = conn.execute(
            settlement_groups.insert().values(
                name=name, currency=currency.code, invite_token_hash=token_hash
            )
        )
        return Group(int(result.inserted_primary_key[0]), name, currency)


def find_group(engine: Engine, group_id: int, token_hash: bytes) -> Group | None:
    """id와 토큰 해시가 모두 맞는 그룹. 그룹이 없거나 토큰이 틀리면 똑같이 None."""
    with engine.connect() as conn:
        row = conn.execute(
            select(settlement_groups.c.name, settlement_groups.c.currency).where(
                settlement_groups.c.id == group_id,
                settlement_groups.c.invite_token_hash == token_hash,
            )
        ).one_or_none()
    if row is None:
        return None
    return Group(group_id, row.name, get_currency(row.currency))


def add_member(engine: Engine, group_id: int, name: str) -> Member:
    try:
        with engine.begin() as conn:
            result = conn.execute(members.insert().values(group_id=group_id, name=name))
            return Member(int(result.inserted_primary_key[0]), name)
    except IntegrityError as exc:
        # 이름 중복 검사를 SELECT로 먼저 하면 두 요청이 동시에 통과할 수 있다.
        # 유니크 제약에 맡기고 위반을 번역하는 쪽이 경쟁 상황에서도 정확하다.
        if _is_duplicate(exc):
            raise DuplicateMemberError(f"이미 있는 이름: {name}") from None
        raise


def list_members(conn: Connection, group_id: int) -> list[Member]:
    rows = conn.execute(
        select(members.c.id, members.c.name)
        .where(members.c.group_id == group_id)
        .order_by(members.c.id)
    )
    return [Member(r.id, r.name) for r in rows]


def get_members(engine: Engine, group_id: int) -> list[Member]:
    with engine.connect() as conn:
        return list_members(conn, group_id)


# ---------------------------------------------------------------- 분개 쓰기

# 분개를 만드는 함수 build(conn, seq).
# - conn: 역분개는 원래 분개를 DB에서 읽어야 만들 수 있다.
# - seq: 이 분개가 받을 순번. 분할에서 남는 단위를 받을 출발점(rotation)으로 쓴다.
#   그룹 잠금 안에서 불리므로 이 순번은 확정된 값이다.
EntryBuilder = Callable[[Connection, int], JournalEntry]


def post_entry(
    engine: Engine,
    group: Group,
    build: EntryBuilder,
    idempotency_key: str,
    request_hash: bytes,
) -> PostResult:
    """분개 하나를 검사하고 기록한다. 같은 멱등성 키로 다시 오면 처음 결과를 돌려준다.

    한 트랜잭션 안에서:
    1. 그룹 행을 잠근다(`lock_group`). 같은 그룹의 쓰기는 여기서 한 줄로 선다.
    2. 잠금을 기다리는 사이 같은 키의 요청이 먼저 커밋됐는지 다시 본다 -> 있으면 그 결과를 돌려준다.
    3. 분개를 만들고(코어) 검사하고 순번 last_seq + 1로 INSERT, last_seq를 1 늘린다.
    4. 멱등성 키를 INSERT한다. 커밋하면 잠금이 풀린다.
    키 INSERT의 유니크 위반은 잠금을 거치지 않는 경로를 위한 마지막 방어선으로 남겨 둔다.
    """
    # 흔한 재시도는 잠금 없이 여기서 끝난다.
    with engine.connect() as conn:
        replay = _find_replay(conn, group.id, idempotency_key, request_hash)
    if replay is not None:
        return replay

    try:
        with engine.begin() as conn:
            last_seq = lock_group(conn, group.id)
            replay = _find_replay(conn, group.id, idempotency_key, request_hash)
            if replay is not None:
                return replay
            seq = last_seq + 1
            entry = build(conn, seq)
            entry_id = _insert_checked(conn, group, entry, seq)
            conn.execute(
                update(settlement_groups)
                .where(settlement_groups.c.id == group.id)
                .values(last_seq=seq)
            )
            try:
                conn.execute(
                    idempotency_keys.insert().values(
                        group_id=group.id,
                        idem_key=idempotency_key,
                        request_hash=request_hash,
                        entry_id=entry_id,
                    )
                )
            except IntegrityError as exc:
                if _is_duplicate(exc):
                    raise _KeyAlreadyUsed from None
                raise
            stored = _load_entries(conn, group.id, [entry_id])[0]
    except _KeyAlreadyUsed:
        # 위 트랜잭션은 롤백됐다(분개도 없음). 먼저 커밋된 요청의 결과를 새 연결로 읽는다.
        with engine.connect() as conn:
            replay = _find_replay(conn, group.id, idempotency_key, request_hash)
        assert replay is not None
        return replay
    return PostResult(stored, replayed=False)


def lock_group(conn: Connection, group_id: int) -> int:
    """그룹 행을 배타 잠금(SELECT ... FOR UPDATE)하고 지금 원장 버전(last_seq)을 돌려준다.

    잠금은 트랜잭션이 끝날 때(커밋, 롤백) 풀린다. 다른 그룹의 쓰기는 막지 않는다(행 잠금).

    반드시 트랜잭션의 첫 쿼리로 부른다. MySQL 기본 격리 수준(REPEATABLE READ)에서 일반 SELECT는
    트랜잭션의 "첫 일반 읽기" 시점 스냅숏을 계속 본다. 잠금보다 먼저 일반 SELECT를 하면, 잠금을
    기다리는 동안 다른 트랜잭션이 커밋한 분개가 그 뒤의 읽기에서 보이지 않는다.
    (FOR UPDATE 자체는 잠금 읽기라 스냅숏이 아니라 최신 커밋 값을 읽는다)
    """
    return conn.execute(
        select(settlement_groups.c.last_seq)
        .where(settlement_groups.c.id == group_id)
        .with_for_update()
    ).scalar_one()


class _KeyAlreadyUsed(Exception):
    """멱등성 키가 다른 트랜잭션에서 이미 커밋됐다 (내부 신호)."""


def _find_replay(
    conn: Connection, group_id: int, key: str, request_hash: bytes
) -> PostResult | None:
    row = conn.execute(
        select(idempotency_keys.c.request_hash, idempotency_keys.c.entry_id).where(
            idempotency_keys.c.group_id == group_id,
            idempotency_keys.c.idem_key == key,
        )
    ).one_or_none()
    if row is None:
        return None
    if row.request_hash != request_hash:
        # 같은 키로 다른 내용을 보냈다. 이전 결과를 주면 클라이언트는 새 요청이 처리된 줄 안다.
        raise IdempotencyKeyReusedError(f"멱등성 키 {key!r}는 다른 요청에 이미 쓰였다")
    return PostResult(_load_entries(conn, group_id, [row.entry_id])[0], replayed=True)


def _insert_checked(conn: Connection, group: Group, entry: JournalEntry, seq: int) -> int:
    member_ids = [member_key(m.id) for m in list_members(conn, group.id)]
    check_postable(entry, group.currency, member_ids)
    if entry.kind is EntryKind.REVERSAL:
        assert entry.reverses is not None
        target = load_posted(conn, group, entry.reverses)
        check_reversal(entry, target, _is_reversed(conn, target.entry_id))

    try:
        result = conn.execute(
            journal_entries.insert().values(
                group_id=group.id,
                seq=seq,
                kind=entry.kind.value,
                memo=entry.memo,
                reverses_entry_id=entry.reverses,
                original_currency=entry.source.amount.currency.code if entry.source else None,
                original_amount=entry.source.amount.amount if entry.source else None,
                fx_rate=entry.source.rate if entry.source else None,
            )
        )
    except IntegrityError as exc:
        # 검사와 INSERT 사이에 다른 요청이 같은 분개를 먼저 취소했다.
        # uq_journal_entries_reverses_entry_id가 두 번째 취소를 막는다.
        if entry.kind is EntryKind.REVERSAL and _is_duplicate(exc):
            raise AlreadyReversedError(f"분개 {entry.reverses}는 이미 취소됐다") from None
        raise
    entry_id = int(result.inserted_primary_key[0])
    conn.execute(
        entry_lines.insert(),
        [
            {
                "group_id": group.id,
                "entry_id": entry_id,
                "line_no": no,
                "member_id": int(line.member),
                "side": line.side.value,
                "amount": line.amount.amount,
            }
            for no, line in enumerate(entry.lines, start=1)
        ],
    )
    return entry_id


def _is_reversed(conn: Connection, entry_id: int) -> bool:
    return (
        conn.execute(
            select(journal_entries.c.id).where(journal_entries.c.reverses_entry_id == entry_id)
        ).first()
        is not None
    )


def _is_duplicate(exc: IntegrityError) -> bool:
    return bool(exc.orig.args) and exc.orig.args[0] == ER_DUP_ENTRY


# ---------------------------------------------------------------- 분개 읽기


def load_posted(conn: Connection, group: Group, entry_id: int) -> PostedEntry:
    """DB의 분개를 코어 객체로 되살린다. 만들면서 균형 검사를 다시 하므로 손상된 기록도 드러난다."""
    found = _load_entries(conn, group.id, [entry_id])
    if not found:
        raise EntryNotFoundError(f"분개 {entry_id}가 없다")
    stored = found[0]
    entry = JournalEntry(
        stored.kind,
        tuple(Line(member_key(ln.member_id), ln.side, ln.amount) for ln in stored.lines),
        stored.memo,
        reverses=stored.reverses_entry_id,
        source=stored.original,
    )
    return PostedEntry(stored.id, entry)


def reversal_builder(group: Group, entry_id: int, memo: str) -> EntryBuilder:
    def build(conn: Connection, _seq: int) -> JournalEntry:
        return reversal_of(load_posted(conn, group, entry_id), memo)

    return build


def list_entries(
    engine: Engine, group_id: int, limit: int, before_id: int | None = None
) -> list[StoredEntry]:
    """최신 분개부터 `limit`개. 다음 쪽은 마지막 id를 `before_id`로 넘긴다 (키셋 페이지네이션).

    OFFSET 방식은 뒤쪽 페이지일수록 건너뛸 행을 모두 읽고, 그사이 새 분개가 생기면 항목이 밀린다.
    """
    query = select(journal_entries.c.id).where(journal_entries.c.group_id == group_id)
    if before_id is not None:
        query = query.where(journal_entries.c.id < before_id)
    query = query.order_by(journal_entries.c.id.desc()).limit(limit)
    with engine.connect() as conn:
        ids = list(conn.execute(query).scalars())
        return _load_entries(conn, group_id, ids)


def _load_entries(conn: Connection, group_id: int, ids: Sequence[int]) -> list[StoredEntry]:
    """분개와 줄을 쿼리 두 번으로 읽는다 (분개마다 줄을 읽는 N+1 쿼리를 피한다). id 순서 유지."""
    if not ids:
        return []
    currency = _group_currency(conn, group_id)
    reverser = journal_entries.alias("reverser")
    heads = conn.execute(
        select(
            journal_entries.c.id,
            journal_entries.c.seq,
            journal_entries.c.kind,
            journal_entries.c.memo,
            journal_entries.c.reverses_entry_id,
            journal_entries.c.original_currency,
            journal_entries.c.original_amount,
            journal_entries.c.fx_rate,
            journal_entries.c.created_at,
            reverser.c.id.label("reversed_by"),
        )
        .select_from(
            journal_entries.outerjoin(
                reverser, reverser.c.reverses_entry_id == journal_entries.c.id
            )
        )
        .where(journal_entries.c.group_id == group_id, journal_entries.c.id.in_(ids))
    ).all()
    lines: dict[int, list[StoredLine]] = {}
    for row in conn.execute(
        select(entry_lines)
        .where(entry_lines.c.group_id == group_id, entry_lines.c.entry_id.in_(ids))
        .order_by(entry_lines.c.entry_id, entry_lines.c.line_no)
    ):
        lines.setdefault(row.entry_id, []).append(
            StoredLine(row.member_id, Side(row.side), Money(row.amount, currency))
        )
    by_id = {
        h.id: StoredEntry(
            id=h.id,
            seq=h.seq,
            kind=EntryKind(h.kind),
            memo=h.memo,
            reverses_entry_id=h.reverses_entry_id,
            original=(
                None
                if h.original_currency is None
                else ForeignAmount(
                    Money(h.original_amount, get_currency(h.original_currency)), h.fx_rate
                )
            ),
            reversed_by_entry_id=h.reversed_by,
            created_at=h.created_at.replace(tzinfo=UTC),  # DB에는 UTC로 저장된다 (연결 시간대)
            lines=tuple(lines.get(h.id, ())),
        )
        for h in heads
    }
    return [by_id[i] for i in ids if i in by_id]


def _group_currency(conn: Connection, group_id: int) -> Currency:
    code = conn.execute(
        select(settlement_groups.c.currency).where(settlement_groups.c.id == group_id)
    ).scalar_one()
    return get_currency(code)


# ---------------------------------------------------------------- 잔액


@dataclass(frozen=True, slots=True)
class LedgerSnapshot:
    """한 시점의 원장: 버전(그 시점까지 기록된 마지막 순번)과 그 버전의 잔액."""

    version: int
    balances: dict[int, Money]


def snapshot(engine: Engine, group: Group) -> LedgerSnapshot:
    """버전과 잔액을 같은 트랜잭션에서 읽는다.

    REPEATABLE READ에서는 한 트랜잭션의 일반 SELECT가 모두 같은 스냅숏을 본다. 그래서 두 쿼리
    사이에 다른 분개가 커밋돼도 "버전 N과 버전 N+1의 잔액"처럼 어긋난 짝이 나오지 않는다.
    """
    with engine.connect() as conn:
        version = conn.execute(
            select(settlement_groups.c.last_seq).where(settlement_groups.c.id == group.id)
        ).scalar_one()
        return LedgerSnapshot(version, group_balances(conn, group))


def balances(engine: Engine, group: Group) -> dict[int, Money]:
    with engine.connect() as conn:
        return group_balances(conn, group)


def group_balances(conn: Connection, group: Group) -> dict[int, Money]:
    """멤버별 잔액(대변 합 - 차변 합)을 SQL SUM으로 구한다. 기록이 없는 멤버는 0.

    LEFT JOIN이라 줄이 없는 멤버도 나온다. 합이 0이 아니면 원장이 깨진 것이므로 예외를 낸다.
    """
    signed = case(
        (entry_lines.c.side == Side.CREDIT.value, entry_lines.c.amount),
        else_=-entry_lines.c.amount,
    )
    query = (
        select(members.c.id, func.coalesce(func.sum(signed), 0).label("balance"))
        .select_from(
            members.outerjoin(
                entry_lines,
                (entry_lines.c.group_id == members.c.group_id)
                & (entry_lines.c.member_id == members.c.id),
            )
        )
        .where(members.c.group_id == group.id)
        .group_by(members.c.id)
        .order_by(members.c.id)
    )
    result = {row.id: Money.of(row.balance, group.currency) for row in conn.execute(query)}
    total = total_balance({member_key(m): v for m, v in result.items()}, group.currency)
    if not total.is_zero():
        raise UnbalancedBalancesError(f"그룹 {group.id}의 잔액 합이 0이 아니다: {total}")
    return result


# ---------------------------------------------------------------- 정산 확정 (4단계)


class StaleVersionError(StoreError):
    """확정하려는 버전 이후에 분개가 더 기록됐다. 최신 정산 계획을 다시 받아 확정해야 한다."""

    def __init__(self, expected: int, current: int):
        super().__init__(f"원장 버전이 바뀌었다: 요청 {expected}, 현재 {current}")
        self.extra = {"expected_version": expected, "current_version": current}


class SettlementConflictError(StoreError):
    """같은 버전이 다른 방법(strategy)으로 이미 확정됐다."""


class SettlementNotFoundError(StoreError):
    """이 그룹에 없는 정산."""


@dataclass(frozen=True, slots=True)
class SettlementTransfer:
    sender_id: int
    receiver_id: int
    amount: Money


@dataclass(frozen=True, slots=True)
class StoredSettlement:
    id: int
    version: int
    strategy: Strategy
    created_at: datetime
    transfers: tuple[SettlementTransfer, ...]


def plan_settlement(balances: dict[int, Money], strategy: Strategy) -> list[SettlementTransfer]:
    """잔액 -> 송금 계획. 계산은 코어(`settle`)가 하고 여기서는 id만 바꾼다."""
    transfers = settle({member_key(m): v for m, v in balances.items()}, strategy)
    return [SettlementTransfer(int(t.sender), int(t.receiver), t.amount) for t in transfers]


def confirm_settlement(
    engine: Engine, group: Group, expected_version: int, strategy: Strategy
) -> tuple[StoredSettlement, bool]:
    """원장 버전이 `expected_version`일 때만 그 시점의 송금 계획을 확정 기록한다.

    돌려주는 bool은 새로 기록했는지(False면 같은 버전의 이전 확정을 돌려줌).

    그룹 잠금 아래에서 버전을 비교하고 잔액을 읽는다. 지출 기록도 같은 잠금을 잡으므로
    "확정하는 순간 원장은 정확히 그 버전"이 커밋 시점까지 보장된다. 확정과 지출이 동시에 오면
    둘 중 하나가 먼저 하고, 지출이 먼저면 확정은 409(StaleVersionError)다.
    (잠금 없이 한 스냅숏에서 읽기만 해도 "버전 N의 올바른 계획"은 나온다. 잠금은 그 계획이
    커밋 순간에도 최신이라는 것까지 보장한다.)
    """
    with engine.begin() as conn:
        current = lock_group(conn, group.id)  # 첫 쿼리 (lock_group 설명 참고)
        existing = _settlement_by_version(conn, group, expected_version)
        if existing is not None:
            # 같은 버전을 다시 확정: 재시도로 보고 처음 기록을 돌려준다.
            if existing.strategy is not strategy:
                raise SettlementConflictError(
                    f"버전 {expected_version}은 이미 {existing.strategy.value}로 확정됐다"
                )
            return existing, False
        if current != expected_version:
            raise StaleVersionError(expected_version, current)

        transfers = plan_settlement(group_balances(conn, group), strategy)
        settlement_id = int(
            conn.execute(
                settlements.insert().values(
                    group_id=group.id, version=current, strategy=strategy.value
                )
            ).inserted_primary_key[0]
        )
        if transfers:
            conn.execute(
                settlement_transfers.insert(),
                [
                    {
                        "group_id": group.id,
                        "settlement_id": settlement_id,
                        "line_no": no,
                        "sender_member_id": t.sender_id,
                        "receiver_member_id": t.receiver_id,
                        "amount": t.amount.amount,
                    }
                    for no, t in enumerate(transfers, start=1)
                ],
            )
        return _load_settlement(conn, group, settlement_id), True


def get_settlement(engine: Engine, group: Group, settlement_id: int) -> StoredSettlement:
    with engine.connect() as conn:
        return _load_settlement(conn, group, settlement_id)


def list_settlements(engine: Engine, group: Group, limit: int) -> list[StoredSettlement]:
    """최근 확정부터."""
    with engine.connect() as conn:
        ids = conn.execute(
            select(settlements.c.id)
            .where(settlements.c.group_id == group.id)
            .order_by(settlements.c.version.desc())
            .limit(limit)
        ).scalars()
        return [_load_settlement(conn, group, i) for i in ids]


def current_version(engine: Engine, group: Group) -> int:
    with engine.connect() as conn:
        return conn.execute(
            select(settlement_groups.c.last_seq).where(settlement_groups.c.id == group.id)
        ).scalar_one()


def _settlement_by_version(conn: Connection, group: Group, version: int) -> StoredSettlement | None:
    settlement_id = conn.execute(
        select(settlements.c.id).where(
            settlements.c.group_id == group.id, settlements.c.version == version
        )
    ).scalar_one_or_none()
    return None if settlement_id is None else _load_settlement(conn, group, settlement_id)


def _load_settlement(conn: Connection, group: Group, settlement_id: int) -> StoredSettlement:
    head = conn.execute(
        select(settlements).where(
            settlements.c.group_id == group.id, settlements.c.id == settlement_id
        )
    ).one_or_none()
    if head is None:
        raise SettlementNotFoundError(f"정산 {settlement_id}가 없다")
    rows = conn.execute(
        select(settlement_transfers)
        .where(
            settlement_transfers.c.group_id == group.id,
            settlement_transfers.c.settlement_id == settlement_id,
        )
        .order_by(settlement_transfers.c.line_no)
    )
    return StoredSettlement(
        id=head.id,
        version=head.version,
        strategy=Strategy(head.strategy),
        created_at=head.created_at.replace(tzinfo=UTC),
        transfers=tuple(
            SettlementTransfer(
                r.sender_member_id, r.receiver_member_id, Money(r.amount, group.currency)
            )
            for r in rows
        ),
    )
