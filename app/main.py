"""FastAPI 앱. 실행: uvicorn app.main:app"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import errors
from app.api.routes import router
from app.db.config import DatabaseSettings, create_db_engine


def create_app(db_settings: DatabaseSettings | None = None) -> FastAPI:
    """설정을 받아 새 앱을 만든다. 테스트는 테스트 DB 설정으로 앱을 따로 만든다."""
    engine = create_db_engine(db_settings or DatabaseSettings())

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        engine.dispose()  # 종료할 때 연결 풀을 닫는다

    app = FastAPI(title="group-settlement", lifespan=lifespan)
    app.state.engine = engine
    app.include_router(router)
    errors.install(app)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
