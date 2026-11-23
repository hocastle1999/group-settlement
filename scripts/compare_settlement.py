"""그리디 vs 최적 정산 비교 실험. 결과는 docs/experiments.md에 정리한다.

실행: .\\.venv\\Scripts\\python.exe -m scripts.compare_settlement

1) 송금 횟수: 무작위 모임(지출 기록을 실제로 원장에 넣어 만든 잔액)에서
   두 방법의 송금 횟수를 비교한다.
   - realistic: 금액이 500원 단위. 합이 0인 작은 묶음이 우연히 생기기 어렵다.
   - round: 금액이 10,000원 단위이고 지출마다 참여자 2~3명. 잔액이 같은 값으로 겹치기 쉽다.
2) 실행 시간: 잔액이 남은 사람 수 k에 따른 최적 DP의 시간 (O(2^k x k)).

난수는 random.Random(seed)만 쓴다. 같은 seed면 같은 결과.
"""

from __future__ import annotations

import argparse
import random
import statistics
import time
from dataclasses import dataclass

from app.ledger import (
    EqualSplit,
    Ledger,
    Money,
    expense_entry,
    get_currency,
    greedy_transfers,
    optimal_transfers,
)

KRW = get_currency("KRW")


@dataclass(frozen=True)
class Scenario:
    name: str
    unit: int  # 금액 단위 (원)
    max_units: int  # 지출 하나의 최대 단위 수
    min_participants: int
    max_participants: int | None  # None이면 전원까지


SCENARIOS = [
    Scenario("realistic", unit=500, max_units=400, min_participants=2, max_participants=None),
    Scenario("round", unit=10_000, max_units=5, min_participants=2, max_participants=3),
]


def random_balances(rng: random.Random, n: int, scenario: Scenario) -> dict[str, Money]:
    """n명 모임에서 지출 3n건을 무작위로 기록한 뒤의 잔액."""
    members = [f"m{i:02}" for i in range(n)]
    ledger = Ledger(KRW, members)
    for _ in range(3 * n):
        payer = rng.choice(members)
        upper = n if scenario.max_participants is None else min(n, scenario.max_participants)
        size = rng.randint(min(scenario.min_participants, upper), upper)
        participants = rng.sample(members, size)
        amount = rng.randint(1, scenario.max_units) * scenario.unit
        ledger.post(expense_entry(payer, Money.of(amount, KRW), EqualSplit(participants)))
    return ledger.balances()


def timed(fn, *args) -> tuple[list, float]:
    start = time.perf_counter()
    result = fn(*args)
    return result, (time.perf_counter() - start) * 1000


def compare_counts(seed: int, trials: int, sizes: list[int]) -> None:
    rng = random.Random(seed)
    print(f"## 송금 횟수 (모임마다 {trials}번, seed={seed})\n")
    print(
        "| 시나리오 | 인원 | 평균 k | 그리디 평균 | 최적 평균 | 그리디가 더 많은 비율 "
        "| 최대 차이 | 그리디 ms | 최적 ms |"
    )
    print("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for scenario in SCENARIOS:
        for n in sizes:
            ks, greedy_counts, optimal_counts, greedy_ms, optimal_ms = [], [], [], [], []
            worse, max_gap = 0, 0
            for _ in range(trials):
                balances = random_balances(rng, n, scenario)
                ks.append(sum(1 for v in balances.values() if not v.is_zero()))
                greedy, g_ms = timed(greedy_transfers, balances)
                optimal, o_ms = timed(optimal_transfers, balances, n)
                greedy_counts.append(len(greedy))
                optimal_counts.append(len(optimal))
                greedy_ms.append(g_ms)
                optimal_ms.append(o_ms)
                gap = len(greedy) - len(optimal)
                worse += gap > 0
                max_gap = max(max_gap, gap)
            print(
                f"| {scenario.name} | {n} | {statistics.mean(ks):.1f} "
                f"| {statistics.mean(greedy_counts):.2f} | {statistics.mean(optimal_counts):.2f} "
                f"| {worse / trials:.0%} | {max_gap} "
                f"| {statistics.mean(greedy_ms):.3f} | {statistics.mean(optimal_ms):.1f} |"
            )
    print()


def time_optimal(seed: int, ks: list[int], repeats: int) -> None:
    rng = random.Random(seed)
    print(f"## 최적 DP 실행 시간 (k별 {repeats}번 중 중앙값, seed={seed})\n")
    print("| k (잔액이 남은 사람) | 2^k x k | 최적 ms | 그리디 ms |")
    print("| ---: | ---: | ---: | ---: |")
    for k in ks:
        opt_times, greedy_times = [], []
        for _ in range(repeats):
            values = [rng.randint(-1_000_000, 1_000_000) or 1 for _ in range(k - 1)]
            values.append(-sum(values))
            balances = {f"m{i:02}": Money.from_minor(v, KRW) for i, v in enumerate(values)}
            opt_times.append(timed(optimal_transfers, balances, k)[1])
            greedy_times.append(timed(greedy_transfers, balances)[1])
        print(
            f"| {k} | {(2**k) * k:,} | {statistics.median(opt_times):.1f} "
            f"| {statistics.median(greedy_times):.3f} |"
        )
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--trials", type=int, default=300)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()

    compare_counts(args.seed, args.trials, sizes=[4, 6, 8, 10, 12])
    time_optimal(args.seed, ks=[8, 10, 12, 14, 16, 18, 20], repeats=args.repeats)


if __name__ == "__main__":
    main()
