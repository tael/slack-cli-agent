# slack-cli-agent

로컬에 설치된 CLI 에이전트를 슬랙에 연결하는 범용 어댑터.

슬랙에서 받은 말을 CLI 에이전트에게 전달하고, 그 답을 슬랙 표기로 되돌린다.
설정 파일만으로 자기 봇을 만든다. 코드에 조직 고유값을 두지 않는다.

- 엔진은 claude, codex, gemini 를 지원한다. 주 엔진이 막히면 fallback 엔진으로 넘긴다
- 소켓 모드로 접수하는 ingress 프로세스와 작업 큐를 소비하는 worker 프로세스가 나뉘어 있다
- 채널마다 모델·응답 정책·권한을 따로 준다
- 기본 권한은 읽기 전용이다. 쓰기 도구는 프로필과 채널 설정으로 연다

## 요구 사항

- Python 3.11 이상
- 붙일 CLI 에이전트 바이너리 (claude, codex, gemini 중 하나 이상)
- 슬랙 앱의 봇 토큰(`xoxb-`)과 앱 토큰(`xapp-`). 앱 토큰은 소켓 모드용이다

## 설치

    git clone https://github.com/tael/slack-cli-agent.git
    cd slack-cli-agent
    python3 -m venv .venv
    .venv/bin/pip install -e ".[dev]"

`[dev]` 없이 설치하면 실행은 되지만 테스트 도구(pytest, pytest-xdist, mypy, ruff)가
빠진다.

## 빠른 시작

1. 프로필과 상태 디렉터리를 만든다.

        .venv/bin/slack-cli-agent init --name mybot

   `<프로필 디렉터리>/mybot.json` 과 상태 디렉터리 `~/.mybot` 이 생긴다.
   프로필 항목은 [profiles/example.example.json](profiles/example.example.json) 을 참고한다.
   엔진 바이너리 경로, 모델, `owner_user_id`, `troubleshoot_channel` 을 자기 값으로 채운다.

2. 슬랙 토큰을 둔다. 상태 디렉터리의 `credentials.json` 에 넣거나
   `SLACK_BOT_TOKEN`·`SLACK_APP_TOKEN` 환경변수로 준다.

        cat > ~/.mybot/credentials.json <<'EOF'
        {"bot_token": "xoxb-...", "app_token": "xapp-1-..."}
        EOF
        chmod 600 ~/.mybot/credentials.json

3. 슬랙 앱을 만든다. [slack-apps/_template.json](slack-apps/_template.json) 이
   필요한 스코프와 이벤트 구독을 담은 매니페스트 원형이다. 소켓 모드를 켜고
   워크스페이스에 설치한다.

4. 기동 전 점검과 DB 마이그레이션을 돌린다.

        .venv/bin/slack-cli-agent preflight --profile mybot
        .venv/bin/slack-cli-agent migrate --profile mybot

5. 두 프로세스를 띄운다.

        .venv/bin/slack-cli-agent ingress --profile mybot
        .venv/bin/slack-cli-agent worker --profile mybot

봇을 채널에 초대하고 멘션하면 답한다.

## 명령

| 명령 | 하는 일 |
|---|---|
| `init` | 프로필 뼈대와 상태 디렉터리 구조를 만든다 |
| `preflight` | 기동 전 점검을 실행한다 |
| `migrate` | DB 마이그레이션을 실행한다 |
| `channels` | 등록된 채널 목록을 본다 |
| `ingress` | 슬랙 이벤트 접수 프로세스를 띄운다 |
| `worker` | 작업 큐를 소비하는 워커 프로세스를 띄운다 |
| `learn` | 하루치 응답 기록을 분석해 학습 제안을 만들고 반영한다 |
| `rewrite` | 이미 올린 메시지를 교정본 파일 내용으로 바꿔 쓴다 |
| `web` | 설정과 지표를 보는 웹 콘솔을 띄운다 |

각 명령의 인자는 `slack-cli-agent <명령> --help` 로 본다.

## 개발

    .venv/bin/pytest tests -q                     # 단위 시험
    .venv/bin/pytest tests/real_cli -m real_cli   # 실제 CLI 를 부르는 시험. 따로 지정해야 돈다
    .venv/bin/ruff check src tests
    .venv/bin/mypy src

`tests/conftest.py` 가 실제 홈 경로 접근을 차단하고 자격 환경변수를 비운다.
시험이 운영 상태 파일을 건드리거나 실행한 셸의 토큰을 읽지 않는다.

## 구조

    src/slack_cli_agent/config/    Profile, EngineSpec, ChannelRegistry, StatePaths
    src/slack_cli_agent/core/      요청 파이프라인, RequestContext, 예외 계층
    src/slack_cli_agent/engine/    claude·codex·gemini 어댑터, fallback 전환
    src/slack_cli_agent/slack/     접수, 표기 변환, 자격, 응답 정책
    src/slack_cli_agent/jobs/      작업 큐와 워커 하트비트
    src/slack_cli_agent/storage/   SQLite 저장소와 마이그레이션
    src/slack_cli_agent/web/       설정·지표 웹 콘솔
    tests/                         격리 conftest 와 단위 시험

새 엔진을 붙일 때 본체를 고치지 않는다. `engine/base.py` 를 구현하고
`engine/registry.py` 에 등록한다.

## 설정과 비밀값

민감 정보는 저장소에 두지 않는다. 프로필(`profiles/*.json`), 슬랙 앱 매니페스트
정본(`slack-apps/*.json`), 프롬프트(`prompts/*.md`), 페르소나(`persona/`)는
`.gitignore` 로 제외돼 있고 저장소에는 예시와 템플릿만 둔다.

## 문서

설계 문서는 [docs/](docs/) 에 있다. 한국어로 쓰였다.

| 문서 | 내용 |
|---|---|
| [02-PRD.md](docs/02-PRD.md) | 제품 요구사항 |
| [03-TRD.md](docs/03-TRD.md) | 기술 설계 |
| [엔진-보장-축.md](docs/엔진-보장-축.md) | 엔진별로 무엇이 보장되고 무엇이 안 되는지 |
| [패키징-경계.md](docs/패키징-경계.md) | 패키지 기본값과 상태 디렉터리 override 경계 |

## 라이선스

MIT. [LICENSE](LICENSE) 참고.
