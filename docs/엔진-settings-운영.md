# 엔진 settings — 무엇을 어디에 두면 무엇이 막히는가

claude 엔진에 걸리는 `permissions` 는 운영물이라 저장소에 없다. 그래서 봇 3개가
전부 그 파일 없이 돌았고 `deny` 가 하나도 안 걸린 채였다(sca-j2zp). 이 문서는
어떤 파일이 어느 요청에 걸리는지, 본보기를 어떻게 까는지를 적는다.

## 파일 자리와 적용 범위

상태 디렉터리의 `engine/` 아래에 둔다. 이름은 `StatePaths.engine_settings` 가
정한다(`settings-<엔진>.json`, 수준별은 `settings-<엔진>.<수준>.json`).

| 파일 | 걸리는 요청 |
|---|---|
| `engine/settings-claude.json` | 모든 요청. 공통 바탕이다 |
| `engine/settings-claude.owner.json` | 소유자 요청에만 덧씌운다 |
| `engine/settings-claude.trusted.json` | 채널 신뢰 사용자 요청에만 덧씌운다 |
| `engine/settings-claude.general.json` | 그 밖의 모든 사용자 요청에 덧씌운다 |

엔진은 `공통 + 수준별 덧씌움 + 요청별 진행 훅` 을 한 JSON 으로 합쳐
`--settings` 하나로 넘긴다(`engine/claude.py` 의 `_settings_value`). 합치는
규칙은 `engine/claude_settings.py` 에 있다 — 사전은 키로, 목록은 이어 붙이고
중복을 거른다. 그래서 덧씌움은 공통의 `deny` 를 지우지 못하고 더하기만 한다.

원본 `bot.py` 는 수준마다 파일 하나를 통째로 뒀지만 여기는 덧씌움이다. 원본은
owner 와 trusted 파일이 같은 항목을 들고 있어 손으로 맞춰야 했다.

## 무엇을 어디 두면 무엇이 막히는가

- **공통 파일에 `Bash` 를 넣으면 소유자도 Bash 를 못 쓴다.** 공통은 모든 수준에
  걸리고 덧씌움이 그것을 되돌리지 못한다. 도구 전면 차단은 general 에만 둔다
- **general 에 `Read(...)` 만 넣으면 일반 사용자가 파일을 고칠 수 있다.** 쓰기
  차단은 `Edit`·`Write`·`NotebookEdit` 세 개를 따로 적어야 한다
- **`permissions` 를 `null` 이나 목록으로 적으면 적재가 거부된다.** 합침이 뒷값을
  취하므로 덧씌움의 `{"permissions": null}` 이 공통의 deny 를 통째로 지운다.
  `load_settings_file` 의 모양 검증이 그것을 먼저 막는다
- **파일이 아예 없으면 조용히 빈 조각이 된다.** 없는 것은 정상 경로라 로그에 안
  남는다. 깔았는지는 파일 존재로 본다
- **JSON 이 깨져 있으면 기동이 아니라 요청이 실패한다.** 읽기 실패는 예외로 낸다.
  deny 가 빠진 채로 도는 것보다 낫다는 판단이다

## 훅을 본보기에 두지 않는 이유

원본 `runtime/bot-settings*.json` 을 그대로 복사하면 안 된다. 그 파일의
`PreToolUse` 훅이 원본 고정 경로의 `progress_hook.py` 를 인자 없이 부르는데,
이 판은 요청마다 진행 로그 경로를 인자로 받는 훅을 따로 붙인다
(`engine/claude.py` 의 `progress_hook_settings`). 훅 목록은 이어 붙으므로 한
턴에 둘 다 실행된다. 본보기에는 훅을 두지 않는다.

## 본보기를 까는 명령

    tools/install-settings.py <봇이름> [--state-dir 경로]

동봉한 본보기(`src/slack_cli_agent/assets/engine/`)를 상태 디렉터리로 옮긴다.
`__STATE__` 자리는 그 봇의 상태 디렉터리 권한 경로 표기로 바뀐다 — 홈 아래면
`~/...`, 밖이면 `//절대경로`.

**이미 있는 파일은 건너뛴다.** 설치물 갱신이 운영물을 덮으면 안 된다는 규칙
그대로다(docs/패키징-경계.md). 그래서 이미 도는 봇에 돌려도 손으로 고친 내용이
사라지지 않는다.

`tools/new-bot.sh` 는 claude 봇을 만들 때 이 명령을 부른다. 다른 엔진은 아직
본보기가 없다.

본보기 내용은 손 확인으로 두지 않는다. `tests/unit/test_settings_asset.py` 가
`load_settings_file` 의 모양 검증을 통과하는지, 깐 뒤 합친 결과에 deny 가
남는지, 훅이 둘로 늘지 않는지를 본다.

## 본보기가 막는 것

공통 파일은 읽기만 막는다 — `~/.ssh`, `~/.aws`, `~/.config`, `~/.claude`,
`~/Library`, `~/.mcp.json`, 자격 파일 이름 패턴, `.env*`. 여기에 그 봇 자신의
`credentials.json` 과 `engine/` 아래(엔진 자격과 이 settings 파일들)를 읽기·
쓰기·고치기로 막고, `state.db` 와 `audit.jsonl` 은 쓰기·고치기만 막는다.

general 덧씌움은 `Bash`, `Edit`, `Write`, `NotebookEdit` 넷을 막는다.

조직마다 더 막을 것이 있다. 회사 문서 경로, 사내 자격 파일 자리 같은 것은 깐 뒤
운영물 쪽 파일에 더한다. 그 값은 조직 고유값이라 본보기에 넣지 않는다.
