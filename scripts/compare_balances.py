"""잔액 계산 방식과 동시성 비교 실험 (4단계). 결과는 docs/experiments.md에 정리한다.

실행: .\\.venv\\Scripts\\python.exe -m scripts.compare_balances [--sizes 1000 10000 100000]

테스트 DB(group_settlement_test, 이름이 _test로 끝나야 함)를 비우고 쓴다. 개발 DB는 건드리지 않는다.

1) 조회: 한 그룹의 분개 줄 수에 따른 잔액 조회 시간
   - SUM: 3단계 방식. 인덱스 (group_id, member_id)로 멤버마다 줄을 찾고
     side, amount는 기본키로 다시 읽는다.
   - SUM + 커버링 인덱스 (group_id, member_id, side, amount): 인덱스만 읽고 끝난다.
     4단계부터 스키마의 인덱스다.
   - 잔액 테이블: 멤버마다 한 행. 미리 더해 둔 값을 읽는다.
2) 쓰기: 분개 하나를 기록하는 시간. 잔액 테이블을 쓰면 같은 트랜잭션에서 멤버 행을 UPDATE해야 한다.
3) 잔액 테이블의 동시성 함정 (그룹 잠금 없이, 스레드 여러 개가 같은 멤버들 사이 송금)
   - 읽고 더해서 쓰기(read-modify-write): 갱신 손실
   - UPDATE balance = balance + ? 를 멤버 순서 없이: 데드락
   - 같은 UPDATE를 멤버 id 순서로: 둘 다 없음
4) 정산 확정의 굶주림: 쓰기 사이 간격에 따른 확정 성공률

난수는 random.Random(seed)만 쓴다.
"""

from __future__ import annotations

import argparse
import os
import random
import statistics
import threading
import time
from collections.abc import Callable
from decimal import Decimal

# 앱 설정을 읽기 전에 테스트 DB로 바꾼다 (.env보다 환경 변수가 우선).
os.environ["DB_NAME"] = os.environ.get("TEST_DB_NAME", "group_settlement_test")

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import Engine, text  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

from app.db import store  # noqa: E402
from app.db.config import DatabaseSettings, create_db_engine  # noqa: E402
from app.db.store import Group, StaleVersionError, member_key  # noqa: E402
from app.db.tables import metadata  # noqa: E402
from app.ledger import (  # noqa: E402
    EqualSplit,
    Money,
    Strategy,
    expense_entry,
    get_currency,
    transfer_entry,
)

KRW = get_currency("KRW")
MEMBERS = 20
ER_LOCK_DEADLOCK = 1213


# ---------------------------------------------------------------- 준비


def connect() -> Engine:
    settings = DatabaseSettings()
    assert settings.db_name.endswith("_test"), f"실험은 *_test DB에서만 (현재 {settings.db_name})"
    command.upgrade(Config("alembic.ini"), "head")
    return create_db_engine(settings)


def clear(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for table in reversed(metadata.sorted_tables):
            conn.execute(text(f"DELETE FROM {table.name}"))
        # 실험용 잔액 테이블은 스키마(마이그레이션)에 없다. 남겨 두면 alembic check가 차이로 본다.
        conn.execute(text("DROP TABLE IF EXISTS bench_balances"))
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))


def create_bench_table(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE bench_balances ("
                " group_id BIGINT NOT NULL, member_id BIGINT NOT NULL,"
                " balance DECIMAL(19, 4) NOT NULL DEFAULT 0,"
                " PRIMARY KEY (group_id, member_id))"
            )
        )


def make_group(engine: Engine, name: str) -> tuple[Group, list[str]]:
    group = store.create_group(engine, name, KRW, store.hash_bytes(name.encode()))
    ids = [member_key(store.add_member(engine, group.id, f"m{i}").id) for i in range(MEMBERS)]
    return group, ids


def bulk_load(engine: Engine, group: Group, ids: list[str], entries: int, rng: random.Random):
    """분개 `entries`개(줄 4개씩: 대변 300, 차변 100 x 3)를 빠르게 넣는다.

    실험 데이터 적재라 코어 검사를 거치지 않고 여러 행 INSERT로 넣는다. 순번은 이어서 매긴다.
    """
    chunk = 5_000
    with engine.begin() as conn:
        last = store.lock_group(conn, group.id)
        for start in range(0, entries, chunk):
            n = min(chunk, entries - start)
            seqs = range(last + start + 1, last + start + n + 1)
            first_id = conn.execute(
                text(
                    "INSERT INTO journal_entries (group_id, seq, kind) VALUES "
                    + ",".join(f"({group.id}, {s}, 'EXPENSE')" for s in seqs)
                )
            ).lastrowid  # 여러 행 INSERT면 첫 행의 id. 같은 문장 안의 id는 연속이다.
            rows = []
            for k in range(n):
                payer, *others = rng.sample(ids, 4)
                eid = first_id + k
                rows.append(f"({group.id}, {eid}, 1, {payer}, 'CREDIT', 300)")
                rows += [
                    f"({group.id}, {eid}, {i}, {m}, 'DEBIT', 100)" for i, m in enumerate(others, 2)
                ]
            conn.execute(
                text(
                    "INSERT INTO entry_lines (group_id, entry_id, line_no, member_id, side, amount)"
                    " VALUES " + ",".join(rows)
                )
            )
        conn.execute(
            text("UPDATE settlement_groups SET last_seq = :s WHERE id = :g"),
            {"s": last + entries, "g": group.id},
        )


def median_ms(fn: Callable[[], object], repeat: int) -> float:
    fn()  # 첫 실행(캐시 데우기)은 빼고 잰다
    times = []
    for _ in range(repeat):
        t = time.perf_counter()
        fn()
        times.append((time.perf_counter() - t) * 1000)
    return statistics.median(times)


COVER = "ix_entry_lines_group_id_member_id_side_amount"
PLAIN = "ix_bench_plain"


def swap_index(engine: Engine, to_plain: bool) -> None:
    with engine.begin() as conn:
        if to_plain:
            conn.execute(text(f"CREATE INDEX {PLAIN} ON entry_lines (group_id, member_id)"))
            conn.execute(text(f"DROP INDEX {COVER} ON entry_lines"))
        else:
            conn.execute(
                text(f"CREATE INDEX {COVER} ON entry_lines (group_id, member_id, side, amount)")
            )
            conn.execute(text(f"DROP INDEX {PLAIN} ON entry_lines"))
        conn.execute(text("ANALYZE TABLE entry_lines"))


def explain_key(engine: Engine, group_id: int) -> str:
    query = (
        "EXPLAIN SELECT m.id, SUM(CASE WHEN l.side = 'CREDIT' THEN l.amount ELSE -l.amount END)"
        " FROM members m LEFT JOIN entry_lines l"
        " ON l.group_id = m.group_id AND l.member_id = m.id"
        " WHERE m.group_id = :g GROUP BY m.id"
    )
    with engine.connect() as conn:
        rows = conn.execute(text(query), {"g": group_id}).mappings().all()
    line = next(r for r in rows if r["table"] == "l")
    return f"{line['key']} ({line['Extra'] or '-'})"


# ---------------------------------------------------------------- 1) 조회


def read_benchmark(engine: Engine, sizes: list[int], repeat: int) -> list[str]:
    rng = random.Random(1)
    # 다른 그룹의 줄도 섞어 둔다 (테이블에 한 그룹만 있으면 인덱스 효과가 과장된다).
    noise, noise_ids = make_group(engine, "noise")
    bulk_load(engine, noise, noise_ids, 25_000, rng)
    group, ids = make_group(engine, "target")

    def sum_balances() -> dict:
        with engine.connect() as conn:
            return store.group_balances(conn, group)

    def table_balances() -> list:
        with engine.connect() as conn:
            return conn.execute(
                text("SELECT member_id, balance FROM bench_balances WHERE group_id = :g"),
                {"g": group.id},
            ).all()

    out = [
        "| 그룹의 줄 수 | SUM (ms) | SUM + 커버링 인덱스 (ms) | 잔액 테이블 (ms)"
        " | SUM이 쓴 인덱스 | 커버링일 때 |",
        "| ---: | ---: | ---: | ---: | --- | --- |",
    ]
    loaded = 0
    for lines in sizes:
        bulk_load(engine, group, ids, lines // 4 - loaded, rng)
        loaded = lines // 4
        with engine.begin() as conn:
            conn.execute(text("ANALYZE TABLE entry_lines"))
            conn.execute(text("DELETE FROM bench_balances WHERE group_id = :g"), {"g": group.id})
            for member, money in store.group_balances(conn, group).items():
                conn.execute(
                    text("INSERT INTO bench_balances VALUES (:g, :m, :b)"),
                    {"g": group.id, "m": member, "b": money.amount},
                )
        # 스키마에는 커버링 인덱스가 있다(마이그레이션 b37a5d0c1e92). 먼저 그대로 재고,
        # 비교용으로 예전 인덱스 (group_id, member_id)로 잠시 바꿔 잰 뒤 되돌린다.
        # 멤버 외래 키가 인덱스를 써야 하므로 항상 "새 인덱스를 만든 뒤 기존 것을 지운다".
        cover_key = explain_key(engine, group.id)
        covered = median_ms(sum_balances, repeat)
        swap_index(engine, to_plain=True)
        plain_key = explain_key(engine, group.id)
        plain = median_ms(sum_balances, repeat)
        swap_index(engine, to_plain=False)
        table = median_ms(table_balances, repeat)
        out.append(
            f"| {lines:,} | {plain:.2f} | {covered:.2f} | {table:.2f} | {plain_key} | {cover_key} |"
        )
        print(out[-1], flush=True)
    return out


# ---------------------------------------------------------------- 2) 쓰기


def write_benchmark(engine: Engine, writes: int) -> list[str]:
    rng = random.Random(2)
    group, ids = make_group(engine, "writes")
    bulk_load(engine, group, ids, 2_500, rng)  # 줄 1만 개
    with engine.begin() as conn:
        for member, money in store.group_balances(conn, group).items():
            conn.execute(
                text("INSERT INTO bench_balances VALUES (:g, :m, :b)"),
                {"g": group.id, "m": member, "b": money.amount},
            )

    def entry():
        people = rng.sample(ids, 4)
        return expense_entry(people[0], Money.of(rng.randint(1, 100_000), KRW), EqualSplit(people))

    def post(with_table: bool, key: str) -> None:
        e = entry()
        with engine.begin() as conn:
            seq = store.lock_group(conn, group.id) + 1
            store._insert_checked(conn, group, e, seq)
            conn.execute(
                text("UPDATE settlement_groups SET last_seq = :s WHERE id = :g"),
                {"s": seq, "g": group.id},
            )
            if with_table:
                deltas: dict[int, Decimal] = {}
                for line in e.lines:
                    sign = 1 if line.side.value == "CREDIT" else -1
                    member = int(line.member)
                    deltas[member] = deltas.get(member, Decimal(0)) + sign * line.amount.amount
                for member in sorted(deltas):  # 데드락을 피하는 고정 순서 (3번 실험)
                    conn.execute(
                        text(
                            "UPDATE bench_balances SET balance = balance + :d"
                            " WHERE group_id = :g AND member_id = :m"
                        ),
                        {"d": deltas[member], "g": group.id, "m": member},
                    )

    counter = iter(range(10**9))
    plain = median_ms(lambda: post(False, f"p{next(counter)}"), writes)
    table = median_ms(lambda: post(True, f"t{next(counter)}"), writes)
    with engine.connect() as conn:
        drift = conn.execute(
            text(
                "SELECT COUNT(*) FROM bench_balances b JOIN ("
                " SELECT member_id, SUM(CASE WHEN side = 'CREDIT' THEN amount ELSE -amount END) s"
                " FROM entry_lines WHERE group_id = :g GROUP BY member_id) l"
                " ON l.member_id = b.member_id WHERE b.group_id = :g AND b.balance <> l.s"
            ),
            {"g": group.id},
        ).scalar_one()
    # 잔액 테이블을 쓰지 않은 기록(plain)도 섞였으므로 drift는 그만큼 생긴다 -> 이 값은 표시만 한다.
    return [
        "| 방식 | 분개 하나 기록 (ms, 중앙값) |",
        "| --- | ---: |",
        f"| 원장만 (지금) | {plain:.2f} |",
        f"| 원장 + 잔액 테이블 UPDATE (멤버 4명) | {table:.2f} |",
        "",
        f"(참고: 잔액 테이블 없이 기록한 {writes + 1}건 때문에 어긋난 멤버 수 {drift}."
        " 두 저장소를 함께 쓰면 모든 쓰기 경로가 둘 다 갱신해야 한다)",
    ]


# ---------------------------------------------------------------- 3) 잔액 테이블 동시성


def balance_table_race(engine: Engine, threads: int, transfers: int) -> list[str]:
    members = [1, 2, 3, 4]
    group_id = 999_999

    def reset() -> None:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM bench_balances WHERE group_id = :g"), {"g": group_id})
            for m in members:
                conn.execute(
                    text("INSERT INTO bench_balances VALUES (:g, :m, 0)"), {"g": group_id, "m": m}
                )

    def plan(seed: int) -> list[tuple[int, int, int]]:
        rng = random.Random(seed)
        return [(*rng.sample(members, 2), rng.randint(1, 1000)) for _ in range(transfers)]

    def naive(conn, sender: int, receiver: int, amount: int) -> None:
        for member, delta in ((sender, amount), (receiver, -amount)):
            current = conn.execute(
                text("SELECT balance FROM bench_balances WHERE group_id = :g AND member_id = :m"),
                {"g": group_id, "m": member},
            ).scalar_one()
            conn.execute(
                text(
                    "UPDATE bench_balances SET balance = :b WHERE group_id = :g AND member_id = :m"
                ),
                {"b": current + delta, "g": group_id, "m": member},
            )

    def atomic(order_by_id: bool):
        def apply(conn, sender: int, receiver: int, amount: int) -> None:
            pairs = [(sender, amount), (receiver, -amount)]
            if order_by_id:
                pairs.sort()
            for member, delta in pairs:
                conn.execute(
                    text(
                        "UPDATE bench_balances SET balance = balance + :d"
                        " WHERE group_id = :g AND member_id = :m"
                    ),
                    {"d": delta, "g": group_id, "m": member},
                )

        return apply

    def run(apply) -> tuple[int, int, Decimal, float]:
        reset()
        deadlocks = 0
        lock = threading.Lock()
        expected = {m: 0 for m in members}
        plans = [plan(t) for t in range(threads)]
        for p in plans:
            for sender, receiver, amount in p:
                expected[sender] += amount
                expected[receiver] -= amount

        def worker(p):
            nonlocal deadlocks
            for sender, receiver, amount in p:
                while True:  # 데드락으로 롤백되면 다시 시도한다 (실제 서비스와 같은 처리)
                    try:
                        with engine.begin() as conn:
                            apply(conn, sender, receiver, amount)
                        break
                    except OperationalError as exc:
                        if exc.orig.args[0] != ER_LOCK_DEADLOCK:
                            raise
                        with lock:
                            deadlocks += 1

        started = time.perf_counter()
        ts = [threading.Thread(target=worker, args=(p,)) for p in plans]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        elapsed = time.perf_counter() - started
        with engine.connect() as conn:
            actual = dict(
                conn.execute(
                    text("SELECT member_id, balance FROM bench_balances WHERE group_id = :g"),
                    {"g": group_id},
                ).all()
            )
        lost = sum(abs(actual[m] - expected[m]) for m in members)
        total = sum(actual.values())
        return deadlocks, lost, total, elapsed

    out = [
        f"스레드 {threads}개 x 송금 {transfers}건, 멤버 4명 (그룹 잠금 없음)",
        "",
        "| 방식 | 데드락 (재시도) | 기대값과의 차이 합 | 잔액 합 | 시간 (s) |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for name, apply in [
        ("읽고 더해서 쓰기 (SELECT 후 UPDATE)", naive),
        ("UPDATE balance = balance + ?, 순서 없음", atomic(False)),
        ("UPDATE balance = balance + ?, 멤버 id 순서", atomic(True)),
    ]:
        deadlocks, lost, total, elapsed = run(apply)
        out.append(f"| {name} | {deadlocks} | {lost:,.0f} | {total:,.0f} | {elapsed:.2f} |")
        print(out[-1], flush=True)
    return out


# ---------------------------------------------------------------- 4) 확정 굶주림


def starve_once(engine: Engine, writers: int, seconds: float, gap_ms: int) -> str:
    """쓰기 스레드들이 `gap_ms` 간격으로 기록하는 동안 확정 스레드 하나가 계속 시도한다."""
    group, ids = make_group(engine, f"starve{gap_ms}")
    stop = threading.Event()
    attempts = successes = 0

    def writer(t: int) -> None:
        rng = random.Random(t)
        i = 0
        while not stop.is_set():
            a, b = rng.sample(ids, 2)
            e = transfer_entry(a, b, Money.of(rng.randint(1, 1000), KRW))
            key = f"{t}-{i}"
            store.post_entry(engine, group, lambda _c, e=e: e, key, store.hash_bytes(key.encode()))
            i += 1
            if gap_ms:
                time.sleep(gap_ms / 1000)

    def confirmer() -> None:
        nonlocal attempts, successes
        while not stop.is_set():
            attempts += 1
            try:
                version = store.current_version(engine, group)
                store.confirm_settlement(engine, group, version, Strategy.GREEDY)
                successes += 1
            except StaleVersionError:
                pass

    threads = [threading.Thread(target=writer, args=(t,)) for t in range(writers)]
    threads.append(threading.Thread(target=confirmer))
    for t in threads:
        t.start()
    time.sleep(seconds)
    stop.set()
    for t in threads:
        t.join()
    entries = store.current_version(engine, group)
    rate = successes / attempts if attempts else 0
    return f"| {gap_ms}ms | {entries:,} | {attempts:,} | {successes:,} | {rate:.0%} |"


def confirm_starvation(
    engine: Engine, writers: int, seconds: float, gaps_ms: list[int]
) -> list[str]:
    out = [
        f"쓰기 스레드 {writers}개가 {seconds:.0f}초 동안 송금을 기록하는 사이,"
        " 확정 스레드 하나가 계속 시도",
        "",
        "| 쓰기 사이 간격 | 기록된 분개 | 확정 시도 | 성공 | 성공률 |",
        "| ---: | ---: | ---: | ---: | ---: |",
    ]
    for gap in gaps_ms:
        out.append(starve_once(engine, writers, seconds, gap))
        print(out[-1], flush=True)
    return out


# ---------------------------------------------------------------- 실행


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sizes", type=int, nargs="+", default=[1_000, 10_000, 100_000, 1_000_000])
    parser.add_argument("--repeat", type=int, default=30, help="조회 측정 반복 횟수")
    parser.add_argument("--writes", type=int, default=200, help="쓰기 측정 횟수")
    parser.add_argument("--output", help="결과를 저장할 Markdown 파일 (UTF-8)")
    args = parser.parse_args()

    engine = connect()
    clear(engine)
    create_bench_table(engine)
    sections = [
        ("1) 잔액 조회", read_benchmark(engine, args.sizes, args.repeat)),
        ("2) 분개 기록", write_benchmark(engine, args.writes)),
        ("3) 잔액 테이블의 동시 갱신", balance_table_race(engine, threads=8, transfers=200)),
        ("4) 정산 확정 성공률", confirm_starvation(engine, 4, 3.0, [0, 2, 10, 50])),
    ]
    clear(engine)
    engine.dispose()

    text_out = "\n\n".join(f"### {title}\n\n" + "\n".join(lines) for title, lines in sections)
    print("\n" + text_out)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text_out + "\n")


if __name__ == "__main__":
    main()
