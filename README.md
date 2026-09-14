# slack-cli-agent

로컬에 설치된 CLI 에이전트를 슬랙에 연결하는 범용 어댑터.

슬랙에서 받은 말을 CLI 에이전트에게 전달하고, 그 답을 슬랙 표기로 되돌린다.
설정 파일만으로 자기 봇을 만든다. 코드에 조직 고유값을 두지 않는다.

상태 — 설계 단계. 구현 전이다.

## 문서

| 문서 | 내용 |
|---|---|
| [01-source-analysis.md](docs/01-source-analysis.md) | 참조 원본 분석. 정책과 실측 근거 |
| [02-PRD.md](docs/02-PRD.md) | 제품 요구사항 |
| [03-TRD.md](docs/03-TRD.md) | 기술 설계 |

## 참조 원본

`~/Projects/원본 저장소` — 한 조직 전용으로 동작 중인 선행 구현.
모든 정책의 출처다. 이 저장소로 복사하지 않는다.

## 원칙

- **구조만 새로 만든다.** 동작과 기술 선택은 원본을 그대로 따른다. 기능을
  추가하지 않는다. 근거는 TRD 0절
- 민감 정보는 상태 디렉터리에만 둔다. 저장소에는 예시만 둔다
- 엔진은 교체 가능한 계층이다. 새 엔진 추가에 본체 변경이 없다
- 기본 권한은 읽기 전용이다
- 조회 실패와 부재를 구분한다
- 지침으로 막히지 않는 것은 코드가 검출한다

## 참고 자료

`~/Jobs/tasks/원본-redesign-gemini.md` — 제미나이의 재설계안. 수용·부분 수용·
비수용 판정은 TRD 0절과 0-1절에 반영돼 있다.

## 구현 현황

    src/slack_cli_agent/config/    Profile, EngineSpec, ChannelRegistry,
                                   RuntimeSettings, StatePaths
    src/slack_cli_agent/core/      RequestContext, Outcome, 예외 계층
    src/slack_cli_agent/storage/   Database, SqliteRepository, 마이그레이션
    src/slack_cli_agent/jobs/      JobQueue(Protocol), SqliteJobQueue,
                                   WorkerHeartbeat
    tests/                         격리 conftest 와 단위 테스트

테스트는 `python3 -m pytest tests -q` 로 실행한다. `tests/conftest.py` 가 실제 홈
경로 접근을 차단하므로 시험이 운영 상태 파일을 건드리지 않는다.

**아직 커밋하지 않는다.** 사용자 지시다. `.gitignore` 는 먼저 두었고, 실제 프로필과
프롬프트는 저장소 밖 상태 디렉터리에 둔다.
