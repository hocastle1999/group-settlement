"""감사 로그 테스트 (MySQL 테스트 DB 필요).

- 바뀌는 일마다 한 줄씩, 같은 트랜잭션에 남는다 (실패하거나 재시도면 남지 않는다)
- 해시 체인 검증이 DB를 직접 고치거나 지운 것을 잡는다
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import Engine, text

# ---------------------------------------------------------------- 도우미


def setup_group(client):
    body = client.post("/api/groups", json={"name": "모임", "currency": "KRW"}).json()
    headers = {"Authorization": f"Bearer {body['invite_token']}"}
    ids = {}
    for name in ("a", "b"):
        r = client.post(f"/api/groups/{body['id']}/members", json={"name": name}, headers=headers)
        ids[name] = r.json()["id"]
    return body["id"], headers, ids


def write(client, path, headers, json, key=None, actor=None):
    h = {**headers, "Idempotency-Key": key or str(uuid.uuid4())}
    if actor is not None:
        h["X-Actor-Member-Id"] = str(actor)
    return client.post(path, json=json, headers=h)


def expense(ids, amount="1000"):
    return {
        "payer_id": ids["a"],
        "amount": amount,
        "split": {"type": "equal", "member_ids": list(ids.values())},
    }


def events(client, gid, headers):
    """오래된 것부터."""
    res = client.get(f"/api/groups/{gid}/audit?limit=200", headers=headers)
    assert res.status_code == 200, res.text
    return list(reversed(res.json()["events"]))


def verify(client, gid, headers):
    return client.get(f"/api/groups/{gid}/audit/verify", headers=headers).json()


def full_history(client):
    """그룹 생성부터 송금 완료까지 모든 종류의 행동을 한 번씩."""
    gid, headers, ids = setup_group(client)
    base = f"/api/groups/{gid}"
    first = write(client, f"{base}/expenses", headers, expense(ids), actor=ids["a"]).json()
    write(client, f"{base}/entries/{first['id']}/reversal", headers, {})
    write(client, f"{base}/expenses", headers, expense(ids, "3000"))
    body = {"sender_id": ids["b"], "receiver_id": ids["a"], "amount": "500"}
    write(client, f"{base}/transfers", headers, body)
    version = client.get(f"{base}/settlement", headers=headers).json()["version"]
    settlement = client.post(
        f"{base}/settlements", json={"expected_version": version}, headers=headers
    ).json()
    write(client, f"{base}/settlements/{settlement['id']}/transfers/1/complete", headers, {})
    return gid, headers, ids


# ---------------------------------------------------------------- 기록


def test_every_change_is_logged_in_order(client):
    gid, headers, ids = full_history(client)
    logged = events(client, gid, headers)
    assert [e["action"] for e in logged] == [
        "GROUP_CREATED",
        "MEMBER_ADDED",
        "MEMBER_ADDED",
        "EXPENSE_RECORDED",
        "ENTRY_REVERSED",
        "EXPENSE_RECORDED",
        "TRANSFER_RECORDED",
        "SETTLEMENT_CONFIRMED",
        "TRANSFER_COMPLETED",
    ]
    assert [e["seq"] for e in logged] == list(range(1, 10))
    assert logged[1]["detail"] == {"name": "a"}
    assert logged[3]["actor_member_id"] == ids["a"]  # 헤더로 밝힌 사람
    assert logged[3]["detail"]["amount"] == "1000"
    assert logged[4]["detail"]["reverses_entry_id"] == logged[3]["target_id"]
    assert logged[8]["detail"]["line_no"] == 1
    assert verify(client, gid, headers) == {
        "valid": True,
        "events": 9,
        "broken_at_seq": None,
        "reason": None,
    }


def test_retries_and_failures_leave_no_log(client):
    gid, headers, ids = setup_group(client)
    path = f"/api/groups/{gid}/expenses"
    assert write(client, path, headers, expense(ids), key="same").status_code == 201
    assert write(client, path, headers, expense(ids), key="same").status_code == 201  # 재시도
    assert write(client, path, headers, expense(ids, "1.5")).status_code == 422  # 실패
    dup = client.post(f"/api/groups/{gid}/members", json={"name": "a"}, headers=headers)
    assert dup.status_code == 409
    actions = [e["action"] for e in events(client, gid, headers)]
    assert actions.count("EXPENSE_RECORDED") == 1
    assert actions.count("MEMBER_ADDED") == 2


def test_actor_must_be_a_member_of_the_group(client):
    gid, headers, ids = setup_group(client)
    path = f"/api/groups/{gid}/expenses"
    res = write(client, path, headers, expense(ids), actor=10**15)
    assert res.status_code == 422
    assert res.json()["error"] == "UnknownMemberError"
    assert write(client, path, headers, expense(ids), actor="abc").status_code == 422


def test_each_group_has_its_own_chain(client):
    gid, headers, _ = full_history(client)
    other_gid, other_headers, _ = setup_group(client)
    assert [e["seq"] for e in events(client, other_gid, other_headers)] == [1, 2, 3]
    assert verify(client, other_gid, other_headers)["valid"]
    assert verify(client, gid, headers)["valid"]


# ---------------------------------------------------------------- 변조 감지


def tamper(db: Engine, sql: str, **params) -> None:
    """앱을 거치지 않고 DB를 직접 고친다 (DB 접근 권한이 있는 사람의 변조를 흉내)."""
    with db.begin() as conn:
        conn.execute(text(sql), params)


def test_editing_a_row_breaks_the_chain(client, db):
    gid, headers, _ = full_history(client)
    tamper(
        db,
        "UPDATE audit_events SET detail = JSON_SET(detail, '$.amount', '1') "
        "WHERE group_id = :g AND seq = 4",
        g=gid,
    )
    result = verify(client, gid, headers)
    assert (result["valid"], result["broken_at_seq"]) == (False, 4)
    assert "고쳐졌다" in result["reason"]


@pytest.mark.parametrize("column", ["action = 'MEMBER_ADDED'", "actor_member_id = NULL"])
def test_editing_any_column_is_detected(client, db, column):
    gid, headers, _ = full_history(client)
    tamper(db, f"UPDATE audit_events SET {column} WHERE group_id = :g AND seq = 4", g=gid)
    assert verify(client, gid, headers)["broken_at_seq"] == 4


def test_deleting_a_middle_row_is_detected(client, db):
    gid, headers, _ = full_history(client)
    tamper(db, "DELETE FROM audit_events WHERE group_id = :g AND seq = 5", g=gid)
    result = verify(client, gid, headers)
    assert (result["valid"], result["broken_at_seq"]) == (False, 5)


def test_truncating_the_tail_is_detected(client, db):
    gid, headers, _ = full_history(client)
    tamper(db, "DELETE FROM audit_events WHERE group_id = :g AND seq >= 8", g=gid)
    result = verify(client, gid, headers)
    assert result["valid"] is False
    assert "잘렸다" in result["reason"]


def test_recomputing_the_whole_chain_is_the_known_limit(client, db):
    """DB에 쓸 수 있는 사람이 체인과 기준점을 통째로 다시 계산하면 검증은 통과한다.

    이 한계를 문서(docs/design.md 6단계)에 적었다.
    막으려면 DB 밖의 키(HMAC)나 바깥 기준점이 필요하다.
    """
    from app.db import audit
    from app.db.tables import audit_events, settlement_groups

    gid, headers, _ = full_history(client)
    tamper(
        db,
        "UPDATE audit_events SET detail = JSON_SET(detail, '$.amount', '1') "
        "WHERE group_id = :g AND seq = 4",
        g=gid,
    )
    with db.begin() as conn:  # 공격자처럼 4번부터 hash를 다시 계산해 덮어쓴다
        rows = conn.execute(
            audit_events.select().where(audit_events.c.group_id == gid).order_by(audit_events.c.seq)
        ).all()
        prev = audit.GENESIS
        for row in rows:
            event = audit._event(row)
            payload = audit._payload(
                gid,
                event.seq,
                event.action,
                event.target_id,
                event.actor_member_id,
                event.detail,
                event.created_at.replace(tzinfo=None),
            )
            digest = audit.chain_hash(prev, payload)
            conn.execute(
                audit_events.update()
                .where(audit_events.c.group_id == gid, audit_events.c.seq == event.seq)
                .values(prev_hash=prev, hash=digest)
            )
            prev = digest
        conn.execute(
            settlement_groups.update().where(settlement_groups.c.id == gid).values(audit_head=prev)
        )
    assert verify(client, gid, headers)["valid"] is True
