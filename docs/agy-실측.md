# Antigravity CLI(agy) 조사

2026-09-15 확인. 바이너리는 `/opt/homebrew/bin/agy`.

공식 문서(`https://antigravity.google/docs/cli/`)를 읽고, 문서에 없거나 우리 쓰임에
직접 걸리는 것만 실행으로 확인했다. 각 절에 근거가 문서인지 실측인지 적는다.

Gemini 엔진을 붙이기 전에 CLI 가 실제로 무엇을 받고 무엇을 내는지 확인한 것이다.
claude CLI 가 출력 형식을 바꿨을 때 실기기 확인에서야 알아차린 적이 있어, 추측으로
파서를 쓰지 않는다.

## 1. 인자 순서 함정

`-p` 는 값을 선택적으로 받는다. 뒤에 플래그가 오면 그것을 프롬프트로 가져간다.

    agy -p --output-format json ... "프롬프트"
    → Error: -p took "--output-format" as its prompt, so the intended
      prompt was left as an argument and ignored.

**`-p` 를 명령줄 맨 끝에 두고 프롬프트를 바로 붙인다.**

    agy --output-format json --dangerously-skip-permissions \
        --model gemini-3.8-flash --effort medium -p "프롬프트"

## 2. 출력 형식

`--output-format json` 은 **단일 JSON 객체**다. claude 처럼 이벤트 배열이 아니고
codex 처럼 JSONL 도 아니다.

성공:

```json
{"conversation_id":"3fd60ecf-bbc2-449e-9f19-fe11e11f1736","status":"SUCCESS",
 "response":"2\n","duration_seconds":1.89,"num_turns":1,
 "usage":{"input_tokens":13521,"output_tokens":57,"thinking_tokens":56,
          "cache_read_tokens":0,"total_tokens":13578}}
```

실패:

```json
{"conversation_id":"","status":"ERROR","response":"",
 "error":"invalid --effort \"xhigh\" (valid: low, medium, high)",
 "duration_seconds":0,"num_turns":0,"usage":{...전부 0}}
```

- 실패해도 stdout 에 같은 형태의 JSON 이 나온다. 종료 코드는 1 이고 stderr 에도
  같은 문구가 나온다
- `model` 필드가 없다. `EngineResponse.model_actual` 은 None 으로 둔다
- 세션 식별자는 `conversation_id` 다. CLI 가 발행하므로 codex 와 같이
  `session_id_from()` 으로 받아 온다

### usage 키가 다르다

    공통 어휘                  agy 의 키
    input_tokens               input_tokens
    output_tokens              output_tokens
    cache_read_tokens          cache_read_tokens        ← claude 는 cache_read_input_tokens
    cache_creation_tokens      (없음)
    (대응 없음)                 thinking_tokens

`Usage.from_mapping()` 은 `cache_read_input_tokens` 를 읽으므로 그대로 쓰면 gemini
의 캐시 값이 항상 0 이 된다. 엔진별 매핑이 필요하다(bd sca-dyb.4).

## 3. 모델과 effort 는 독립이 아니다

`agy models` 가 내는 이름에는 effort 접미사가 붙어 있다. 두 표기가 있고 서로
배타적이다.

    gemini-3.8-flash-medium              --effort 를 주면 거부한다
    gemini-3.8-flash --effort medium     --effort 가 없으면 거부한다

실제 오류 문구:

    --model gemini-3.8-flash-medium conflicts with --effort=low
    --model gemini-3.8-flash requires --effort (available: low, medium, high)

**엔진은 접미사 없는 기본 이름 + `--effort` 형태를 쓴다.** `EngineRequest` 의
`model` 과 `effort` 가 그대로 대응되고, 계약 시험의 "모델이 명령에 반영된다" 와
"effort 가 명령에 반영된다" 를 둘 다 만족한다.

### effort 범위가 좁다

`low|medium|high` 뿐이다. claude 의 `xhigh`·`max` 를 그대로 넘기면 실행 자체가
거부된다. 채널 설정이 그 값을 쓸 수 있으므로 **엔진 안에서 `high` 로 내린다.**

사용 가능한 모델(`agy models`, 2026-09-15):

    gemini-3.8-flash-{high,medium,low}
    gemini-3.7-flash-{high,medium,low}
    gemini-3.6-flash-{high,medium,low}
    gemini-3.1-pro-{high,low}            ← medium 이 없다
    claude-sonnet-4-6 / claude-opus-4-6-thinking / gpt-oss-120b-medium

`gemini-3.1-pro` 는 medium 이 없다. 모델별로 쓸 수 있는 effort 가 다르므로 프로필에
적은 조합이 유효한지는 preflight 에서 봐야 한다.

## 4. 격리 — 무엇이 어디에 저장되는가

근거는 공식 문서(headless·settings·mcp·skills·rules 페이지)와 실행 확인이다.

    대상          전역 경로                                        작업공간 로컬
    MCP 서버      ~/.gemini/config/mcp_config.json                 <작업>/.agents/mcp_config.json
    스킬          ~/.gemini/config/skills/<이름>/                  <작업>/.agents/skills/<이름>/
    규칙          ~/.gemini/GEMINI.md                              <작업>/.agents/rules/
    설정·권한     ~/.gemini/antigravity-cli/settings.json          없음
    인증          ~/.gemini/antigravity-cli/antigravity-oauth-token 없음
    대화 기록     ~/.gemini/antigravity-cli/conversations/*.db     없음
    로그          ~/.gemini/antigravity-cli/log/                   없음

**MCP·스킬·규칙은 작업 디렉터리로 격리할 수 있다.** 봇마다 `work_root` 가 다르므로
그 아래 `.agents/` 를 두면 홈을 건드리지 않고 봇별로 갈린다. `.agent/` 는 옛 이름이고
지금은 `.agents/` 가 기본이다.

**설정·권한·인증·대화 기록은 작업공간 로컬이 없다.** 이것만은 `HOME` 을 갈라야 한다.

### 설정 위치를 바꾸는 전용 환경변수는 없다

문서가 언급하는 환경변수는 `GEMINI_API_KEY`, `GOOGLE_GEMINI_BASE_URL`,
`AGY_CLI_DISABLE_AUTO_UPDATE`, `AGY_CLI_HIDE_LOGO` 뿐이고, 설정 디렉터리를 옮기는
항목이 없다. 문서는 `~/.gemini/antigravity-cli/` 를 고정 경로로 쓴다.

바이너리에 이름이 있던 후보 2개를 실제로 걸어서 확인했다.

    env -i PATH=... HOME=/tmp/agy-h2 \
        XDG_CONFIG_HOME=/tmp/agy-xdg \
        ANTIGRAVITY_EXECUTABLE_DATA_DIR=/tmp/agy-edd \
        agy ... -p "..."

`/tmp/agy-xdg` 와 `/tmp/agy-edd` 는 빈 채로 남았고 설정·대화·로그가 전부
`$HOME/.gemini/` 아래에 생겼다. 두 변수 모두 효과가 없다.

### HOME 격리는 실제로 된다

`HOME` 만 바꿔서 돌렸을 때 사용자 홈(`~/.gemini`)은 변경되지 않았다. 격리 홈에
`.gemini/antigravity-cli/antigravity-oauth-token` 을 복사해 두면 로그인 없이 돈다.

### 따라오는 결과

- `GeminiEnvironmentPolicy` 는 `HOME_ENV_VAR = "HOME"` 이 된다. 프로세스의 실제
  홈을 덮어쓰는 것이므로 allowlist 방식(codex 와 같이)으로 만들어 나머지 변수가
  새어 들어가지 않게 한다
- 인증 파일을 봇별 홈에 둬야 한다. codex 의 `auth.json` 과 같은 문제다(sca-kos.6).
  토큰이 만료되면 봇마다 따로 갱신해야 하므로, 복사 대신 심볼릭 링크를 둘지 정한다

## 5. 권한 모델 — 플래그가 아니라 설정 파일이다

근거는 `docs/cli/permissions` 와 `docs/cli/headless`.

`settings.json` 의 `permissions` 에 `allow` / `deny` / `ask` 세 목록을 둔다.
항목 형식은 `동작(대상)` 이다.

```json
{"permissions": {
  "allow": ["command(git)", "command(regex:npm run (build|lint|test))"],
  "deny": ["command(sudo)", "write_file(.git/)"],
  "ask": ["command(*)", "mcp(sql/execute_mutation)"]}}
```

    동작           대상 형식
    read_file      경로 또는 *. 디렉터리는 하위까지
    write_file     경로 또는 *. 같은 경로의 read_file 을 함께 허용한다
    read_url       도메인 또는 *
    execute_url    도메인 또는 * (브라우저 조작)
    command        접두사, regex:패턴, 또는 *
    unsandboxed    샌드박스 밖 실행
    mcp            server/tool, server/*, 또는 *

판정 순서는 **deny > ask > allow** 다. 작업공간 안의 파일 읽기·쓰기는 기본 허용이고,
그 밖은 기본 ask 다.

### 헤드리스에서 ask 는 soft-deny 다

헤드리스는 물을 수 없으므로 ask 에 걸린 도구는 거부되는데, **실행은 계속되고 종료
코드는 0 이며 거부 안내가 stderr 로 나간다.** 종료 코드로 실패를 판정하면 놓친다.

### 결정 — 전부 연다 (2026-09-15 사용자 지시)

사용자 지시 원문 — "모든 읽기 쓰기를 다 가능하도록 풀어줘야해."

**엔진은 권한을 제한하지 않는다.** 봇별 `settings.json` 에 전면 허용을 적고
`--dangerously-skip-permissions` 를 붙인다.

```json
{"allowNonWorkspaceAccess": true,
 "permissions": {"allow": ["*"], "deny": [], "ask": []}}
```

`allowNonWorkspaceAccess` 가 필요한 이유는 기본값이 작업공간 안의 파일만 자동
허용하기 때문이다. `readable_dirs` 는 작업 디렉터리 밖을 가리키므로 이것이 없으면
그 경로를 못 읽는다.

**제한은 엔진이 아니라 격리로 만든다.** 봇이 무엇을 건드릴 수 있는지는 `work_root`
와 `readable_dirs`, 그리고 봇별 `HOME` 이 정한다. 권한 목록으로 한 번 더 조이면
두 곳에서 같은 것을 정하게 돼 어긋난다.

## 6. 출력 형식 stream-json

`--output-format stream-json` 은 NDJSON 이고 이벤트가 3종이다.

    init          시작 시 1회. cwd, tools, permission_mode
    step_update   단계마다. step_type, state(ACTIVE|DONE), text_delta,
                  tool_info, duration_seconds, usage
    result        끝에 1회. json 형식의 봉투와 같은 구조

트랜스크립트 리더(sca-dyb.3 의 gemini 판)를 만들 때 이것을 쓴다.

`--input-format stream-json` 을 함께 쓰면 한 프로세스에 여러 프롬프트를 넣을 수
있다. **stdin 을 닫아야 세션이 끝난다.** 안 닫으면 프로세스가 남는다.

## 7. 종료 코드와 status

    종료 코드   status        뜻
    0          SUCCESS        응답이 나왔다
    1          ERROR          요청 실패(모델 오류, 인증 오류 등)
    2          ERROR          입력이 잘못됐거나 지원 안 하는 명령
    -          CANCELED       중단됨
    -          INTERRUPTED    중단됨
    -          WAITING        입력 대기

## 8. 계약 시험에서 막히는 것

    시스템 프롬프트    전용 플래그가 없다. 프롬프트 본문 앞에 붙이거나
                      <작업>/.agents/rules/ 에 둔다(12,000자 제한)
    허용 도구 목록      명령줄 플래그가 없다. settings.json 의 permissions 로 정한다
                      계약 시험은 명령줄을 보므로 codex 와 같이 xfail 로 둔다
    읽기 허용 경로      --add-dir (반복 가능). 그대로 쓴다
    재개               --conversation <ID>. ID 는 CLI 가 발행한다
    타임아웃           --print-timeout, 기본 5분. 긴 작업은 늘려야 한다
    구조화 출력         --json-schema 로 스키마를 강제하면 structured_output 에 담긴다

## 9. --sandbox 는 쓰지 않는다 (2026-09-17 실측)

`agy --help` 에 `--sandbox`("Run in a sandbox with terminal restrictions
enabled") 가 있다. 코덱스가 sandbox 모드로 실행 격리를 선언하므로 같은 것을
쓸 수 있는지 실제로 재 봤다.

**결과 - 작업 디렉터리가 바뀐다.** `--sandbox` 를 붙이고 `/tmp/agysb/read` 에서
띄운 뒤 셸로 `pwd` 를 찍게 했더니 이렇게 나왔다.

    $ pwd
    /Users/taelkim/.gemini/antigravity-cli/scratch
    $ ls
    analyze.py	out.txt

파일 쓰기 시험도 같았다. "현재 디렉터리에 out.txt 를 만들어라" 가 성공으로
보고됐는데 실제 파일은 위 scratch 디렉터리에 생겼고 띄운 자리에는 없었다.
셸 명령(`echo hello`) 자체는 그대로 실행된다. 즉 막는 것은 터미널이 아니라
파일 경로다.

**그래서 이 봇에는 쓸 수 없다.**

- 요청마다 정하는 작업 디렉터리가 안 보인다. 채널별 workdir 이 무의미해진다
- scratch 는 사용자 하나에 하나뿐이라 채널 간 격리가 오히려 사라진다
- 실패가 오류가 아니라 "성공했다는 보고" 로 나온다. 로그로는 원인이 안 보인다

`--add-dir` 로 읽기 경로를 더해도 CWD 자체가 바뀌는 것은 안 돌아온다. 같은
프롬프트를 `--sandbox` 없이 돌리면 띄운 자리에 그대로 파일이 생긴다.

**결론** - GeminiEngine 의 execution_isolation 은 NONE 그대로 둔다. 엔진별
격리 수준 차이는 EngineCapabilities 로 드러내는 것이 맞고, 쓸 수 없는 플래그로
격리를 선언하면 선언과 실제가 어긋난다.

## 참고한 문서

    https://antigravity.google/docs/cli/headless
    https://antigravity.google/docs/cli/permissions
    https://antigravity.google/docs/cli/settings
    https://antigravity.google/docs/cli/mcp
    https://antigravity.google/docs/skills
    https://antigravity.google/docs/rules-workflows
    https://antigravity.google/docs/cli/install
    https://antigravity.google/docs/cli/troubleshooting

전체 목록은 `https://antigravity.google/sitemap.xml` 에서 `/docs/` 로 거른다.
