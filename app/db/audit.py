"""감사 로그 (6단계): 추가만 하고, 해시 체인으로 고쳐지거나 지워지면 드러나게 한다.

기록 방식
- 바뀌는 내용(분개, 멤버, 정산)과 **같은 트랜잭션**에 기록한다. 변경은 됐는데 기록이 없거나,
  기록은 있는데 변경이 롤백된 상태가 생기지 않는다. (앱 로그 파일이나 비동기 큐로 보내면
  둘 중 하나만 남을 수 있다)
- 그룹마다 순번(seq)을 매긴다. 그룹 잠금을 잡은 트랜잭션에서만 부르므로 순서가 꼬이지 않는다.
- 행마다 hash = SHA-256(직전 행 hash + 이 행의 내용)을 저장한다. 첫 행의 직전 hash는 0 32바이트.
  그룹 행에는 마지막 순번과 마지막 hash(`audit_seq`, `audit_head`)를 둔다.

검증(`verify`)이 잡는 것
- 가운데 행의 내용을 고침 -> 그 행의 hash가 다시 계산한 값과 다르다
- 가운데 행을 지움 -> 순번이 비고, 다음 행의 prev_hash가 이어지지 않는다
- 끝부분을 잘라 냄 -> 마지막 순번/hash가 그룹 행의 기준점과 다르다

한계: DB에 쓸 수 있는 사람은 체인 전체와 기준점을 다시 계산해 바꿀 수 있다. 이것까지 막으려면
DB 밖의 비밀 키로 서명(HMAC)하거나, 기준점 hash를 바깥(다른 저장소, 공개 게시)에
주기적으로 남겨야 한다.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, Engine, select, update

from app.db.tables import audit_events, settlement_groups

GENESIS = bytes(32)


@dataclass(frozen=True, slots=True)
class AuditEvent:
    seq: int
    action: str
    target_id: int | None
    actor_member_id: int | None
    detail: dict[str, Any]
    created_at: datetime  # UTC
    prev_hash: bytes
    hash: bytes


@dataclass(frozen=True, slots=True)
class VerifyResult:
    valid: bool
    events: int
    broken_at_seq: int | None = None
    reason: str | None = None


def _payload(
    group_id: int,
    seq: int,
    action: str,
    target_id: int | None,
    actor_member_id: int | None,
    detail: dict[str, Any],
    created_at: datetime,
) -> bytes:
    """해시할 내용. 키를 정렬한 JSON이라 DB(JSON 열)가 키 순서나 공백을 바꿔도 같은 값이다."""
    return json.dumps(
        {
            "group_id": group_id,
            "seq": seq,
            "action": action,
            "target_id": target_id,
            "actor_member_id": actor_member_id,
            "detail": detail,
            "created_at": created_at.strftime("%Y-%m-%dT%H:%M:%S.%f"),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def chain_hash(prev_hash: bytes, payload: bytes) -> bytes:
    return hashlib.sha256(prev_hash + payload).digest()


def append(
    conn: Connection,
    group_id: int,
    action: str,
    target_id: int | None,
    actor_member_id: int | None,
    detail: dict[str, Any],
) -> AuditEvent:
    """감사 로그 한 줄을 추가한다.

    그룹 잠금을 잡은 트랜잭션(또는 그룹을 막 만든 트랜잭션)에서 부른다.

    기준점은 잠금 읽기(FOR UPDATE)로 읽어 스냅숏과 상관없이 최신 값을 본다.
    """
    head = conn.execute(
        select(settlement_groups.c.audit_seq, settlement_groups.c.audit_head)
        .where(settlement_groups.c.id == group_id)
        .with_for_update()
    ).one()
    seq = head.audit_seq + 1
    prev_hash = head.audit_head or GENESIS
    # DB에는 UTC 마이크로초까지 그대로 저장된다(DATETIME(6)). 읽어서 다시 해시해도 같은 값이다.
    created_at = datetime.now(UTC).replace(tzinfo=None)
    digest = chain_hash(
        prev_hash, _payload(group_id, seq, action, target_id, actor_member_id, detail, created_at)
    )
    conn.execute(
        audit_events.insert().values(
            group_id=group_id,
            seq=seq,
            action=action,
            target_id=target_id,
            actor_member_id=actor_member_id,
            detail=detail,
            created_at=created_at,
            prev_hash=prev_hash,
            hash=digest,
        )
    )
    conn.execute(
        update(settlement_groups)
        .where(settlement_groups.c.id == group_id)
        .values(audit_seq=seq, audit_head=digest)
    )
    return AuditEvent(
        seq,
        action,
        target_id,
        actor_member_id,
        detail,
        created_at.replace(tzinfo=UTC),
        prev_hash,
        digest,
    )


def list_events(
    engine: Engine, group_id: int, limit: int, before_seq: int | None = None
) -> list[AuditEvent]:
    """최신부터. 다음 쪽은 마지막 seq를 before_seq로 (키셋 페이지네이션)."""
    query = select(audit_events).where(audit_events.c.group_id == group_id)
    if before_seq is not None:
        query = query.where(audit_events.c.seq < before_seq)
    query = query.order_by(audit_events.c.seq.desc()).limit(limit)
    with engine.connect() as conn:
        return [_event(row) for row in conn.execute(query)]


def verify(engine: Engine, group_id: int) -> VerifyResult:
    """처음부터 끝까지 hash를 다시 계산해 체인과 기준점을 확인한다. 처음 어긋난 순번을 돌려준다."""
    with engine.connect() as conn:  # 한 트랜잭션 = 한 스냅숏: 검증 도중 추가된 행과 섞이지 않는다
        head = conn.execute(
            select(settlement_groups.c.audit_seq, settlement_groups.c.audit_head).where(
                settlement_groups.c.id == group_id
            )
        ).one()
        rows = conn.execute(
            select(audit_events)
            .where(audit_events.c.group_id == group_id)
            .order_by(audit_events.c.seq)
        )
        prev_hash, expected_seq, count = GENESIS, 1, 0
        for row in rows:
            count += 1
            if row.seq != expected_seq:
                return VerifyResult(False, count, expected_seq, "순번이 비었다 (행이 지워졌다)")
            if row.prev_hash != prev_hash:
                return VerifyResult(False, count, row.seq, "직전 행과 이어지지 않는다")
            event = _event(row)
            payload = _payload(
                group_id,
                event.seq,
                event.action,
                event.target_id,
                event.actor_member_id,
                event.detail,
                event.created_at.replace(tzinfo=None),
            )
            if chain_hash(prev_hash, payload) != row.hash:
                return VerifyResult(False, count, row.seq, "내용이 hash와 다르다 (고쳐졌다)")
            prev_hash, expected_seq = row.hash, expected_seq + 1
    if count != head.audit_seq or (count and prev_hash != head.audit_head):
        return VerifyResult(False, count, count + 1, "마지막 행이 기준점과 다르다 (끝이 잘렸다)")
    return VerifyResult(True, count)


def _event(row) -> AuditEvent:
    detail = row.detail if isinstance(row.detail, dict) else json.loads(row.detail)
    return AuditEvent(
        seq=row.seq,
        action=row.action,
        target_id=row.target_id,
        actor_member_id=row.actor_member_id,
        detail=detail,
        created_at=row.created_at.replace(tzinfo=UTC),
        prev_hash=row.prev_hash,
        hash=row.hash,
    )
