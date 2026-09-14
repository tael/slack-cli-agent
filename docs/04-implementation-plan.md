# 구현 계획 — TDD 와 병렬 분해

작성 2026-09-14. TRD 13절의 단계 구분을 실행 단위로 나눈 것이다.

---

## 1. 규칙

### TDD

    1. 테스트를 먼저 쓴다
    2. pytest 로 실행해 실패를 확인한다. 실패 사유가 "기능 없음" 인지 본다
       실행은 `python3 -m pytest tests -q` 다. pyproject 의 addopts 가 -n 4 로
       병렬 실행한다. 테스트는 실행 순서와 워커 배정에 의존하면 안 된다
    3. 통과시키는 최소 구현을 쓴다
    4. 다시 실행해 통과와 기존 테스트 유지를 확인한다
    5. 통과 상태에서만 정리한다

**실패를 관측하지 않은 테스트는 무엇을 검출하는지 증명되지 않았다.** 구현을 먼저
쓴 코드는 지우고 다시 쓴다. 2026-09-14 에 기반 4개 패키지가 이 규칙으로 폐기됐다.

**테스트를 통과시키려고 테스트를 고치지 않는다.** 구현을 고친다. 테스트가 틀렸다고
판단하면 왜 틀렸는지 먼저 적는다.

### 이식 대상의 TDD

이식 분류는 "원본 함수 본문을 수정하지 않는다" 가 원칙이라 최소 구현 규칙과
충돌한다. 특성화 테스트로 푼다.

    1. 원본 함수에 넣을 입력과 기대 출력을 테스트에 적는다
       기대 출력은 원본을 실행해 얻는다. 손으로 짐작하지 않는다
    2. 새 모듈이 없어 실패하는 것을 확인한다
    3. 원본 함수 본문을 그대로 옮긴다. 클래스로 감싸기만 한다
    4. 통과를 확인한다

**입력 표본은 원본의 `audit.jsonl` 과 실제 슬랙 메시지에서 가져온다.** 정규식은
특히 그대로 옮긴다 — `REACTION_WORD` 의 대안에 `+` 를 붙이지 않는 이유가 원본
주석에 있다.

### OOP

    상태와 동작을 클래스로 묶는다. 모듈 전역 변수를 두지 않는다
    의존은 생성자로 주입한다. 모듈 수준에서 다른 모듈을 생성하지 않는다
    import 에 부수 효과를 두지 않는다
    계약과 구현을 분리한다 — 공유 기본 구현이 있으면 ABC, 없으면 Protocol
    값 객체는 frozen dataclass 로 둔다

### 코드 안의 자연어

한글로 쓴다. 비유와 의인화를 쓰지 않는다. 실측 근거가 있는 상수는 근거를 주석에
남긴다.

---

## 2. 웨이브 구분

의존 방향으로 나눴다. 같은 웨이브 안의 단위는 서로 의존하지 않아 병렬로 진행한다.

### 웨이브 0 — 기반 (단독)

    config/     Profile, EngineSpec, ChannelRegistry, RuntimeSettings, StatePaths
    core/       RequestContext, Outcome, 예외 계층
    storage/    Database, SqliteRepository, 마이그레이션
    jobs/       JobQueue(Protocol), SqliteJobQueue, WorkerHeartbeat

다른 모든 단위가 이것을 의존한다. 계약이 여기서 어긋나면 뒤 웨이브 전부가
어긋나므로 병렬로 나누지 않는다.

### 웨이브 1 — 독립 도메인 (5개 병렬)

| 단위 | 범위 | 분류 |
|---|---|---|
| W1-A | `render/` — markdown, splitter, verifier, blocks | 이식 |
| W1-B | `slack/gate.py`, `guard/` — 응답 게이트와 후처리 가드 | 이식 + 재구성 |
| W1-C | `auth/`, `prompt/` — 권한 계층과 프롬프트 조립 | 재구성 |
| W1-D | `engine/` — Engine ABC, Registry, Claude, Codex, Switcher, Fallback | 재구성 |
| W1-E | `session/`, `observability/audit.py`, `observability/notices.py` | 재구성 |

### 웨이브 2 — 슬랙 계층 (3개 병렬)

| 단위 | 범위 |
|---|---|
| W2-A | `slack/` — gateway, listener, publisher, reactions, history, transcript, attachments |
| W2-B | `preflight/`, `admin/`, `plugin/` |
| W2-C | `reliability/` — catchup, health, watchjobs, dedup |

W2-A 는 W1-A 의 `render` 를, W2-C 는 W2-A 의 `history` 를 의존한다. W2-C 는
`HistoryReader` 대역으로 진행하고 통합에서 실물로 바꾼다.

### 웨이브 3 — 후처리 (2개 병렬)

| 단위 | 범위 |
|---|---|
| W3-A | `review/` — ReviewTask 와 부검·추적·서식 점검 |
| W3-B | `observability/progress.py`, `cli.py` |

### 웨이브 4 — 통합 (단독)

    core/application.py    조립
    core/ingress.py        수신 프로세스
    core/worker.py         실행 프로세스

전부를 의존한다. 통합 시점에 대역을 실물로 바꾸고 전체 테스트를 실행한다.

---

## 3. 병렬 작업자에게 주는 제약

- **기반 패키지를 수정하지 않는다.** 계약이 안 맞으면 고치지 말고 보고한다
- **다른 웨이브 단위의 디렉터리를 건드리지 않는다**
- **원본 `bot.py` 는 읽기 전용이다.** 운영 중인 봇이라 수정하지 않는다
- **RED 로그를 보고에 포함한다.** 실패 출력을 인용하지 못하면 TDD 를 안 한 것이다
- **테스트를 약하게 고쳐 통과시키지 않는다**

---

## 4. 검증

각 웨이브 종료 시 전체 테스트를 실행한다. 웨이브 4 이후 추가로 확인한다.

    저장소 전체에서 조직 고유값 grep 결과 0건
    워커를 SIGKILL 로 죽였을 때 그 작업이 다시 처리된다
    원본 출력과 이식 모듈 출력의 바이트 단위 대조 불일치 0
