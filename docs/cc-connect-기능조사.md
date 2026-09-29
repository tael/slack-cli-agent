# chenhg5/cc-connect 기능 전수 조사

`slack-cli-agent`와 목적이 겹치는 오픈소스 프로젝트
[chenhg5/cc-connect](https://github.com/chenhg5/cc-connect)의 기능을 확인한
기록이다. 재사용 가능 여부와 설계 참고 대상을 판단하는 근거로 쓴다.

근거 자료 — GitHub API로 조회한 `README.md`, `docs/usage.md`(1269줄),
`docs/slack-feature-inventory.md`, `cmd/cc-connect/` 디렉터리 파일 목록,
`config.example.toml`(2338줄, 목차만 확인). 조사 시각 2026-09-26.
저장소를 clone 하지 않고 문서와 파일 목록만 봤으므로 실제 동작은 검증하지
않았다 — 문서에 적힌 것과 실제 동작이 다를 가능성은 남는다.

---

## 1. 프로젝트 개요

- 언어: Go 1.22+. 설치는 npm/Homebrew/바이너리/소스 빌드.
- 라이선스: MIT.
- 규모: 스타 15665개, 활발히 유지보수(2026-09-26 확인 시점 최신 버전
  v1.5.1-beta.1, 최근 16개 PR 병합).
- 한 줄 정의: 로컬에 설치된 AI 코딩 에이전트(Claude Code, Codex 등)를 여러
  메신저 플랫폼에 연결하는 브리지.

---

## 2. 지원 에이전트

`cmd/cc-connect/plugin_agent_*.go` 파일 기준 15종의 에이전트 플러그인이
있다. README의 "10+ AI Agents" 표기보다 실제 플러그인 수가 많다.

    acp          Agent Client Protocol — 이 프로토콜을 구현한 임의의 CLI
    antigravity  Antigravity
    claudecode   Claude Code
    codex        OpenAI Codex
    copilot      GitHub Copilot
    cursor       Cursor Agent
    devin        Devin (Cognition) — ACP 경유
    gemini       Google Gemini CLI
    iflow        iFlow CLI
    kimi         Kimi CLI (Moonshot)
    opencode     OpenCode (Crush)
    pi           Pi (Cursor Background Agent)
    qoder        Qoder CLI
    reasonix     Reasonix
    tmux         tmux 세션에 붙는 범용 어댑터

ACP 플러그인 덕에 OpenClaw처럼 ACP를 구현한 에이전트는 별도 플러그인 없이
붙는다(`docs/usage.md` FAQ, issue #501).

---

## 3. 지원 플랫폼

`cmd/cc-connect/plugin_platform_*.go` 기준 20종. README의 "13 Chat
Platforms" 표보다 실제로는 더 많다.

    cloud_web       cc-connect 자체 웹 채팅
    dingtalk        DingTalk
    discord         Discord
    feishu          Feishu(Lark)
    googlechat      Google Chat
    line            LINE
    matrix          Matrix
    max             Max
    qq              QQ(NapCat/OneBot 비공식 브리지)
    qqbot           QQ Bot(공식)
    slack           Slack
    telegram        Telegram
    tuitui          Tuitui
    webex           Webex
    wecom           WeChat Work
    weibo           Weibo
    weixin          개인 위챗(ilink)
    wps_agentspace  WPS Agentspace
    wps_xiezuo      WPS Xiezuo
    yuanbao         Yuanbao

대부분 공인 IP 없이 WebSocket/Socket Mode/Long Polling으로 연결된다.
예외는 LINE(웹훅, 공인 URL 필요)과 WeChat Work 웹훅 모드.

Slack은 Socket Mode로 연결하고 공인 IP가 필요 없다.

---

## 4. 세션 관리

채널·사용자마다 독립 세션을 유지하고 슬래시 명령으로 조작한다.

    /new [name]      새 세션 시작
    /list            세션 목록
    /switch <id>     세션 전환
    /current         현재 세션 정보
    /history [n]     최근 n개 메시지(기본 10, 항목당 최대 길이는
                     [display].history_max_len, 기본 1000)
    /usage           계정/모델 사용량(지원 시)
    /stop            현재 실행 중단
    /help            도움말

**유휴 시 자동 세션 회전**(`reset_on_idle_mins`, 기본 30분) — 오래
방치된 세션에 새 메시지가 오면 자동으로 새 세션을 시작한다. 이전 세션은
지워지지 않고 `/list`·`/switch`로 접근 가능하다. 0으로 설정하면 이전
동작(항상 이어가기)으로 되돌아간다.

**모델 전환은 세션을 보존한다** — `/model`로 모델을 바꿔도 대화 맥락이
유지되고 추가 토큰 비용이 없다. 단 같은 프로젝트를 여러 플랫폼이 공유하면
모델 변경이 전체에 적용된다.

---

## 5. 권한 모드

에이전트별로 `/mode`로 전환 가능한 권한 모드가 다르다.

| 에이전트 | 모드 | 동작 |
|---|---|---|
| Claude Code | default | 모든 도구 호출에 승인 필요 |
| | acceptEdits/edit | 파일 편집 자동 승인 |
| | auto | Claude가 승인 필요 여부를 판단 |
| | plan | 계획만 세우고 실행 안 함 |
| | yolo/bypassPermissions | 전체 자동 승인 |
| Codex | suggest | 신뢰된 명령만 승인 없이 실행 |
| | auto-edit | 모델이 승인 필요 여부 판단 |
| | full-auto | 샌드박스 안에서 자동 승인 |
| | yolo | 승인·샌드박스 전부 우회 |
| Cursor Agent | default | 작업공간 신뢰, 도구 전 확인 |
| | force/yolo | 전체 자동 승인 |
| | plan | 읽기 전용 분석 |
| | ask | 읽기 전용 문답 |
| Gemini CLI | default | 승인 요청 |
| | auto_edit/edit | 편집 자동 승인 |
| | yolo | 전체 자동 승인 |
| | plan | 읽기 전용 계획 |
| Qoder/OpenCode/iFlow | default/yolo | 표준 권한 / 전체 생략 |

---

## 6. API 프로바이더·모델 관리

- 프로젝트 하나에 프로바이더 여러 개를 등록하고 재기동 없이 런타임에서
  전환한다(`/provider switch <name>`). Anthropic, MiniMax, Bedrock 등
  OpenAI 호환·자체 base_url 프로바이더를 다 등록할 수 있다.
- CLI로도 관리한다 — `cc-connect provider add/list/remove/import`
  (`import`는 `cc-switch`에서 가져온다).
- 에이전트별 환경변수 매핑이 고정돼 있다(Claude Code →
  `ANTHROPIC_API_KEY`/`ANTHROPIC_BASE_URL`, Codex →
  `OPENAI_API_KEY`/`OPENAI_BASE_URL` 등).
- 프로바이더마다 `[[providers.models]]`로 별칭(alias)을 등록해 두면
  `/model`이 API 호출 없이 그 목록을 즉시 보여준다. 없으면 프로바이더
  API에서 조회하거나 내장 목록으로 대체한다.

---

## 7. 작업 디렉터리 전환 (`/dir`, `/cd`)

- `/dir <path>`로 다음 세션이 시작할 디렉터리를 바꾼다. 상대경로·절대경로·
  히스토리 인덱스·`-`(이전 디렉터리)를 지원한다.
- **관리자 전용 명령이다.** `config.toml`의 `[[projects]]` 아래
  `admin_from`에 사용자 ID를 등록해야 쓸 수 있다(`[projects.platforms.options]`
  아래 두면 무시된다 — 실제로 이 오배치가 반복 이슈였던 것으로 보인다).
- `/dir reset`으로 설정된 `work_dir`로 복귀하고 `data_dir/projects/
  <project>.state.json`에 저장된 override를 지운다.

---

## 8. OS 사용자 격리 (`run_as_user`)

Linux/macOS에서 프로젝트별로 에이전트를 다른 유닉스 사용자 권한으로
실행해 cc-connect 실행 계정과 파일시스템을 격리한다. 현재 Claude Code만
지원.

- 대상 사용자는 감독 계정으로부터 비밀번호 없는 sudo를 받아야 하고, 자기
  sudo 권한은 없어야 하며, `work_dir`에 대한 읽기/쓰기 권한과 자기 몫의
  `~/.claude/settings.json`이 있어야 한다.
- `claude.ai` OAuth로 인증했다면 대상 사용자의
  `~/.claude/.credentials.json`을 감독 계정 사본에 심볼릭 링크로 걸어
  토큰 갱신을 동기화한다.
- 기동 전 감사 명령 `cc-connect doctor user-isolation`이 go/no-go 사전
  점검 3개와 격리 프로브를 돌려 대상 사용자가 무엇을 읽을 수 있는지
  보고한다. 게이트 하나라도 실패하거나 계정 간 유출이 감지되면 기동을
  거부한다.
- `run_as_env`로 sudo 경계를 넘길 환경변수 허용 목록을 확장할 수 있다
  (기본은 `PATH`, `LANG`, `LC_*`, `TERM`).

이 항목은 slack-cli-agent의 R4(기본 읽기 전용)·보안 요구와 방향은 같지만
접근 방식이 다르다 — cc-connect는 OS 계정 분리로 격리하고, slack-cli-agent
PRD는 도구 화이트리스트(owner/trusted/general 계층)로 격리한다.

---

## 9. Claude Code Router 연동

[Claude Code Router](https://github.com/musistudio/claude-code-router)를
붙여 요청을 여러 모델 프로바이더로 라우팅할 수 있다.
`[projects.agent.options]`에 `router_url`/`router_api_key`를 설정하는
방식이다.

---

## 10. Claude Code PermissionRequest 훅 — 이중 실행 처리

Claude Code의 `settings.json`에 등록된 PermissionRequest 훅을 cc-connect가
그대로 존중한다. 다만 cc-connect가 Claude Code를
`--permission-prompt-tool stdio`로 띄우는 구조상 Claude Code 자체의 훅
실행 결과는 프로토콜에 먹혀 버려진다. 그래서 **cc-connect가 훅 정의를
`settings.json`에서 다시 읽어 독립적으로 재실행**한다 — 요청 1건당 훅이
2번 실행되는 구조다.

규칙 기반 훅은 문제없지만 LLM 호출 훅은 첫 실행이 낭비다. 그래서
cc-connect가 Claude Code 서브프로세스 환경에
`CC_CONNECT_PERMISSION_HOOK_SKIP=1`을 심어 두고, 훅 스크립트가 이 값을
보면 즉시 종료하도록 가드를 넣게 안내한다(cc-connect 자신이 훅을 실행할
때는 이 변수를 벗기고 실행한다).

이건 cc-connect의 구현 제약이 낳은 우회책이지, 표준 동작이 아니다. 근본
원인은 Claude Code CLI의 stdout이 permission-prompt-tool 프로토콜에
전부 소비돼 훅 출력이 안 보이는 것이다.

---

## 11. 음성 — STT/TTS

- **STT(음성 메시지 → 텍스트)**: Feishu, WeChat Work, Telegram, LINE,
  Discord, Slack 지원. OpenAI 또는 Groq Whisper API와 `ffmpeg`가 필요.
- **TTS(답변 → 음성)**: Feishu/Lark, DingTalk, Telegram, Max, Weixin처럼
  오디오 전송을 구현한 플랫폼에서 지원. 프로바이더는 qwen, openai,
  minimax, mimo, espeak, pico, edge 중 선택. 에이전트별로 다른 음성을
  지정할 수 있다(`[tts.agents.<name>]`).
- TTS 모드는 `voice_only`(사용자가 음성으로 보낼 때만 음성 답장)와
  `always`(항상 음성 답장) 둘이고 `/tts` 명령으로 런타임 전환한다.

Slack은 STT는 지원 목록에 있으나 TTS 지원 목록에는 없다 — 즉 Slack에서는
음성 메시지를 텍스트로 바꿔 받을 수는 있어도 음성으로 답장을 받지는
못한다(문서 기준. 실측 안 함).

---

## 12. 첨부파일·음성 송신 (`cc-connect send`)

에이전트가 로컬에 만든 스크린샷·PDF·리포트 같은 파일을 대화방에 직접
돌려보내는 기능. **현재 지원 플랫폼은 Feishu와 Telegram뿐이다.** Slack은
이 기능 지원 목록에 없다.

```bash
cc-connect send --image /path/chart.png
cc-connect send --file /path/report.pdf
cc-connect send --tts "합성 음성으로 보낼 문장"
```

- 시스템 프롬프트를 자동 주입하지 않는 에이전트는 `/bind setup` 또는
  `/cron setup`을 한 번 실행해 cc-connect 사용법을 프로젝트 메모리
  파일에 새겨야 한다.
- `attachment_send = "off"`로 이 기능만 끌 수 있다(일반 텍스트 답장에는
  영향 없음).
- 첨부 1건당 기본 50MiB 상한, `max_attachment_size_mb` 또는
  `CC_MAX_ATTACHMENT_SIZE_MB` 환경변수로 조정. 플랫폼 자체 상한과 겹치면
  더 작은 쪽이 적용된다.
- 활성 세션이 없으면(대화 맥락이 없으면) 실행이 실패한다.

---

## 13. 스케줄 작업 (Cron)

```
/cron                                       전체 작업 목록
/cron add <분> <시> <일> <월> <요일> <프롬프트>   작업 생성
/cron del <id>                              삭제
/cron enable/disable <id>                   켜기/끄기
```

CLI로도 관리하고(`cc-connect cron add/list/edit/exec/del`), Claude Code는
"매일 오전 6시에 깃허브 트렌딩 요약해줘" 같은 자연어를 직접 받아 크론
작업으로 등록한다. 다른 에이전트는 `/cron setup`으로 안내 문구를 먼저
심어야 한다. 실행마다 새 세션을 쓸지(`--session-mode new-per-run`) 기존
세션을 재사용할지 선택 가능하고, 실행 타임아웃 기본 30분.

---

## 14. 셸 설정

`/shell` 명령, 크론 exec 작업, 훅, 웹훅 exec가 전부 같은 셸 설정을
공유한다. 기본은 Unix에서 `sh`, Windows에서 `powershell.exe`. 전역 또는
프로젝트별로 `sh`/`bash`/`zsh`/`fish`/`cmd`/`powershell.exe`/`pwsh` 중
바꿀 수 있고, `shell_profile`로 매 실행 전에 `~/.zshrc` 같은 프로파일을
소싱한다.

---

## 15. 멀티봇 릴레이

같은 그룹챗에 여러 프로젝트(에이전트)를 동시에 바인딩해 서로 대화시킨다.

```
/bind claudecode      claudecode 프로젝트 추가
/bind gemini          gemini 프로젝트 추가
```

```bash
cc-connect relay send --to gemini "이 설계 어떻게 생각해?"
```

---

## 16. 데몬 모드

```bash
cc-connect daemon install --config ~/.cc-connect/config.toml
cc-connect daemon start/stop/restart/status
cc-connect daemon logs [-f]
cc-connect daemon uninstall
```

백그라운드 상시 서비스로 등록·관리하는 단일 CLI다. slack-cli-agent
PRD의 "운영 명령이 하나의 CLI에 모인다"(R9-1) 요구와 방향이 같다 —
cc-connect는 이미 그 형태로 돼 있다.

---

## 17. 멀티워크스페이스 모드

봇 하나가 채널마다 다른 로컬 폴더에 바인딩돼 여러 프로젝트를 동시에
서비스한다. 채널 이름(`#project-a`)이 `base_dir/project-a/`에 자동
바인딩된다. 채널마다 세션과 에이전트 상태가 격리된다.

```
/workspace bind <name>       로컬 폴더 바인딩
/workspace init <git-url>    저장소 클론 후 바인딩
/workspace list              전체 바인딩 목록
```

---

## 18. 웹 관리 대시보드 (베타, v1.2.2-beta.5부터)

바이너리에 내장된 관리 UI. 프로젝트 CRUD, 세션 관리, 크론 편집기, 전역
설정, 채팅 인터페이스, 다국어까지 포함. `/web setup` 한 번으로 Management
API와 Bridge를 동시에 켜고 토큰을 발급해 접속 URL을 알려준다
(기본 `http://localhost:9820`).

Management API(`/api/v1/...`)는 `Authorization: Bearer <token>`로
인증하고 상태 조회, 재기동, 설정 리로드, 프로젝트·세션·크론 조회/수정을
REST로 제공한다. 웹 자산은 기본 내장이고 `no_web` 빌드 태그로 뺄 수
있다(바이너리 약 1MB 절감).

---

## 19. Bridge — 외부 어댑터 연동 (베타, v1.2.2-beta.5부터)

WebSocket(`/bridge/ws`)과 REST를 열어 cc-connect 세션에 외부 UI·봇·
스크립트가 직접 붙게 하는 기능. 메시지 송수신, 세션 생성/조회/삭제/전환을
지원한다. 인증은 쿼리 파라미터·`Authorization` 헤더·`X-Bridge-Token`
헤더 중 하나로 토큰을 전달한다.

이 두 기능(웹 대시보드, Bridge)은 slack-cli-agent PRD 4절에서 명시적으로
제외한 "웹 대시보드"에 해당한다. cc-connect는 이미 구현해 뒀다.

---

## 20. Slack 플랫폼 구현 이력 (docs/slack-feature-inventory.md 근거)

이 문서는 cc-connect 저장소 내부 개발 기록으로, main 브랜치에 처음 들어간
Slack 지원과 이후 `feat/multi-workspace` 브랜치에서 추가된 것을 구분해
적어 뒀다.

**최초 구현(커밋 `eaec71f`)**
- DM만 처리(`MessageEvent`), 이미지·오디오 첨부 다운로드, 스레드 답글
  컨텍스트, Socket Mode(`app_token`+`bot_token`), 세션 키
  `slack:channel:user`.

**이후 추가된 것**
- `@봇` 멘션 처리(`AppMentionEvent`).
- 슬래시 명령을 엔진 명령으로 변환(`/btw`, `/new`, `/stop` 등을 슬랙
  네이티브 슬래시 명령으로).
- 멀티워크스페이스/세션 공유 — `share_session_in_channel`로 세션 키를
  채널 단위(`slack:channel`)로 공유할지 사용자 단위로 유지할지 선택.
- **타이핑 표시 이모지 반응** — 사용자 메시지에 점진적으로 이모지를
  붙인다. 즉시 눈(👀), 2분 뒤 시계, 이후 5분마다 임의 이모지. 에이전트
  응답 완료 시 전부 정리. slack-cli-agent PRD 6.5절의 "리액션으로 상태
  표시" 요구와 개념이 같다 — 다만 cc-connect는 처리중 하나만 시간대별로
  바꾸고, PRD는 처리중/대기/완료/실패/침묵/감시이관 6종을 요구한다.
- 보안 — `allow_from` 사용자 허용목록, 오류 메시지의 토큰 마스킹, 오래된
  메시지 필터링(`core.IsOldMessage()`).
- Slack mrkdwn 서식 준수 — 시스템 프롬프트로 에이전트에게 표준 마크다운
  대신 `*굵게*` 같은 Slack 고유 mrkdwn을 쓰도록 지시.

**커밋 안 된 상태로 남아있던 것(stash, 문서 작성 시점 기준 미병합)**
- 컨텍스트 사용률(%) 계산 버그 수정 — SDK가 보고하는 `input_tokens`가
  한 자릿수처럼 비정상일 때 에이전트 자체 보고값(`[ctx: ~XX%]`)으로
  대체.
- 엔진 최초 연결 시 무조건 `--continue`로 가장 최근 CLI 세션을 이어받는
  처리(`hasConnectedOnce`) — CLI 직접 사용과 cc-connect 세션을
  연결하기 위한 것.

**Slack 설정 옵션(문서 작성 시점 기준)**

| 옵션 | 필수 | 설명 |
|---|---|---|
| `bot_token` | 예 | Slack 봇 OAuth 토큰 |
| `app_token` | 예 | Socket Mode용 앱 레벨 토큰 |
| `allow_from` | 아니오 | 사용자 허용목록 |
| `share_session_in_channel` | 아니오 | 채널 내 세션 공유 |

이 문서 자체가 "코어에는 슬랙을 하드코딩하지 않는다"는 아키텍처 원칙을
표방한다 — 플랫폼별 코드는 `platform/slack/`에만 있고, 코어는
`ChannelNameResolver`, `TypingIndicator` 같은 인터페이스로만 플랫폼
기능을 호출한다. slack-cli-agent PRD R3(엔진 교체 가능)와 같은 설계
원칙을 플랫폼 축에 적용한 것이다.

---

## 21. slack-cli-agent 요구사항과의 대조 요약

| slack-cli-agent PRD 항목 | cc-connect 대응 여부 |
|---|---|
| R1 설정만으로 봇 정의 | 대응 — `config.toml` 프로젝트 블록 |
| R3 엔진 교체 가능 | 대응 — 15종 에이전트 플러그인 구조 |
| R4 기본 읽기 전용 | 부분 대응 — 권한 모드는 있으나 owner/trusted/general 3계층 세분화는 없음 |
| R5 요청 유실 방지(영속 큐+워커 크래시 감지) | 문서에서 확인 안 됨 — README/usage.md에 큐·크래시 복구 서술 없음 |
| R6 판정 불가와 부재 구분 | 문서에서 확인 안 됨 |
| R7 후처리 가드(코드가 프롬프트 미준수를 검출·보정) | 확인 안 됨 — mrkdwn 서식은 프롬프트 지시일 뿐 사후 검증 언급 없음 |
| R9 기동 전 점검 | 부분 대응 — `doctor user-isolation`은 있으나 프롬프트·MCP 누락 검출과는 다른 대상 |
| R9-1 단일 CLI로 운영 | 대응 — `cc-connect daemon/cron/provider/send/relay` 등 하위 명령 체계 |
| R10 한 머신 여러 봇 | 대응 — 프로젝트 여러 개 등록 |
| 리액션 6종 상태 표시 | 부분 대응 — 타이핑 표시 이모지만 있고 세분화된 상태 전이는 없음 |
| 관리 명령(모델 전환·리버전 승인 등) | 대응 이상 — `/model`, `/provider`, `/mode`, `/dir`, `/cron`, `/bind`, `/workspace`로 이미 풍부함 |
| 웹 대시보드(PRD에서 명시적 제외) | cc-connect는 이미 구현(베타) |

**결론.** cc-connect가 이미 커버하는 영역(엔진 플러그인화, 단일 CLI
운영, 모델/프로바이더 런타임 전환, 웹 대시보드)은 slack-cli-agent가
새로 설계할 필요가 낮다. 반대로 slack-cli-agent가 핵심으로 잡은 신뢰성
축(영속 큐·워커 크래시 감지·요청 정확히 1회 처리 보장)과 세분화된
권한·상태 모델은 cc-connect 공개 문서 기준으로는 확인되지 않는 차별점이다
— 단, 이건 문서에 없다는 것이지 코드에 없다는 뜻은 아니다(소스 코드는
조사하지 않았다).
