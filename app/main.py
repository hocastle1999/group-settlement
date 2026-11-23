"""FastAPI 앱. 실행: uvicorn app.main:app"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import errors
from app.api.rate_limit import RateLimiter, client_ip, retry_after_header
from app.api.routes import router
from app.config import AppSettings
from app.db.config import DatabaseSettings, create_db_engine


async def json_charset_utf8(request: Request, call_next):
    """JSON 응답의 Content-Type에 charset을 붙인다: application/json; charset=utf-8

    JSON은 표준상(RFC 8259) UTF-8뿐이라 charset이 없어도 되고 Starlette도 붙이지 않는다.
    그런데 Windows PowerShell 5.1의 Invoke-RestMethod는 charset이 없으면 응답을 ISO-8859-1로
    읽어 한글이 깨진다 (docs/troubleshooting.md). 오류 응답(422, 401, 404)까지 한 곳에서 처리하려고
    응답 클래스가 아니라 미들웨어로 고친다.
    """
    response = await call_next(request)
    if response.headers.get("content-type") == "application/json":
        response.headers["content-type"] = "application/json; charset=utf-8"
    return response


STATIC_DIR = Path(__file__).resolve().parent / "static"


def rate_limit_middleware(settings: AppSettings):
    """POST만 센다. 그룹 만들기는 시간당, 나머지 쓰기는 분당 한도. 넘으면 429 + Retry-After.

    async 미들웨어라 이벤트 루프 스레드 하나에서만 돈다. check() 안에 await가 없으므로
    두 요청이 같은 deque를 동시에 고치는 일이 없다(락이 필요 없다).
    """
    groups = RateLimiter(settings.rate_limit_groups_per_hour, 3600)
    writes = RateLimiter(settings.rate_limit_writes_per_minute, 60)

    async def middleware(request: Request, call_next):
        if request.method != "POST":
            return await call_next(request)
        if request.url.path == "/api/groups":
            limiter = groups
        else:
            limiter = writes
        if limiter.limit > 0:
            ip = client_ip(
                request.client.host if request.client else None,
                request.headers.get("x-forwarded-for"),
                settings.trusted_proxy_hops,
            )
            wait = limiter.check(ip)
            if wait > 0:
                return JSONResponse(
                    {"error": "RateLimited", "detail": "요청이 너무 많다. 잠시 뒤 다시 시도하라"},
                    status_code=429,
                    headers={"Retry-After": retry_after_header(wait)},
                    media_type="application/json; charset=utf-8",
                )
        return await call_next(request)

    return middleware


def create_app(
    db_settings: DatabaseSettings | None = None, settings: AppSettings | None = None
) -> FastAPI:
    """설정을 받아 새 앱을 만든다. 테스트는 테스트 DB 설정으로 앱을 따로 만든다."""
    engine = create_db_engine(db_settings or DatabaseSettings())

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        engine.dispose()  # 종료할 때 연결 풀을 닫는다

    settings = settings or AppSettings()
    app = FastAPI(title="group-settlement", lifespan=lifespan)
    app.middleware("http")(json_charset_utf8)
    app.middleware("http")(rate_limit_middleware(settings))
    app.state.engine = engine
    app.state.settings = settings
    app.include_router(router)
    errors.install(app)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # 데모 페이지 (6단계). 스크립트는 파일로만 받는다: CSP가 페이지 안의 인라인 스크립트를 막아,
    # 누가 기록(메모, 이름)에 스크립트를 넣어도 실행되지 않게 한 겹 더 막는다.
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def demo_page() -> FileResponse:
        return FileResponse(
            STATIC_DIR / "index.html",
            headers={
                "Content-Security-Policy": (
                    "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
                ),
                "Referrer-Policy": "no-referrer",
            },
        )

    return app


app = create_app()
