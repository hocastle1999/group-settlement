# CLAUDE.md

모임 경비 정산 서비스. 백엔드 포트폴리오용 프로젝트(3번째).
1번(reservation-api)은 트랜잭션과 락, 2번(smart-elevator)은 실시간 처리와 알고리즘을 보여 준다.
이 프로젝트는 돈을 정확하게 다루는 설계(이중기입 원장, 금액 정밀도, 최소 송금)를 보여 준다.

## 목적과 작업 방식
- 사용자가 면접에서 모든 코드를 설명할 수 있어야 한다.
  - 새 개념을 도입할 때는 왜 그렇게 하는지, 다른 방법과 비교해 짧게 설명한다.
  - 기능 단위로 작게 나눠 진행하고 커밋도 그 단위로 나눈다 (한국어 conventional commit).
- 실제로 겪은 문제는 `docs/troubleshooting.md`에 문제, 원인, 해결, 배운 점으로 남긴다.
- 설계 결정은 `docs/design.md`를 갱신한다.
- 진행 단계는 README "로드맵"을 따른다. 현재 1단계 완료.
- 범위 결정: 인증은 그룹별 초대 링크 토큰(회원가입 없음), 화면은 REST API + 데모 한 페이지, 다중 통화는 5단계.
- GitHub 저장소 생성과 push는 사용자에게 먼저 묻는다.
- 사용자와의 대화와 문서는 한국어로 작성한다. 코드 식별자는 영어, 주석은 한국어로 쓴다.

## 기술 스택
- Python 3.11 (`.python-version`)
- 코어(`app/ledger`)는 표준 라이브러리만 쓴다.
- 3단계부터: FastAPI, MySQL 8.0(로컬 Windows 서비스 `MySQL80`), SQLAlchemy 2.x Core, Alembic (smart-elevator와 같은 방식)
- pytest, hypothesis, ruff, GitHub Actions

## 명령어 (Windows PowerShell, 프로젝트 루트 기준)
- 가상환경 Python: `.\.venv\Scripts\python.exe`
- 의존성 설치: `.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt`
- 테스트: `.\.venv\Scripts\python.exe -m pytest -q` (CI와 같은 고정 시드: `$env:CI=1` 후 실행)
- 린트/포맷: `.\.venv\Scripts\ruff.exe check .` / `.\.venv\Scripts\ruff.exe format .`
- 커밋 전에는 테스트, 린트, 포맷 검사를 모두 통과시킨다. CI(`.github/workflows/ci.yml`)도 같은 검사를 한다.

## 구조
- `app/ledger/`: 원장 코어. 순수 파이썬. I/O, 시계, 전역 `random`을 쓰지 않는다. 같은 입력이면 같은 결과(결정성).
  - `money.py`: `Currency`(코드, 소수 자릿수), `Money`(Decimal, 불변). float 거부, 통화 단위보다 작은 값 거부. 통화가 다르면 `CurrencyMismatchError`. 최소 단위 정수는 `minor_units`, `from_minor`.
  - `split.py`: `allocate`(최대 나머지 방식, 동점은 멤버 ID 순), `SplitRule`(ABC) → `EqualSplit`, `RatioSplit`(정수 가중치), `FixedSplit`(합이 총액과 같아야 함). 결과의 합은 반드시 총액과 같다.
  - `ledger.py`: `Line`(금액은 항상 양수, 방향은 `Side`), `JournalEntry`(만들 때 균형 검사), `Ledger`(추가만, `post`, `reverse`, `balances`). 잔액 = 대변 합 - 차변 합, 합은 항상 0.
    - 분개를 고치거나 지우는 API를 만들지 않는다. 취소는 역분개(`REVERSAL`, `reverses`=원래 번호)로만. 한 번만 취소, 역분개는 취소 불가.
    - 분개는 `expense_entry`, `transfer_entry`, `reversal_of`로 만든다.
  - `errors.py`: `LedgerError` 하위 예외. 3단계에서 HTTP 상태 코드로 바꾼다.
- `tests/`: 예시 테스트 + hypothesis property 테스트. 불변식(분할 합 = 총액, 잔액 합 = 0)을 바꾸는 코드는 property 테스트를 반드시 통과해야 한다.
