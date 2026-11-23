"""앱 설정 (DB 접속 외). 환경 변수 또는 .env에서 읽는다."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.ledger.settle import DEFAULT_OPTIMAL_LIMIT


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # 정산 AUTO가 최적(비트마스크 DP)을 쓰는 최대 인원(잔액이 남은 사람 수). 넘으면 그리디.
    # DP는 O(2^k x k): 실측 16명 37ms, 18명 170ms, 20명 870ms (docs/experiments.md 2단계).
    # 요청 하나가 1초를 넘지 않도록 20명까지만 허용한다. 느린 서버면 낮춘다.
    settle_optimal_limit: int = Field(default=DEFAULT_OPTIMAL_LIMIT, ge=1, le=20)

    # 쓰기 빈도 제한 (app/api/rate_limit.py). 0이면 끈다. 접속자(IP)마다 센다.
    rate_limit_groups_per_hour: int = Field(default=10, ge=0)  # 그룹 만들기 (인증 없음)
    rate_limit_writes_per_minute: int = Field(default=60, ge=0)  # 그 밖의 모든 POST
    # 앞단 프록시 수. 로컬은 0(접속한 상대 = 사용자), Render는 1. X-Forwarded-For를 오른쪽에서 센다.
    trusted_proxy_hops: int = Field(default=0, ge=0, le=5)
