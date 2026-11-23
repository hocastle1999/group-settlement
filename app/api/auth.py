"""초대 링크 토큰 인증.

회원가입 없이, 그룹을 만들 때 받은 토큰을 아는 사람이 그 그룹의 멤버다(정산 앱의 "링크로 초대").

- 토큰: `secrets.token_urlsafe(32)` (256비트 무작위). 그룹을 만들 때 응답으로 한 번만 보여 준다.
- 저장: 토큰 원문이 아니라 SHA-256 해시. DB가 유출돼도 해시로는 API를 호출할 수 없다.
  - 비밀번호처럼 bcrypt/argon2(느린 해시 + 솔트)를 쓰지 않는 이유: 사람이 고른 비밀번호와 달리
    256비트 무작위 값이라 사전 공격이나 무차별 대입이 불가능하다. 또 솔트를 넣으면 같은 토큰도
    해시가 매번 달라 "해시로 인덱스 조회"를 할 수 없다.
- 전달: `Authorization: Bearer <토큰>` 헤더. URL 경로나 쿼리에 넣지 않는다
  (접근 로그, 브라우저 기록, Referer 헤더에 남는다).
- 실패 응답: 토큰이 틀려도, 그룹이 없어도 똑같이 404. 403을 주면 "이 id의 그룹은 있다"는 사실이 새어
  id를 하나씩 늘려 가며 그룹 수를 셀 수 있다. 헤더가 아예 없을 때만 401이다(그룹과 무관한 응답).
"""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import Engine

from app.db import store
from app.db.store import Group

TOKEN_BYTES = 32


def new_invite_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> bytes:
    return store.hash_bytes(token.encode("utf-8"))


def get_engine(request: Request) -> Engine:
    return request.app.state.engine


def _bearer_token(authorization: str | None) -> str:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="Authorization: Bearer <초대 토큰> 헤더가 필요하다",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token.strip()


def current_group(
    group_id: int,
    engine: Annotated[Engine, Depends(get_engine)],
    authorization: Annotated[str | None, Header()] = None,
) -> Group:
    """경로의 그룹 id와 토큰이 함께 맞아야 그룹을 돌려준다.

    WHERE id = ? AND invite_token_hash = ? 한 번으로 확인한다. 토큰 해시를 비교하므로 문자열 비교
    시간 차이로 토큰을 한 글자씩 알아내는 타이밍 공격도 의미가 없다.
    """
    token = _bearer_token(authorization)
    group = store.find_group(engine, group_id, hash_token(token))
    if group is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="그룹을 찾을 수 없다")
    return group


CurrentGroup = Annotated[Group, Depends(current_group)]
EngineDep = Annotated[Engine, Depends(get_engine)]
