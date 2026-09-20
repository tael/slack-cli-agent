# claude CLI 의 도구 제한 실측

`--allowedTools` 가 읽기 전용 경계를 강제한다고 적어 둔 주석이 틀렸다는 것을
확인한 기록이다. 관련 이슈는 sca-6ewc 와 sca-mo4g.

## 측정 조건

claude 2.1.263, 디렉터리 `/tmp/도구실측b`, 모델 haiku, stdin 을 닫고 실행.
공통 인자는 `-p --output-format json --permission-mode dontAsk` 다.
판정은 출력 JSON 의 `tool_use` 블록과 `tool_result` 내용으로 했다.

사용자 `~/.claude/settings.json` 은 `permissions.allow` 에 `Bash` 를 담고
있고 `defaultMode` 가 `bypassPermissions` 다. `engine/environment.py` 가 봇
프로세스에 부모 `HOME` 을 넘기므로 봇의 claude 도 같은 파일을 읽는다.

## --allowedTools 는 목록 밖을 막지 않는다

프롬프트는 "Bash 도구로 echo x 를 실행해라" 다.

| 조건 | Bash |
|---|---|
| `--allowedTools "Read"` | 실행됨 |
| `--setting-sources project` + 빈 permissions + `--allowedTools "Read"` | 실행됨 |
| `--allowedTools "Read,Grep,Glob"` (봇의 실제 조건) | 실행됨 |
| `--allowedTools ""` | 실행됨 |

빈 값과 생략도 구분되지 않는다. `--allowedTools` 는 허용을 더하는 인자이고
목록에 없는 도구를 닫지 않는다.

그 설정 파일이 읽히지 않아서가 아니다. 같은 경로에 deny 를 넣으면 막힌다.

| 조건 | Bash |
|---|---|
| `--setting-sources project` + `{"permissions":{"deny":["Bash"]}}` + `--allowedTools "Read"` | 도구 목록에서 제거됨 |

## 와일드카드는 허용목록으로 되돌릴 수 없다

| 조건 | 결과 |
|---|---|
| `--disallowedTools "*"` + `--allowedTools "Read,Grep,Glob"` | 도구 호출 0건. 모델이 도구 없이 가짜 태그를 출력 |
| settings `{"deny":["*"],"allow":["Read","Grep","Glob"]}` | 같음 |

와일드카드가 이기고 allow 가 복원하지 않는다. `ToolAccess.FORBIDDEN` 이
`--disallowedTools "*"` 하나만 쓰는 이유가 여기 있다.

## 이름 기반 거부는 사용자 settings 의 allow 를 이긴다

`--setting-sources` 없이, 즉 사용자 settings 의 `allow: ["Bash"]` 가 살아 있는
조건에서 쟀다.

| 조건 | 결과 |
|---|---|
| `--allowedTools "Read,Grep,Glob" --disallowedTools "Bash,Edit,Write,NotebookEdit"` + Bash 요청 | 거부됨 |
| 같은 조건 + Read 요청 | 정상 동작 |

## --tools 가 내장 도구 집합을 정한다

도움말은 `--tools` 를 "Specify the list of available tools from the built-in
set. Use \"\" to disable all tools, \"default\" to use all tools" 라고 적는다.

| 조건 | 결과 |
|---|---|
| `--tools "Read,Grep,Glob"` + Write 요청 | `No such tool available: Write` 로 막힘. 파일 미생성 |
| `--tools "Read,Grep,Glob"` + Read 요청 | 정상 동작 |
| `--tools "Read,mcp__slack__channels_list"` | 오류 없음. MCP 이름은 그 자리에서 뜻이 없다 |

## MCP 도구는 --tools 로 안 막힌다

`--tools "Read,Grep,Glob"` 에서 Bash 를 시켰더니 모델이
`mcp__playwright__browser_run_code_unsafe` 를 대신 호출했다. dontAsk 모드가
그 호출을 거부했지만, 그것은 승인 규칙에 걸린 것이라 `--allowedTools` 나
settings 의 allow 에 이름이 들어가면 열린다.

더 중요한 것은 그 도구가 애초에 왜 붙어 있었는가다. `--strict-mcp-config` 는
프로필에 MCP 서버가 있을 때만 붙는다. 서버가 없는 프로필에서는 사용자
`~/.claude.json` 의 전역 서버가 전부 실린다. 측정 시점에 9개였다.

| 조건 | 붙은 도구 |
|---|---|
| `--strict-mcp-config` 없이 `--tools ""` + Read 요청 | `mcp__claude_ai_Google_Drive__read_file_content` 를 호출 |
| `--strict-mcp-config` + `--tools "Read,Grep,Glob"` | Glob, Grep, Read 세 개뿐 |

## MCP 도구는 접두어 패턴으로 닫는다

`--tools` 는 내장 도구 집합만 다룬다. 프로필이 MCP 서버를 붙였으면 그 도구는
허용목록에 없어도 모델의 도구 목록에 남는다.

| 조건 | 붙은 도구 |
|---|---|
| `--tools "Read,Grep,Glob"` | Read, Glob, Grep 과 전역 MCP 도구 다수 |
| 같은 조건 + `--disallowedTools "mcp__*"` | Glob, Grep, Read |

## 적용한 설계

`ToolAccess.ALLOWLIST` 일 때 `--allowedTools <허용 전체>` 로 자동 승인을 주고
`--tools <허용 중 내장 도구>` 로 도구 집합을 닫는다. MCP 이름은 `--tools` 에서
빼고 승인 쪽에만 남긴다. 내장 도구를 하나도 허용하지 않았으면 빈 값을 넘긴다.
인자를 빼면 전체 내장 도구가 열려 뜻이 반대가 된다.

허용목록에 MCP 이름이 하나도 없으면 `--disallowedTools "mcp__*"` 를 더한다.
이름을 하나 열면서 접두어 전체를 닫으면 그 이름도 함께 닫히므로, MCP 를 허용한
턴에는 안 붙이고 그 경계를 승인 목록에 맡긴다.

`FORBIDDEN` 은 `--disallowedTools "*"` 그대로 두었다. 실측한 명령 모양이
그것이다. `UNRESTRICTED` 에는 세 인자를 모두 안 붙인다.

## 남은 구멍

원본 `bot.py:1343-1348` 은 `--setting-sources project` 와 `--settings <봇 전용
파일>` 로 사용자 settings 를 배제한다. 이식본에는 그 두 인자가 없다. 위에서
보듯 그것만으로 Bash 가 막히지는 않으므로 이번 수정과는 별개다.

`--restricted` 는 코드 실행 도구와 WebFetch 를 빼고 사용자·프로젝트 설정을
무시하며 파일 도구를 작업 디렉터리에 가둔다. bypassPermissions 도 거부한다.
이번에 쓰지 않았다. 동작 변경 폭이 커서 따로 재야 한다.
