# 원본 분석

참조 원본은 원본 저장소 이다. 이 저장소로 복사하지 않는다.
여기 적은 것은 그 코드를 읽어 확인한 사실과, 주석에 남아 있던 실측 근거다.

분석 시점 2026-09-14. 대상 커밋은 클론 시점의 기본 브랜치 HEAD.

---

## 1. 구성과 규모

| 파일 | 줄수 | 역할 |
|---|---|---|
| `bot.py` | 7,199 | 어댑터 본체. 정책 전부가 여기 있다 |
| `test_bot.py` | 2,686 | 프롬프트 문구·채널 설정 검증. `bot.py` 를 import 하지 않고 AST 로 함수·상수만 떼어 실행 |
| `dashboard/metrics.py` | 1,264 | 대시보드 집계 |
| `test_profile.py` | 645 | 프로필 계약 |
| `usage_monitor.py` | 569 | 1시간 주기 사용량 감시 |
| `runtime/mcp/slack_readonly.py` | 506 | 읽기 전용 슬랙 MCP |
| `runtime/mcp/<이슈추적>_readonly.py` | 470 | 읽기 전용 이슈 추적 MCP |
| `test_codex_integration.py` | 402 | |
| `learn.py` | 366 | 지식 자동 축적·반영 |
| `bot_profile.py` | 290 | 프로필 로드·검증 |
| `healthcheck.py` | 252 | 기능 점검 |
| `check.py` | 234 | 기동 전 정적 점검 4종 |
| `post_rich.py` | 218 | Block Kit 발신 |
| `codex_runner.py` | 211 | Codex 엔진 |
| `scopes.py` | 139 | 슬랙 스코프 점검 |
| `dashboard/server.py` | 134 | |

셸 스크립트는 `run.sh`, `restart.sh`, `apply.sh`, `apply-restart.sh`,
`reinstall.sh`, `sync-runtime.sh`, `run-learn.sh`, `path.env`.

**단일 파일 구조가 기술 부채의 핵심이다.** `bot.py` 하나에 슬랙 이벤트 수신,
권한 판정, 프롬프트 조립, 엔진 실행, 출력 서식, 캐치업, 건강 감시, 관리 명령이
전부 들어 있다. 테스트가 `bot.py` 를 import 하지 못해 AST 로 함수를 떼어 내는
방식을 쓰는 것이 그 증거다 — 모듈을 부르는 순간 환경변수와 파일 경로를
요구하기 때문이다.

---

## 2. 동작 골격

    슬랙 Socket Mode  ->  이벤트 핸들러 3종  ->  handle_request
                                                   |
                              권한 판정 / 세션 조회 / 프롬프트 조립
                                                   |
                                            엔진 실행(subprocess)
                                                   |
                              후처리 가드 -> 서식 변환 -> 분할 -> 발신

Socket Mode 를 쓰므로 공인 IP 와 인바운드 포트가 필요 없다. 아웃바운드
WebSocket 하나로 동작한다.

엔진은 로컬 CLI 를 subprocess 로 부른다. Claude 는
`claude -p --output-format json`, Codex 는 `codex_runner.py` 를 경유한다.

### 이벤트 진입점 3종

- `app_mention` — 그대로 `handle_request`
- `message` — DM 은 바로 처리. 채널은 스레드 답글일 때만, 그것도 봇이 이미
  그 스레드에 낀 경우에만 처리한다. 멘션이 있으면 `app_mention` 이 이미
  받으므로 중복 처리하지 않는다
- `reaction_added` — 부검(dango) / 디버그 추적(brain) / 서식 점검(pencil2)

`message` 핸들러는 `subtype` 이 있으면 거르되 `file_share` 만 예외다. 파일을
붙인 DM 이 무반응이던 문제가 2026-09-11 에 이 경로로 확인됐다.

---

## 3. 권한과 신뢰 계층

    is_trusted(channel, user)        소유자 DM 또는 개인 채널. 자기 지식·페르소나까지 수정 가능
    full_authority(channel, is_owner) 소유자 DM 전용. NEVER_DISCLOSE 가드를 붙이지 않는다
    mechanism_open(channel)          channels.json 의 disclose_mechanism. 구조는 공개, 자격증명은 비공개
    조직 전용 권한 판정(플러그인 몫)

계층이 넷인데 판정 함수가 각자 다른 인자를 받고 서로 참조하지 않는다.
범용판에서는 이것을 하나의 권한 객체로 합쳐야 한다.

### 모델과 effort

```python
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
OWNER_EFFORT_MIN = "medium"
```

규칙은 하나다 — **채널 설정값은 다른 사람을 위한 상한이지 소유자의 하한이 아니다.**
채널값이 소유자 하한보다 낮으면 하한으로 올린다. 프롬프트에 `ultrathink` 가
들어 있으면 `high` 로 올린다.

---

## 4. 상태 외부화

프롬프트 18개, 페르소나, 채널 설정, MCP 설정, 권한 규칙이 전부
`~/.<봇이름>/` 아래 파일이다. 요청마다 다시 읽으므로 재기동 없이 반영된다.

```python
PROMPTS_DIR = STATE_DIR / "prompts"

def prompt_text(name, keep_slots=None):
    """파일이 없거나 비면 RuntimeError. 가드가 빠진 채 답하면 되돌릴 수 없다."""
```

**실패 정책이 명확하다.** 프롬프트 파일이 없으면 조용히 빈 문자열을 쓰지 않고
예외를 낸다. 가드 문구가 빠진 채 답하는 것을 사고로 본다.

자리표는 `<<NEVER_DISCLOSE>>`, `<<OWNER_MENTION>>`, `<<ORG_TOOL_CTL>>`,
`<<SLACK_FORMAT>>` 넷이다. `<<SLACK_FORMAT>>` 은 채널을 알아야 채워진다.
`<<ORG_TOOL_CTL>>` 은 원본이 사내 배포 도구를 조작하는 안내를 끼우던
자리표다. 조직 전용이라 이 패키지에 두지 않고 플러그인 몫으로 뺐다.

### 시스템 프롬프트 조립 순서

`build_system_prompt` 가 두 엔진 공통으로 조립한다.

1. 페르소나 + 도메인 + 채널 지식
2. 채널 모드 프롬프트
3. OWNER_NOTE 또는 NON_OWNER_NOTE
4. SLACK_FORMAT 자리표 치환
5. POSTMORTEM / DEBUG_TRACE / FORMAT_REVIEW_NOTE
6. asker_note
7. TRUSTED_NOTE
8. SENSITIVE_GUARD (비소유자)
9. FULL_AUTHORITY_NOTE 또는 MECHANISM_NOTE
10. ORG_TOOL_NOTE (조직 전용. 플러그인이 넣는다)
11. WATCH_NOTE
12. silence_rule

### 지식 적재 규칙

`_*.md` 는 공통 지식이다. 첫 줄에 `<!-- when: 낱말,낱말 -->` 이 있으면 요청
본문에 그 낱말이 있을 때만 싣는다. 싣지 않은 파일은 이름과 경로만 프롬프트에
남겨 모델이 필요하면 직접 열게 한다.

근거 — 전부 싣던 방식은 턴마다 7,500 토큰을 더 썼다.

---

## 5. 엔진 계층

### Claude 실행 명령

```python
cmd = [CLAUDE_BIN, "-p", "--output-format", "json",
       "--setting-sources", "project",
       "--settings", <trusted | owner | 일반 중 하나>,
       "--mcp-config", BOT_MCP, "--strict-mcp-config",
       "--permission-mode", "dontAsk",
       "--exclude-dynamic-system-prompt-sections",
       "--allowedTools", tools,
       "--model", ..., "--effort", ...]
cmd += ["--add-dir", ...]              # API_SPEC_DIRS + ATTACH_DIR + DATA_DIR + STATE_DIR
cmd += ["--append-system-prompt", system]
cmd += ["--resume", session_id] if resume else ["--session-id", session_id]
cmd += ["--", prompt]
```

마지막 `--` 가 중요하다. 하이픈으로 시작하는 사용자 입력이 옵션으로 해석되는
것을 막는다.

**읽기 전용 권한 모델** — 원본은 `--allowedTools` 화이트리스트가 settings 의
allow 규칙보다 우선해 Bash·Edit·Write·NotebookEdit 를 차단한다고 보았다.
**이 전제는 2026-09-20 실측에서 반증됐다** - `--allowedTools` 는 자동승인을
더하는 인자일 뿐 목록 밖을 막지 않는다. 근거와 이 저장소가 대신 쓰는 인자는
[claude CLI 의 도구 제한 실측](/docs/claude-도구제한-실측.md) 에 있다.

**과금** — OAuth 구독 토큰(`sk-ant-oat`)을 쓰면 `--max-budget-usd` 를 쓰면 안
된다. 정상 요청을 끊는다. seat allowance 를 소모한다.

### Codex 의 다른 점

- `developer_instructions` 는 스레드 최초 값이 끝까지 우선한다. 원본은
  `turn_directives()` 로 턴마다 바뀌는 것(화자·침묵 규칙)만 본문 앞에 붙였다.
  **이식본은 그 훅을 두지 않는다**(sca-r1hc) - resume 턴의 시스템 지침 전체를
  프롬프트 본문에 싣는 쪽으로 바꿨다(sca-ivs, `engine/codex.py` 의
  `_resume_prompt`). 화자 규칙만 싣던 원본과 달리 그 턴의 지식·프롬프트 갱신이
  함께 따라온다
- `--add-dir` 에 해당하는 인자가 없다. `readable_paths_note()` 로 읽을 수 있는
  경로를 문장으로 알린다
- 셸이 항상 붙어 있어 모델이 curl 로 무엇이든 된다고 판단한다.
  `write_paths_note()` 로 실제 가능한 수단을 명시한다

### 엔진 전환 상태 기계

```python
ENGINE_STATE = STATE_DIR / "engine_state.json"
# engine, from_engine, reason, detail, switched_at,
# approval(pending/approved/denied), last_probe_at, probe_ok, probe_detail
ENGINE_PROBE_SEC = 600
ENGINE_PROBE_PROMPT = "준비됐으면 OK 두 글자만 답해라."
```

**전환은 즉시, 답하는 것은 승인 후다.** Claude 한도가 소진되면 Codex 로 바꾸되
사람이 승인하기 전에는 한도 안내만 답한다. `probe_engine()` 으로 실제 응답을
확인한 뒤 알린다.

### 한도 판정

```python
USAGE_LIMIT_HINTS = ("weekly limit", "usage limit", "rate limit",
                     "hit your limit", "limit · resets", "limit reached")
```

`subtype` 은 `success` 인데 `api_error_status` 가 429 이고 `result` 문구에 실제
사유가 있는 경우가 있다. 종료 코드만 보면 못 잡는다.

---

## 6. 세션 관리

SQLite `sessions` 테이블이 `thread_ts -> session_id` 를 잇는다.

    스레드 단위 세션   SESSION_TTL_HOURS = 24
    채널 단위 세션     CHANNEL_SESSION_TTL_DAYS = 7

`workdir` 과 `model` 은 화자가 아니라 대화 단위로 정한다. 스레드는 단일 화자가
아니므로, 화자가 바뀔 때마다 실행 환경을 다시 고르면 한 번 넓어진 세션이 다음
차례에 좁아져 `--resume` 이 끊긴다.

**맥락 복원 원칙** — 대화의 원본은 세션 파일이 아니라 슬랙이다. 세션이
만료되거나 끊기거나 새로 발급돼도 슬랙에서 다시 세운다.

- 이어갈 세션이 없으면 `thread_transcript()` 로 지난 대화를 읽어 프롬프트 앞에 붙인다
- 이어가는 세션이어도 마지막으로 본 시각 이후의 대화를 `after_ts` 로 더 읽어 붙인다.
  세션이 본 것은 봇이 실제로 처리한 턴뿐이고, 사람끼리만 오간 말은 세션 기억에 없다

### 작업 디렉터리

홈 밖이어야 한다. 실측 — 홈 하위 빈 디렉터리에서 63,389 토큰, 홈 밖에서
20,832 토큰. 차이 42,421 이 글로벌 지침 적재량이다.

`full` 과 `light` 두 개를 둔다. `light` 는 도메인 사실을 싣지 않는다.

---

## 7. 요청 처리 흐름 (`handle_request`)

순서대로 적는다. 이 순서 자체가 정책이다.

1. 재전송 판정 — `already_seen_event`. 단 큐 재실행(`_requeued`)은 제외한다.
   큐 재실행까지 걸리면 대기줄에 넣은 요청이 영영 처리되지 않는다
2. 허용 채널 판정 — `is_allowed`
3. 멘션 제거 후 본문이 비면 되묻는다
4. 종료 중이면 `save_restart_dropped` 로 요청 자체를 저장하고 안내만 한다
5. 관리 명령이면 모델을 거치지 않고 즉시 처리
6. 소유자가 부른 채널은 자동 등록
7. 같은 스레드가 처리 중이면 대기줄에 넣고 모래시계를 단다
8. 눈 표식을 달고 진행 표시 스트림을 연다
9. `mark_handled` + `register_inflight` — 답변 표식이 아직 없는 구간을 캐치업이
   다시 집지 않게 막는다
10. 세션 조회, 첨부 저장, 슬랙 링크 선행 조회, 지난 대화 복원
11. `run_with_fallback` 으로 엔진 실행
12. 이어가기가 깨졌으면(`nonzero_exit`, `bad_json`) 새 대화로 한 번 더 시도.
    한도 소진은 새 대화로도 같은 벽이라 다시 시도하지 않는다
13. 800초를 넘겼으면 `report_slow` 로 트러블슈팅 스레드를 연다
14. `audit` 기록
15. 침묵 판정이면 zipper_mouth 표식만 남기고 끝낸다
16. **발송 직전 스레드 재확인** — 답을 만드는 사이 말이 더 달렸으면 반영해 다시 낸다
17. 후처리 가드 — 평문 멘션 치환, 잘못된 호칭 제거
18. WATCH 태그 등록 또는 빈 약속 보정
19. 답이 비었으면 올리지 않는다
20. 발신, 표식 전환, 대기줄의 다음 건 실행

### 캐치업 중복 방지 다층

    _busy_threads        스레드 단위 처리 중
    _handled_msgs        요청 단위. HANDLED_KEEP_SEC = 6시간
    _seen_events         슬랙 재전송. 상한 2000
    _thread_consumed     발송 전 재확인이 흡수한 말의 시각

마지막 것은 2026-09-09 에 추가됐다. 발송 전 재확인과 대기줄이 같은 말을 두 번
소비하던 문제를 공유 상태로 해결했다.

---

## 8. "프롬프트로 막히지 않는 것은 코드가 잡는다"

범용판이 이어받아야 할 핵심 설계 원칙이다. 지침으로 지시했는데 지켜지지 않은
것들을 코드가 발신 직전에 보정한다.

| 함수 | 하는 일 |
|---|---|
| `PROMISE_WITHOUT_WATCH_RE` | "지켜보겠다"고 말만 하고 `[[WATCH:]]` 태그를 안 단 것을 검출. 다시 쓰게 하고, 재시도에도 실패하면 그 문장 자체를 잘라낸다 |
| `fix_plain_mentions()` | `@이름` 평문을 `<@U...>` 진짜 멘션으로 치환. 코드 스팬은 제외 |
| `guard_wrong_addressee()` | 첫머리에 말을 건 사람이 아닌 다른 사람을 부르면 그 멘션을 제거하고 본문에 밝힌다 |
| `late_rewrite_lost_content()` | 재작성본이 앞 답보다 40% 이상 짧으면 내용 유실로 판정하고 둘 다 낸다 |
| `linked_thread_context()` | 슬랙 링크를 모델이 열지 않아도 코드가 먼저 열어 프롬프트에 싣는다 |
| `usage_rows()` | 토큰 사용량은 소유자 전용 채널에서만 노출. 지침이 아니라 코드가 판정 |

**조용히 고치지 않는다.** 무엇을 지웠는지 본문에 남기고 감사 로그에도 적는다.

---

## 9. 출력 계약

### 채널별 표기 이원화

`channels.json` 의 `rich` 값으로 갈린다.

- **리치** — 마크다운 원문을 `markdown` 블록으로 그대로 넘긴다. 슬랙이 표·헤딩·
  구분선을 직접 렌더한다
- **평문** — `to_mrkdwn()` 으로 낮춘다. 이중 별표를 단일로, 헤딩을 굵게, 표를
  불릿으로, 마크다운 링크를 `<url|라벨>` 로 바꾼다. 코드블록 안은 손대지 않는다

### 분할

`md_chunks()` 는 표·코드블록·인용·목록을 중간에서 자르지 않는다.
`fit_chunk()` 는 표 조각마다 열 이름 행을 다시 달고 코드블록은 펜스를 닫고 다시 연다.

분할 후 `verify_chunks()` 로 점검하고 실패하면 `safe_fallback()` 으로 물러선다.
조용히 넘기지 않고 `audit` 에 남긴다.

**`separate_tables()` 를 분할 전후로 두 번 부른다.** 앞에서 한 번 띄워도
`md_chunks` 가 표와 문단을 각각 끊고 `split_for_blocks` 가 `"\n".join` 으로 다시
붙이면서 빈 줄이 사라진다. 2026-08-27 에 그렇게 깨졌다 — 원문 마크다운은
정상이었고 분할이 망가뜨렸다.

### 발신 실패 처리

- 리치 표기만 거절당하면(`invalid_blocks`) 평문으로 낮춰 다시 보낸다. 표기 하나
  때문에 내용을 버리지 않는다. 2026-08-26 06:31 에 부검 하나가 통째로 사라진 적이 있다
- 앞 조각이 이미 나갔는데 뒤가 실패하면 "N/M 까지만 전달됐습니다" 를 붉은 띠로
  덧붙인다. 답이 끊긴 것을 모르고 읽으면 그게 전부인 줄 안다

### DM 과 채널

채널은 스레드로 답한다. DM 은 대화 자체가 1:1 이라 본문에 쓴다. 다만 상한을
넘겨 쪼갠 경우에는 DM 에서도 둘째 조각부터 스레드로 접는다.

---

## 10. 상태 표식 (리액션)

```python
SILENT_MARK_EMOJI = "zipper_mouth_face"
DONE_EMOJI = frozenset({"white_check_mark", SILENT_MARK_EMOJI})
UNFINISHED_EMOJI = frozenset({"eyes", "hourglass", "x"})
POSTMORTEM_EMOJI = "dango"
DEBUG_TRACE_EMOJI = "brain"
FORMAT_REVIEW_EMOJI = "pencil2"
WATCH_MARK_EMOJI = "mag"
```

    eyes                처리 중
    hourglass           대기줄
    white_check_mark    답했다
    x                   실패
    zipper_mouth_face   침묵하기로 결정
    mag                 감시 큐로 넘어감(완료 아님)

`eyes` 와 `hourglass` 는 프로세스가 죽어도 그대로 남으므로 완료로 보지 않는다.
사람이 직접 `white_check_mark` 를 달면 완료로 본다 — 봇이 붙들고 있는 것을
손으로 놓게 하는 수단이다.

---

## 11. 상태 안내문 목록화

```python
NOTICE_LATE / NOTICE_BUSY / NOTICE_FULL / NOTICE_JOINED / NOTICE_RESTART /
NOTICE_ASK_WHAT / NOTICE_NOT_LISTED / NOTICE_MODE_CODE / NOTICE_MODE_API / NOTICE_MODE_PLAIN

def is_notice(text): return (text or "").strip() in NOTICE_TEXTS
```

**안내문이 답변으로 세어지면 캐치업이 그 요청을 처리된 것으로 짝지어 영영
묻힌다.** 그래서 상수로 두고 목록을 만든다. 회귀 테스트가 `post` 로 나가는
리터럴을 훑어 목록에서 빠진 것을 검출한다.

`unanswered()` 가 이 목록을 쓴다 — 봇의 말이라도 안내문이면 답으로 세지 않고
앞 요청을 지우지 않는다.

---

## 12. 응답 게이트

부르지 않은 자리에서 나설지 판단한다.

```python
def worth_answering(text, bot_asked=False)
```

- 괄호로 전체를 감싼 혼잣말(`(하품)`)은 봇이 되물은 자리라도 답이 아니다
- 봇이 되물은 자리에서 온 말은 짧아도, 자모나 이모지만 있어도 답이다
- 이모지와 문장부호만 남으면 아니다
- 맞장구 어휘만으로 이뤄진 말은 아니다
- 맺음 인사("잘 부탁")는 물음표가 없으면 아니다

**길이로 판단하지 않는다.** 한국어 지시문은 짧다 — "올려", "보내", "취소해" 가
전부 15자를 넘지 않는다. 2026-08-25 에 길이 규칙이 승인 3건을 연달아 버렸다.

정규식 주의점이 주석에 남아 있다. `REACTION_WORD` 의 어느 대안에도 `+` 를 붙이지
않는다 — 바깥에서 다시 `+` 로 감싸므로 `(?:ㅋ+)+` 형태가 되어 역추적이 지수로
증가한다. `REACTION_MAX_LEN = 120` 으로 상한을 둔다.

---

## 13. 캐치업 (catch-up)

재기동·장애로 놓친 멘션을 복구한다.

```python
CATCHUP_WINDOW_SEC = 7200
CATCHUP_MAX_WINDOW_SEC = 86400
CATCHUP_THREAD_LOOKBACK_SEC = 7*86400
CATCHUP_GRACE_SEC = 120
```

`CATCHUP_GRACE_SEC` 은 한때 3600 이었는데 틀린 값이었다. 슬랙 읽기 지연만 덮는
역할로 되돌렸다.

### 빈 응답 문제

슬랙이 `ok` 를 주면서 `messages` 를 비워 보내는 일이 있다. 오류가 아니라 정상
응답이라 그대로 쓰면 "놓친 요청이 없다" 가 된다.

```python
HISTORY_MIN_INTERVAL_SEC = 2.0   # 너무 빨리 부르면 429 대신 ok + 빈 목록이 온다
HISTORY_READ_TRIES = 3
HISTORY_READ_PAUSE_SEC = 2.0
```

`read_history()` 는 세 번 읽어도 비면 `None` 을 돌려준다. 빈 목록과 판정 불가를
구분한다. `find_missed` 가 `None` 을 받으면 그 채널을 재시도 큐에 넣는다.

### 타임스탬프 정밀도

```python
def slack_ts(value):
    return f"{float(value):.6f}"
```

슬랙 타임스탬프는 소수 6자리다. `str(time.time())` 은 왕복 가능한 최단 표기를
쓰므로 7자리가 나올 수 있다. 그 값을 `oldest` 로 보내면 슬랙이 오류 없이 빈
목록을 준다. 2026-09-11 측정에서 표본 3,000개 중 956개(약 31%)가 7자리였다.

### 짝짓기

`unanswered(thread, asked)` — "뒤에 봇 답글이 하나라도 있으면 처리됐다" 로 세면
앞 요청이 뒤 요청의 답에 묻힌다. 봇은 스레드마다 하나씩 처리하므로 들어온
순서대로 짝을 짓는다. 연속된 봇 글은 하나로 본다(긴 답이 조각으로 나뉜 경우).

### 묶음 처리

한 스레드에 놓친 것이 여럿이면 가장 최근 것 하나만 실제로 처리한다. 지난 대화
복원이 나머지를 함께 담으므로 그 답이 전체를 종합한 답이 된다. 나머지에는 같은
표식만 남긴다. 2026-08-31 이전에는 건마다 답해 10개가 밀리면 답이 10개 올라갔다.

### 재기동으로 잘린 요청

캐치업이 대화 기록을 훑는 방식은 신선도 유예에 걸리면 그 회차에서 통째로
사라진다. 그래서 요청 자체를 `RESTART_DROPPED` 파일에 저장한다.

`save_inflight_dropped()` 는 종료 신호를 받는 즉시 저장한다. 끝날 때까지
기다린 뒤 저장하면, 기다리는 사이 강제 종료될 때 기록 없이 사라진다.
2026-09-02 21:34 유실이 그 경로였다.

기동 순서 — `replay_restart_dropped`(2초) 가 `catch_up`(5초) 보다 먼저 돌아
그 스레드를 선점한다.

---

## 14. 건강 감시

```python
SOCKET_RECONNECT_LIMIT = 4   # 정상 4시간35분 0회 vs 장애 22분 128회
SOCKET_ERROR_LIMIT = 8       # 이전 20건/180초는 실제 발생률 16.8건보다 높아 한 번도 발화하지 않았다
HEALTH_INTERVAL_SEC = 30
```

`SocketErrorWatch` 는 `logging.Handler` 를 상속해 슬랙 라이브러리 로그를 직접
센다. 라이브러리 콜백에 의존하지 않으므로, 문구가 바뀌어도 건강 점검 자체는
계속 실행된다.

판정의 주 근거는 **재연결 횟수**다. 정상 운영에서는 몇 시간을 돌아도 재연결이
일어나지 않으므로 되풀이 자체가 이상이다. 오류 건수는 라이브러리 문구에 따라
흔들려 보조로만 쓴다.

슬랙 API 에는 닿는데 소켓만 깨지면 `self_restart()` 로 스스로 나간다.
종료 코드 1 로 나가야 launchd 가 다시 띄운다.

`mcp_ready()` 는 MCP 실행 파일과 셰뱅 인터프리터가 PATH 에 있는지 본다.

---

## 15. 지켜보기 큐 (watch job)

"지켜보다가 끝나면 보고하겠다" 는 약속을 실제로 지키는 장치다.

답변에 `[[WATCH: ...]]` 태그가 있으면 본문에서 떼어 내고 큐 파일에 등록한다.
프로세스가 재기동해도 `WATCH_JOBS_FILE` 에서 이어간다 — 세션 하나의 수명과
무관하게 도는 것이 핵심이다.

```python
WATCH_CHECK_INTERVAL_SEC = 300
WATCH_JOB_MIN_GAP_SEC = 300
WATCH_JOB_MAX_AGE_SEC = 24*3600
```

확인 프롬프트는 조회만 시키고 새 작업을 시키지 않는다. 아직이면
`WATCH_STILL_TAG`, 끝났으면 완료 보고와 `WATCH_DONE_TAG` 를 붙이게 한다.
24시간을 넘기면 포기하고 소유자에게 알린다.

큐 저장은 lock 안에서 파일을 다시 읽어 병합한다. 확인이 도는 사이 새로 등록된
건을 덮지 않는다.

---

## 16. 리액션 기반 후처리 3종

셋 다 트러블슈팅 채널에 새 스레드로 올리고, 요약은 채널에 상세는 스레드에 넣는다.

| 리액션 | 이름 | 보는 것 |
|---|---|---|
| dango | 부검 | 무엇이 왜 잘못됐고 어떻게 고칠지 |
| brain | 디버그 추적 | 잘못됐다고 전제하지 않고 입력·판단·출력 과정만 |
| pencil2 | 서식 점검 | 내용은 보지 않고 겉모습만. 교정본으로 원 메시지를 수정까지 한다 |

셋 다 같은 구조다 — 기록 파일로 중복 실행을 막고, 중단되면 기록을 지워
재시도 가능하게 남긴다. 조용히 죽으면 사람은 돌고 있는 줄 알고 기다린다.

`find_answer_record()` 가 `audit.jsonl` 에서 그 답을 만든 실행 기록을 찾는다.
본문 앞 60자로 대조하고, 못 찾으면 같은 스레드의 마지막 성공 기록으로 물러선다.
못 찾으면 실행 정보 줄을 통째로 뺀다 — **없는 값을 0 으로 채우지 않는다.**

부검 프롬프트에 원 요청(`question`)을 함께 넣는다. 넣지 않으면 대화록만 보고
어느 물음에 답한 것인지 스스로 짚어야 하고, 대화록이 한 줄이라도 비면 엉뚱한
물음에 연결해 정상 응답을 오답으로 부검한다. 2026-09-11 에 그런 사례가 있었다.

형식 구분선이 없으면 다시 쓰게 한다. 경고만 남기고 넘기면 형식을 지키라는
요구가 아무것도 아니게 된다.

---

## 17. 진행 표시 (streaming)

`chat.startStream` / `chat.appendStream` / `chat.stopStream` 을 쓴다.

PreToolUse 훅(`progress_hook.py`)이 세션별 파일에 도구 이름을 적고, 봇이 3초
주기로 새 줄만 읽어 흘린다. Claude 실행 경로는 건드리지 않는다.

처음에는 6초마다 경과 초를 덧붙였다. `appendStream` 은 이어붙이기만 되므로
오래 걸릴수록 숫자 줄이 쌓여 읽기 어려웠다. 지금은 도구 이름을 사람이 읽는
단계 이름으로 바꿔 보여준다 — 줄 수가 실제 단계 수만큼으로 줄고, 대기가 길 때
무엇 때문인지 드러난다.

```python
PROGRESS_TICK_SEC = 3
PROGRESS_IDLE_SEC = 45   # 생성만 오래 하는 구간에는 도구 호출이 없어 멈춘 것처럼 보인다
```

**본 흐름을 막지 않는 것이 전제다.** 모든 호출을 예외로 감싸고 실패는 로그로만
남긴다. 세션을 이어가면 지난 요청의 줄이 남아 있으므로 `attach()` 에서 파일을
비우고 시작한다. 끝나면 스트림을 닫고 임시 표시를 지운다.

---

## 18. 관리 명령

소유자 전용. 모델을 거치지 않고 `handle_admin` 이 즉시 처리한다.

    채널 목록 / 채널 해제
    말수 많게 · 보통 · 적게
    api 모드 / 코치 모드 / 기본 모드
    학습 제안 / 학습 반영 / 학습 되돌리기 YYYY-MM-DD
    엔진 상태 / 엔진 승인 / 엔진 거부
    도움말

---

## 19. 실측으로 정해진 상수

주석에 왜 그 값인지가 전부 남아 있다. 범용판이 그대로 이어받을 근거다.

```python
TIMEOUT_SEC = 900          # 300초는 150건 중 2건을 잘랐고 중앙값은 34초
SLOW_REPORT_SEC = 800
SLEEP_GAP_SUSPECT_SEC = 30 # 벽시계와 monotonic 차이. 43분 보고가 실제 42초였던 사례
MAX_CONCURRENT = 10
SLACK_CHUNK = 3500
MARKDOWN_BLOCK_LIMIT = 12000
HISTORY_MAX_MSGS = 40; HISTORY_MAX_CHARS = 12000
LINKED_THREAD_MAX = 3
LATE_REWRITE_MIN_RATIO = 0.6; LATE_REWRITE_MIN_CHARS = 200
ASSUMED_TOKENS_PER_SEC = 40
SHUTDOWN_GRACE_SEC = 330
STATE_INTERVAL_SEC = 5
USAGE_CHECK_INTERVAL_SEC = 3600
CONTEXT_LIMIT = {}         # 추측한 분모로 퍼센트를 만들지 않는다는 원칙으로 비워 뒀다
```

마지막 것이 이 코드베이스의 태도를 보여준다. 모르는 값으로 지표를 만들지 않는다.

### 시간 분해

`time_breakdown()` 이 도구 실행 / 사고(토큰 소모) / 단순 대기(토큰 무관) /
재시도로 버린 시간을 구분한다. `detect_retries()` 는 캐시 사용량으로 역산한다 —
`cache_creation == 0` 이고 `cache_read` 가 직전 합보다 크면 재시도로 본다.

---

## 20. 기동 전 점검 (`check.py`)

`restart.sh` 가 6단계를 순서대로 돌리고 하나라도 실패하면 재시작을 중단해 옛
프로세스를 유지한다.

    py_compile -> check.py -> test_profile.py -> test_codex_runner.py
    -> test_codex_integration.py -> BOT_PROFILE=<봇이름> test_bot.py

`check.py` 자체는 4종을 본다.

1. **AST 로 정의되지 않은 이름 참조 검출**
2. **MCP 서버 기동 가능성** — `path.env` 의 PATH 로 실행 파일·권한·셰뱅
   인터프리터를 확인한다. LaunchAgent 의 PATH 는 `/usr/bin:/bin:/usr/sbin:/sbin`
   뿐이라 `#!/usr/bin/env node` 가 조용히 실패한다. 2026-08-25 에 8개 서버가
   이 이유로 멈췄다
3. **프롬프트 파일 존재·비어있지 않음** — 소스에서 `prompt_text("NAME")` 를
   정규식으로 뽑아 목록을 손으로 관리하지 않는다
4. **작업 디렉터리** — 존재하고, 홈 밖이고, `CLAUDE.md` 가 없는지

PID 파일(`$STATE/bot.pid`)로 재시작 대상을 고른다. 두 봇이 같은 `bot.py` 를
실행하므로 명령줄로는 구별하지 못한다. `trap ... EXIT INT TERM HUP` 으로
unload 와 load 사이에서 끊겨도 load 를 재시도한다.

---

## 21. 회사 결합 실측

범용판에서 제거하거나 설정으로 외부화할 대상이다. 추정이 아니라 grep 건수다.

| 분류 | 실측 |
|---|---|
| 개발자 홈 절대 경로 | `bot-settings-trusted.json` 33, `bot-settings-owner.json` 33, `bot-settings.json` 24, `codex-config.redacted.toml` 18, `bot-mcp.redacted.json` 17, `bot.py` 5, 테스트 7, 프롬프트·지식 6 |
| `bot.py` 리터럴 | 사람 이름 62(문자열 9), 봇 이름 54(대부분 주석), 조직 내부 도구 이름 92 |
| 채널 ID 상수 | `CH_PRIVATE`, `CH_API_HELPDESK`, `RICH_CHANNELS`, `OWNER_MENTION` |
| 경로 상수 | `API_SPEC_DIRS`(회사 저장소 3개), `DOMAIN_FILE`, `PEOPLE_FILE`(455명) |
| MCP 도구 | `ALLOWED_TOOLS` 90여 개 — 사내 검색·데이터웨어하우스·이슈추적·문서·슬랙·깃허브·스프레드시트·일정·메일·오케스트레이션·문서조회·브라우저 |
| 회사 전용 기능 | 사내 워크플로 도구 조작, 사내 API 헬프데스크 모드, 조직 코치 모드 |
| 페르소나 지식 파일 | 채널명이 그대로 파일명인 것 다수 |

`build_people()` 은 슬랙 `users.list` 로 계정 핸들과 실명 표를 만든다. 퇴사자도
담는다 — 과거 데이터에 남아 있는 이름이라 오히려 더 필요하다. 매 요청에 싣지
않고 파일로 두어 필요할 때만 열게 한다.

`TOOL_LABELS` 도 MCP 서버 이름이 그대로 박혀 있다. 표에 없는 `mcp__` 도구는
서버 이름을 살려 "<서버> 조회 중" 으로 쓴다 — 신규 도구가 이름 그대로 노출되지
않게 하는 처리다.

---

## 22. 범용판이 이어받을 것과 버릴 것

### 이어받는다 (정책)

- 권한·신뢰 계층과 소유자 하한 규칙
- 상태 외부화와 프롬프트 실패 정책(없으면 예외)
- 조건부 지식 적재
- 엔진 추상화와 전환 상태 기계(전환 즉시, 응답은 승인 후)
- 세션 맥락의 원본은 슬랙이라는 원칙
- 코드 후처리 가드 전부
- 상태 안내문 목록화
- 캐치업 다층 중복 방지, 판정 불가와 부재의 구분
- 리액션 후처리 3종의 구조(중복 방지 + 실패 시 재시도 가능)
- 기동 전 점검 4종
- 실측 상수와 그 근거 주석

### 버린다 (회사 전용)

- 사내 워크플로 도구 조작, 사내 API 헬프데스크 모드, 조직 코치 모드
- 하드코딩된 채널 ID·사용자 ID·사람 이름
- 회사 저장소 경로와 도메인 파일
- 90여 개 MCP 도구 화이트리스트 고정값

전용 기능은 삭제가 아니라 **봇별 플러그인**으로 옮길 수 있게 확장점을 둔다.

### 구조를 바꾼다

- 단일 파일 7,199줄 -> 책임별 모듈·클래스 분리
- 테스트가 AST 로 함수를 떼어 내는 방식 -> 정상 import 가 가능한 구조
- 전역 변수와 모듈 수준 부수 효과 -> 생성자 주입

---

## 23. 민감 정보 분리 요건

사용자 지시 — 회사 채널명 등 민감 기록은 봇별로 남되 push 돼서는 안 된다.

분리 대상은 다음과 같다.

    채널 ID 와 채널 이름
    사용자 ID (소유자·관리자)
    내부 시스템 주소와 저장소 경로
    페르소나·지식 파일 본문
    MCP 서버 자격증명

저장소에는 예시 파일(`*.example`)만 두고 실제 설정은 상태 디렉터리에 둔다.
`.gitignore` 로 봇별 설정 전체를 제외한다.
