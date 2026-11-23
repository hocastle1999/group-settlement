"""쓰기 빈도 제한 테스트."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.api.rate_limit import RateLimiter, client_ip, retry_after_header
from app.config import AppSettings
from app.main import create_app

# ---------------------------------------------------------------- 단위


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_sliding_window_allows_limit_then_waits_for_oldest():
    clock = FakeClock()
    limiter = RateLimiter(limit=2, window_s=10, clock=clock)
    assert limiter.check("ip") == 0
    clock.now = 4
    assert limiter.check("ip") == 0
    clock.now = 6
    assert limiter.check("ip") == pytest.approx(4)  # 가장 오래된 0초 요청이 10초에 빠진다
    clock.now = 10
    assert limiter.check("ip") == 0
    assert limiter.check("other") == 0  # 접속자마다 따로 센다


def test_retry_after_is_whole_seconds_at_least_one():
    assert retry_after_header(0.2) == "1"
    assert retry_after_header(3.01) == "4"


@pytest.mark.parametrize(
    ("forwarded", "hops", "expected"),
    [
        (None, 1, "10.0.0.1"),  # 헤더 없음 -> 접속한 상대
        ("203.0.113.7", 1, "203.0.113.7"),  # 프록시가 붙인 실제 IP
        ("1.2.3.4, 203.0.113.7", 1, "203.0.113.7"),  # 사용자가 위조한 왼쪽 값은 무시
        ("203.0.113.7", 0, "10.0.0.1"),  # 프록시를 믿지 않으면 헤더를 보지 않는다
        ("203.0.113.7", 2, "10.0.0.1"),  # 홉이 모자라면 헤더를 믿지 않는다
    ],
)
def test_client_ip_counts_trusted_hops_from_the_right(forwarded, hops, expected):
    assert client_ip("10.0.0.1", forwarded, hops) == expected


# ---------------------------------------------------------------- 앱


@pytest.fixture
def limited(db, db_settings):
    settings = AppSettings(
        rate_limit_groups_per_hour=2, rate_limit_writes_per_minute=3, trusted_proxy_hops=1
    )
    app = create_app(db_settings, settings)
    app.state.engine = db
    with TestClient(app) as c:
        yield c


def create_group(client, forwarded="203.0.113.7"):
    return client.post(
        "/api/groups",
        json={"name": "모임", "currency": "KRW"},
        headers={"X-Forwarded-For": forwarded},
    )


def test_group_creation_is_limited_per_ip(limited):
    assert create_group(limited).status_code == 201
    assert create_group(limited).status_code == 201
    blocked = create_group(limited)
    assert blocked.status_code == 429
    assert blocked.json()["error"] == "RateLimited"
    assert int(blocked.headers["Retry-After"]) > 3000  # 시간당 한도: 거의 한 시간 뒤
    assert create_group(limited, forwarded="198.51.100.9").status_code == 201  # 다른 사람


def test_spoofed_forwarded_for_does_not_bypass_the_limit(limited):
    for fake in ("1.1.1.1", "2.2.2.2"):
        assert create_group(limited, forwarded=f"{fake}, 203.0.113.7").status_code == 201
    assert create_group(limited, forwarded="3.3.3.3, 203.0.113.7").status_code == 429


def test_other_writes_have_their_own_limit_and_reads_are_free(limited):
    body = create_group(limited).json()
    headers = {"Authorization": f"Bearer {body['invite_token']}", "X-Forwarded-For": "203.0.113.7"}
    path = f"/api/groups/{body['id']}/members"
    for name in ("a", "b", "c"):
        assert limited.post(path, json={"name": name}, headers=headers).status_code == 201
    assert limited.post(path, json={"name": "d"}, headers=headers).status_code == 429
    for _ in range(5):  # 읽기는 세지 않는다
        assert limited.get(f"/api/groups/{body['id']}", headers=headers).status_code == 200
    h = {**headers, "Idempotency-Key": str(uuid.uuid4())}
    res = limited.post(f"/api/groups/{body['id']}/transfers", json={}, headers=h)
    assert res.status_code == 429  # 검증보다 먼저 막힌다 (DB에 닿지 않는다)
