# CLAUDE.md

모임 경비 정산 서비스. 백엔드 포트폴리오용 프로젝트(3번째).
1번(reservation-api)은 트랜잭션과 락, 2번(smart-elevator)은 실시간 처리와 알고리즘을 보여 준다.
이 프로젝트는 돈을 정확하게 다루는 설계(이중기입 원장, 금액 정밀도, 최소 송금)를 보여 준다.

## 목적과 작업 방식
- 사용자가 면접에서 모든 코드를 설명할 수 있어야 한다.
  - 새 개념을 도입할 때는 왜 그렇게 하는지, 다른 방법과 비교해 짧게 설명한다.
  - 기능 단위로 작게 나눠 진행하고 커밋도 그 단위로 나눈다 (한국어 conventional commit).
- 실제로 겪은 문제는 `docs/troubleshooting.md`에 문제, 원인, 해결, 배운 점으로 남긴다.
- 설계 결정은 `docs/design.md`, 실험 결과는 `docs/experiments.md`에 남긴다.
- 진행 단계는 README "로드맵"을 따른다. 현재 6단계 완료, 7단계(배포)는 준비까지 완료(`render.yaml`, docs/deploy.md). 공개 저장소 github.com/hocastle1999/group-settlement에 push했고 GitHub CI가 통과한다. 실제 배포(외부 MySQL, Render 전용 워크스페이스)는 사용자가 보류했다.
- 범위 결정: 인증은 그룹별 초대 링크 토큰(회원가입 없음), 화면은 REST API + 데모 한 페이지. 다중 통화는 "지출 시점 환율로 기준 통화에 환산, 원장은 통화 하나"(5단계).
- push와 저장소 설정 변경은 사용자에게 먼저 묻는다.
- 사용자와의 대화와 문서는 한국어로 작성한다. 코드 식별자는 영어, 주석은 한국어로 쓴다.

## 기술 스택
- Python 3.11 (`.python-version`)
- 코어(`app/ledger`)는 표준 라이브러리만 쓴다.
- API와 DB(3단계부터): FastAPI, MySQL 8.0(로컬 Windows 서비스 `MySQL80`), SQLAlchemy 2.x **Core**(ORM 아님), PyMySQL, Alembic, pydantic-settings(`.env`의 `DB_*`). smart-elevator와 같은 방식
- 테스트 클라이언트는 `httpx2` (Starlette 1.10부터 `httpx`는 deprecated)
- pytest, hypothesis, ruff, GitHub Actions

## 명령어 (Windows PowerShell, 프로젝트 루트 기준)
- 가상환경 Python: `.\.venv\Scripts\python.exe`
- 의존성 설치: `.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt`
- 테스트: `.\.venv\Scripts\python.exe -m pytest -q` (CI와 같은 고정 시드: `$env:CI=1` 후 실행). DB 테스트는 `group_settlement_test`를 쓴다.
- 서버: `.\.venv\Scripts\python.exe -m uvicorn app.main:app --no-proxy-headers` (데모 페이지 http://localhost:8000 , 문서 /docs). `--no-proxy-headers`를 빼면 uvicorn이 127.0.0.1에서 온 X-Forwarded-For를 믿어 빈도 제한을 위조로 피할 수 있다(docs/troubleshooting.md).
- DB 준비(최초 1회): root로 `scripts/init_db.sql` 실행(비밀번호를 바꿔서, 바꾼 사본은 `*.local.sql`) → `.env.example`을 `.env`로 복사 → `.\.venv\Scripts\alembic.exe upgrade head`
- 스키마 변경: `app/db/tables.py` 수정 → `.\.venv\Scripts\alembic.exe revision --autogenerate -m "설명"` → 생성 파일을 읽고 고친 뒤 `upgrade head` → `downgrade -1` → `upgrade head` 왕복, `alembic.exe check`로 모델과 DB 일치 확인 (CI도 `alembic check`)
  - `alembic.ini`는 ASCII만 쓴다 (Windows에서 cp949로 읽혀 한글이 깨진다).
  - autogenerate는 CHECK 내용 변화를 못 잡는다. 직접 쓰고, 이름은 `op.f("ck_...")`로 감싼다(이름 규칙 접두어가 두 번 붙는 것 방지).
  - 외래 키가 쓰는 인덱스는 테이블보다 먼저 `drop_index`할 수 없다(오류 1553). autogenerate도 이 순서로 만든다. downgrade는 `drop_table`만 남긴다 (docs/troubleshooting.md).
  - 마이그레이션은 앱 상수(통화 목록 등)를 import하지 않고 값을 파일에 적는다("그 시점의 스키마" 기록).
  - 기존 행이 있는 테이블에 NOT NULL 열을 추가할 때는 NULL 허용으로 추가 -> 값 채우기 -> NOT NULL (예: `8d41b6e2a9c3`의 seq).
  - MySQL DDL은 롤백되지 않는다. 마이그레이션이 실패하면 `alembic current`와 실제 테이블부터 확인한다.
  - 테스트 DB가 비어 있으면 downgrade 오류가 드러나지 않는다. 개발 DB에서도 왕복해 본다.
- 린트/포맷: `.\.venv\Scripts\ruff.exe check .` / `.\.venv\Scripts\ruff.exe format .`
- 커밋 전에는 테스트, 린트, 포맷 검사를 모두 통과시킨다. CI(`.github/workflows/ci.yml`)도 같은 검사를 한다.

## 구조
- `app/ledger/`: 원장 코어. 순수 파이썬. I/O, 시계, 전역 `random`을 쓰지 않는다. 같은 입력이면 같은 결과(결정성).
  - `money.py`: `Currency`(코드, 소수 자릿수), `Money`(Decimal, 불변). float 거부, 통화 단위보다 작은 값 거부. 통화가 다르면 `CurrencyMismatchError`. 최소 단위 정수는 `minor_units`, `from_minor`.
  - `split.py`: `allocate(total, weights, rotation)`(최대 나머지 방식, 동점은 멤버 ID(문자열) 순서를 rotation만큼 돌린 순서. DB는 분개 순번을 넘긴다), `SplitRule`(ABC) → `EqualSplit`, `RatioSplit`(정수 가중치), `FixedSplit`(합이 총액과 같아야 함). 결과의 합은 반드시 총액과 같다.
  - `ledger.py`: `Line`(금액은 항상 양수, 방향은 `Side`), `JournalEntry`(만들 때 균형 검사), `Ledger`(추가만, `post`, `reverse`, `balances`). 잔액 = 대변 합 - 차변 합, 합은 항상 0.
    - 분개를 고치거나 지우는 API를 만들지 않는다. 취소는 역분개(`REVERSAL`, `reverses`=원래 번호)로만. 한 번만 취소, 역분개는 취소 불가.
    - 분개는 `expense_entry`, `foreign_expense_entry`, `transfer_entry`, `reversal_of`로 만든다.
    - 외화 지출의 `JournalEntry.source`(`ForeignAmount`: 원래 금액, 환율)는 증빙이다. 만들 때 "원래 금액 x 환율의 반올림 = 대변 합"을 검사한다. 역분개에는 붙지 않는다.
  - `fx.py`: `convert`(총액 한 번 환산, ROUND_HALF_EVEN, 유효 숫자 60자리 문맥에서 곱함), 환율 = 원래 통화 1단위당 기준 통화 단위, 소수 10자리까지(넘으면 거부). 외화 정액 분할은 환산 총액을 원래 금액 비율로 배분한다(각자 환산 금지).
  - `money.py`의 `CURRENCIES`를 바꾸면 통화 CHECK 마이그레이션도 써야 한다. `alembic check`는 못 잡고 `test_db_currency_checks_match_app_currencies`가 잡는다.
  - `settle.py`: 최소 송금. `greedy_transfers`(힙, ≤ k-1번), `optimal_transfers`(합 0 묶음 최대화 비트마스크 DP + 묶음 안 그리디, O(2^k x k)), `settle(strategy=AUTO)`(k ≤ `DEFAULT_OPTIMAL_LIMIT`=16이면 최적). 입력 잔액 합이 0이 아니면 `UnbalancedBalancesError`. 한도 근거는 docs/experiments.md. DP를 바꾸면 `test_optimal_matches_brute_force`(완전 탐색 비교)를 통과해야 한다.
  - `errors.py`: `LedgerError` 하위 예외. HTTP 상태 코드는 `app/api/errors.py`에서 바꾼다.
  - 기록 전 검사는 공개 함수 `check_postable`, `check_reversible`, `check_reversal`. 메모리 `Ledger`와 DB 저장 계층이 같이 쓴다. 규칙을 바꾸면 여기만 바꾼다(SQL로 같은 규칙을 다시 쓰지 않는다).
- `app/db/`: 저장 계층 (3단계).
  - `config.py`(`DatabaseSettings`, `create_db_engine`, 연결마다 time_zone UTC), `tables.py`(Core 테이블, 이름 규칙).
  - 테이블: `settlement_groups`(`GROUPS`는 예약어, `last_seq` = 원장 버전, `audit_seq`/`audit_head` = 감사 로그 기준점), `audit_events`(6단계), `members`, `journal_entries`(`seq` = 그룹 안 순번), `entry_lines`, `idempotency_keys`, `settlements`, `settlement_transfers`(4단계 확정 정산). 금액 `DECIMAL(19,4)` + `CHECK (amount > 0)`. 역분개 대상 `UNIQUE`. 그룹 경계는 복합 외래 키 `(group_id, id)`.
  - **분개를 쓰는 트랜잭션은 첫 쿼리로 `lock_group`(그룹 행 `FOR UPDATE`)을 부른다.** 순번 = `last_seq + 1`, 같은 그룹 쓰기는 한 줄로 서서 순번 순서 = 커밋 순서가 된다. 잠금 전에 일반 SELECT를 하면 REPEATABLE READ 스냅숏이 잠금 대기 중의 커밋을 놓친다. 버전으로 `MAX(id)`를 쓰지 않는다(AUTO_INCREMENT 순서 ≠ 커밋 순서).
  - `store.py`: 쓰기는 `post_entry(engine, group, build, idempotency_key, request_hash)` 하나. `build(conn, seq)`는 그룹 잠금 안에서 불린다. 요청 검사(금액, 환율, 분할 규칙)는 잠금 전에 끝낸다. `build`가 코어 함수로 `JournalEntry`를 만들고, 검사 후 분개·줄·멱등성 키를 한 트랜잭션에 INSERT. 키 유니크 위반이면 롤백하고 처음 결과를 돌려준다(replay). 잔액은 `group_balances(conn, group)`(SQL SUM, 합 0 확인), 버전과 잔액을 같은 스냅숏에서 읽을 때는 `snapshot`.
  - 정산 확정 `confirm_settlement(engine, group, expected_version, strategy)`: 그룹 잠금 아래에서 버전 비교, 다르면 `StaleVersionError`(409, `current_version` 포함). `(group_id, version)` 유니크라 같은 버전 재확정은 처음 기록 반환.
  - 송금 완료(6단계): `completion_builder` + `post_entry(..., link=(settlement_id, line_no))`. 완료 = 그 줄을 가리키고 취소되지 않은 송금 분개. 재완료를 허용해야 해서 유니크 대신 **그룹 잠금 아래의 검사**로 중복을 막는다.
  - 감사 로그(6단계, `audit.py`): 그룹을 바꾸는 모든 쓰기는 같은 트랜잭션에서 `audit.append`를 부른다(그룹 잠금 필요). 새 쓰기 경로를 만들면 감사 로그도 남긴다. 해시할 내용(`_payload`)을 바꾸면 기존 체인 검증이 깨진다. 행위자 `X-Actor-Member-Id`는 인증이 아니다.
  - 리포트(6단계, `report.py`): `text()` SQL + 바인드 파라미터. 집계는 "먼저 GROUP BY, 나중에 JOIN"(멤버 LEFT JOIN 후 집계하면 계획이 흔들렸다, docs/troubleshooting.md). 쿼리를 바꾸면 `EXPLAIN ANALYZE`의 loops를 본다.
  - 코어 `MemberId` = `str(DB 멤버 id)`.
  - **원장 테이블에 UPDATE/DELETE를 하지 않는다.** 취소는 역분개 INSERT. `test_app_never_updates_or_deletes_ledger_rows`가 SQL을 가로채 확인한다.
  - 경쟁 상황은 DB 제약으로 막고, 제약 위반(MySQL 1062)을 도메인 예외로 번역한다(`AlreadyReversedError`, `DuplicateMemberError`). SELECT로 먼저 확인하는 것만으로는 동시 요청을 못 막는다.
- `app/api/`: REST (3단계). `auth.py`(초대 토큰: 응답에 한 번만, DB에는 SHA-256, `Authorization: Bearer`, 틀린 토큰과 없는 그룹은 같은 404), `schemas.py`(금액은 JSON 문자열만, 요청은 `extra="forbid"`, 분할은 `type` 판별 유니온), `routes.py`, `errors.py`(예외 → 상태 코드 표 하나).
  - 핸들러는 `def`(PyMySQL이 블로킹이라 스레드 풀에서). `async def`로 바꾸지 않는다.
  - 분개를 만드는 쓰기(지출, 송금, 취소)는 `Idempotency-Key` 필수. 요청 지문은 메서드 + 경로 + 정렬한 JSON 본문.
  - 새 예외를 추가하면 `app/api/errors.py`의 `STATUS_BY_ERROR`에 넣는다(없으면 500).
- `app/static/`: 데모 페이지(`index.html`, `app.js`). 그룹 id와 토큰은 URL fragment(`#g=..&t=..`). 사용자 입력은 `textContent`로만(innerHTML 금지, 테스트가 검사), 페이지에 CSP. 화면을 바꾸면 브라우저로 직접 눌러 확인한다.
- `app/config.py`: `AppSettings`(`SETTLE_OPTIMAL_LIMIT` 1 ~ 20, `RATE_LIMIT_*`, `TRUSTED_PROXY_HOPS`). `app/db/config.py`: `DB_SSL`(인증서 검증), `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`. 새 설정을 추가하면 `.env.example`과 README 환경 변수 표도 갱신한다.
- `app/api/rate_limit.py`: 쓰기 빈도 제한(슬라이딩 윈도 로그, 서버 메모리). 접속자 IP는 `client_ip`로만 구한다(X-Forwarded-For 왼쪽 값을 믿지 않는다). 테스트 환경은 conftest가 끄고 `test_rate_limit.py`가 따로 검사한다.
- `render.yaml`: 웹 서비스 하나(빈도 제한이 메모리에 있다). 시작 명령이 마이그레이션을 먼저 돈다. 시작 명령을 바꾸면 CI의 Smoke test도 같이 바꾼다. 배포는 이 프로젝트 전용 Render 워크스페이스에서 한다(기존 워크스페이스의 무료 시간을 다른 서비스와 나눠 쓰지 않게).
- `app/main.py`: `create_app(db_settings, settings)`. JSON 응답에 `charset=utf-8`을 붙이는 미들웨어(PowerShell 5.1 한글 깨짐, docs/troubleshooting.md).
- `scripts/compare_balances.py`: 잔액 계산 방식(SUM, 커버링 인덱스, 잔액 테이블)과 동시성(갱신 손실, 데드락, 확정 굶주림) 실험. **테스트 DB를 비운다**(테스트와 동시에 돌리지 않는다). 결과는 docs/experiments.md.
- `scripts/compare_settlement.py`: 그리디 vs 최적 실험(`python -m scripts.compare_settlement`). 난수는 `random.Random(seed)`만. 결과는 docs/experiments.md에 옮긴다.
- `tests/`: 예시 테스트 + hypothesis property 테스트. 불변식(분할 합 = 총액, 잔액 합 = 0)을 바꾸는 코드는 property 테스트를 반드시 통과해야 한다.
  - `tests/conftest.py`가 import 전에 `DB_NAME=group_settlement_test`(`TEST_DB_NAME`)를 넣는다. 세션 시작 때 마이그레이션 base → head. 이름이 `_test`로 끝나지 않는 DB는 거부한다.
  - 테스트마다 `DELETE FROM`으로 비운다(TRUNCATE는 테이블당 수백 ms, docs/troubleshooting.md). AUTO_INCREMENT가 되돌아가지 않으므로 "없는 id"는 10^15 같은 큰 값을 쓴다.
  - 남는 단위를 받는 멤버를 테스트에 이름으로 적지 않는다. 멤버 ID는 문자열 순서라 id 자릿수가 바뀌면("99" -> "100") 순서가 달라진다. `test_api.py`의 `leftover_taker`처럼 규칙으로 계산한다.
  - `test_fx.py`: 환산을 정확한 분수(`Fraction`) 계산과 비교. `test_multi_currency.py`: 외화 지출 API.
  - `test_audit.py`: 감사 로그와 변조 감지(DB 직접 수정). `test_report.py`: 리포트를 파이썬 재계산과 비교. `test_settlements.py`의 뒷부분: 송금 완료.
  - `test_concurrency.py`: 그룹 잠금, 순번, 섞인 작업 스트레스(확정 계획 = 그 버전까지 다시 계산한 계획). `test_settlements.py`: 정산 확정.
  - 동시성 테스트의 장벽/이벤트는 특정 실행 순서를 전제한다. 잠금 구조를 바꾸면 그 위치를 다시 정한다 (docs/troubleshooting.md).
  - `test_store.py`: DB 제약 직접 검사, SQL 잔액 = 메모리 원장 잔액(무작위 거래), 멱등성(동시 요청 포함). `test_api.py`: 엔드포인트, 인증, 오류 매핑.
  - CI는 MySQL 8.0 서비스 컨테이너를 띄우고 테스트 뒤 `alembic check`를 돌린다.

## 규칙
- 돈은 float로 다루지 않는다. API 금액은 문자열, DB는 `DECIMAL`, 계산은 `Money`.
- 비밀 정보는 `.env`에만 둔다. 코드, 문서, 커밋에 넣지 않는다. 새 DB 설정을 추가하면 `.env.example`, README 환경 변수 표, CI의 `env`도 갱신한다.
- 의존성을 추가하면 버전을 고정해 `requirements.txt`(실행용) 또는 `requirements-dev.txt`(개발용)에 적는다.
- 파일은 `encoding="utf-8"`로 연다. PowerShell에서 API를 부를 때는 `-ContentType "application/json; charset=utf-8"`.
- 인덱스를 추가하면 데이터를 충분히 넣고 `EXPLAIN`으로 실제로 쓰이는지 확인한다(행이 몇 개뿐이면 옵티마이저가 다른 인덱스를 고른다).
