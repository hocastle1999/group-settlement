"""동시성 테스트 (MySQL 테스트 DB 필요).

- 왜 AUTO_INCREMENT id를 원장 버전으로 쓸 수 없는가 (MySQL 동작 확인)
- 왜 그룹 잠금을 트랜잭션의 첫 쿼리로 잡아야 하는가 (REPEATABLE READ 스냅숏)
- 같은 그룹의 쓰기는 그룹 잠금에서 기다리고, 다른 그룹은 기다리지 않는다
"""

from __future__ import annotations

import threading
import time

from sqlalchemy import Engine, func, insert, select

from app.db import store
from app.db.store import Group, member_key
from app.db.tables import journal_entries, settlement_groups
from app.ledger import Money, get_currency, transfer_entry

KRW = get_currency("KRW")


def make_group(db: Engine, token: bytes) -> tuple[Group, list[str]]:
    group = store.create_group(db, "모임", KRW, store.hash_bytes(token))
    ids = [member_key(store.add_member(db, group.id, name).id) for name in ("a", "b")]
    return group, ids


def post_transfer(db: Engine, group: Group, ids: list[str], key: str, amount: int = 100):
    entry = transfer_entry(ids[0], ids[1], Money.of(amount, KRW))
    return store.post_entry(db, group, lambda _c: entry, key, store.hash_bytes(key.encode()))


def run_in_thread(fn) -> tuple[threading.Thread, list]:
    out: list = []
    thread = threading.Thread(target=lambda: out.append(fn()))
    thread.start()
    return thread, out


# ---------------------------------------------------------------- 문제 확인 (MySQL 동작)


def test_auto_increment_order_is_not_commit_order(db):
    """id를 먼저 받은 트랜잭션이 늦게 커밋하면, "가장 큰 id"까지 봤어도
    그보다 작은 id가 나중에 나타난다.

    그래서 MAX(id)는 "여기까지 반영했다"는 버전이 될 수 없다. 이 프로젝트가 그룹별 순번(seq)을
    그룹 잠금 아래에서 매기는 이유다.
    """
    group, _ = make_group(db, b"t1")
    slow = db.connect()
    try:
        slow.begin()
        early_id = slow.execute(
            insert(journal_entries).values(group_id=group.id, seq=1, kind="EXPENSE")
        ).inserted_primary_key[0]  # 아직 커밋 안 함

        with db.begin() as fast:  # 늦게 시작했지만 먼저 커밋
            late_id = fast.execute(
                insert(journal_entries).values(group_id=group.id, seq=2, kind="EXPENSE")
            ).inserted_primary_key[0]
        assert early_id < late_id

        with db.connect() as reader:
            visible = reader.execute(select(func.max(journal_entries.c.id))).scalar_one()
            assert visible == late_id  # "버전 = late_id"라고 읽었지만
            assert (
                reader.execute(
                    select(journal_entries.c.id).where(journal_entries.c.id == early_id)
                ).first()
                is None
            )  # 그보다 작은 early_id는 아직 안 보인다
        slow.commit()  # 이제 early_id가 나타난다: "버전 late_id"에 빠져 있던 분개
    finally:
        slow.close()


def test_snapshot_taken_before_lock_misses_commits(db):
    """REPEATABLE READ: 일반 SELECT는 트랜잭션의 첫 일반 읽기 시점 스냅숏을 계속 본다.

    잠금(FOR UPDATE)보다 먼저 일반 SELECT를 하면, 잠금을 기다리는 동안 커밋된 분개가 뒤의 읽기에서
    보이지 않는다. 잠금 읽기 자체는 최신 커밋을 본다.
    그래서 `lock_group`을 트랜잭션의 첫 쿼리로 부른다.
    """
    group, ids = make_group(db, b"t2")
    count_entries = select(func.count()).select_from(journal_entries)
    with db.connect() as conn:
        conn.begin()
        before = conn.execute(count_entries).scalar_one()  # 여기서 스냅숏이 정해진다
        post_transfer(db, group, ids, "other")  # 다른 트랜잭션이 분개를 커밋
        locked_version = store.lock_group(conn, group.id)  # 잠금 읽기: 최신 값
        after = conn.execute(count_entries).scalar_one()  # 일반 읽기: 옛 스냅숏
        conn.rollback()
    assert locked_version == 1
    assert before == after == 0


# ---------------------------------------------------------------- 그룹 잠금


def test_writes_in_same_group_wait_for_group_lock(db):
    """그룹을 잠근 트랜잭션이 있으면 같은 그룹의 쓰기는 기다리고, 다른 그룹은 기다리지 않는다."""
    group, ids = make_group(db, b"t3")
    other, other_ids = make_group(db, b"t4")

    holder = db.connect()
    try:
        holder.begin()
        store.lock_group(holder, group.id)

        waiting, result = run_in_thread(lambda: post_transfer(db, group, ids, "w"))
        time.sleep(0.5)
        assert waiting.is_alive()  # 같은 그룹: 잠금에서 기다린다

        free = post_transfer(db, other, other_ids, "o")  # 다른 그룹: 바로 끝난다
        assert free.entry.seq == 1

        holder.rollback()  # 잠금 해제
        waiting.join(timeout=10)
        assert not waiting.is_alive()
        assert result[0].entry.seq == 1
    finally:
        holder.close()


def test_concurrent_writes_get_gapless_seq_in_commit_order(db):
    """여러 스레드가 같은 그룹에 동시에 써도 순번은 1..n, 빈 번호 없이 커밋 순서대로."""
    group, ids = make_group(db, b"t5")
    n_threads, per_thread = 6, 10
    start = threading.Barrier(n_threads)
    errors: list[Exception] = []

    def worker(t: int) -> None:
        try:
            start.wait(timeout=10)
            for i in range(per_thread):
                post_transfer(db, group, ids, f"k{t}-{i}", amount=1 + t)
        except Exception as exc:  # pragma: no cover - 실패 시 원인 표시용
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors

    total = n_threads * per_thread
    with db.connect() as conn:
        rows = conn.execute(
            select(journal_entries.c.id, journal_entries.c.seq)
            .where(journal_entries.c.group_id == group.id)
            .order_by(journal_entries.c.seq)
        ).all()
        last_seq = conn.execute(
            select(settlement_groups.c.last_seq).where(settlement_groups.c.id == group.id)
        ).scalar_one()
    assert [r.seq for r in rows] == list(range(1, total + 1))
    assert last_seq == total
    # 잠금 아래에서 INSERT하므로 id 순서도 seq 순서와 같다 (id를 받은 순서 = 커밋 순서)
    assert [r.id for r in rows] == sorted(r.id for r in rows)
