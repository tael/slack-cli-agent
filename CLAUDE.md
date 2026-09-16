# slack-cli-agent 작업 지침

## 이 프로젝트의 방향 (2026-09-15 사용자 지시)

**설치하면 바로 쓸 수 있는 패키징을 지향한다.** 그래서 소스코드와, 쓰는 사람이
고쳐 가는 영역이 나뉘어 관리돼야 한다.

    설치물   pip 로 설치되는 것. `src/slack_cli_agent/` 와 패키지에 동봉하는 기본 자료
    운영물   봇마다 달라지는 것. 프로필 JSON, 상태 디렉터리, 플러그인 모듈

판단 기준은 하나다 — **그 봇에만 해당하는 값이나 문구가 설치물에 들어가면 안
되고, 설치물을 갱신할 때 운영물이 덮어써지면 안 된다.**

경계의 상세와 지금 어긋난 것은 [docs/패키징-경계.md](docs/패키징-경계.md) 에 있다.
새 파일을 어디에 둘지 정할 때 그 문서를 먼저 본다.

## 동작이 다르면 원본을 먼저 본다 (2026-09-15 사용자 지시)

이 저장소는 `/Users/example/Projects/mametchi-slack-bot/bot.py` 를 다시 쓴
것이다. **그 파일이 동작의 기준이다.** 읽기 전용으로만 참조한다.

- 기능이 안 도는 것을 발견하면 원인을 추정하기 전에 원본에서 같은 기능을 찾아
  대조한다. 2026-09-15 에 이모지 점검이 안 걸린 원인을 슬랙 이벤트 형식 탓으로
  추정해 발동하지 않는 코드를 한 판 넣었다. 원본은 처음부터 맞게 하고 있었다
- 원본과 다르게 할 이유가 있으면 그 이유를 커밋 메시지나 `docs/` 에 적는다.
  이유 없이 다른 것은 옮기다 생긴 차이로 본다

## 코드 규약

- **주석은 최소로 둔다**(2026-09-15 사용자 지시 — "코드 주석은 영문으로
  자연스럽게 쓰거나 제거. 난 주석을 별로 좋아하지않아"). 코드만 봐서 알 수
  있는 것은 적지 않는다. 남길 가치가 있는 판단 근거만 짧은 영어로 쓴다
- **사용자 대면 문자열·로그·예외 메시지는 한국어 그대로다.** 이 봇은 한국어로
  말한다. 주석 규칙과 무관하다
- 설계 근거처럼 긴 설명이 필요하면 `docs/` 에 둔다. 코드 안에 두지 않는다
- 비유와 의인화를 쓰지 않는다
- 시험은 `python3 -m pytest tests -n 4`. 린트는 `uvx ruff check src tests`,
  타입은 `uvx mypy src`. 시험 쪽까지 보려면 `uvx --with pytest mypy src tests`
  로 pytest 를 넣어야 한다 - 안 넣으면 pytest 를 못 찾아 fixture 표기가 전부
  풀리고 없는 오류가 200건 넘게 더 나온다(2026-09-17 실측 517 대 263). 시험
  쪽에는 아직 정리 안 된 오류가 있다(sca-ve2). 시험은 주석 규칙이 느슨해 함수
  표기를 요구하지 않고, 대역이 실물 서명과 어긋나는 것만 잡는다
- 가상환경이 없다. `python` 이 아니라 `python3`
- 새 동작은 실패하는 시험을 먼저 만들고, 그 시험이 실제로 실패하는 것을 실행으로
  확인한 뒤 구현한다. 구현 뒤에는 그 판정을 되돌려 시험이 실패하는지 다시 잰다


<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:6cd5cc61 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

## Agent Context Profiles

The managed Beads block is task-tracking guidance, not permission to override repository, user, or orchestrator instructions.

- **Conservative (default)**: Use `bd` for task tracking. Do not run git commits, git pushes, or Dolt remote sync unless explicitly asked. At handoff, report changed files, validation, and suggested next commands.
- **Minimal**: Keep tool instruction files as pointers to `bd prime`; use the same conservative git policy unless active instructions say otherwise.
- **Team-maintainer**: Only when the repository explicitly opts in, agents may close beads, run quality gates, commit, and push as part of session close. A current "do not commit" or "do not push" instruction still wins.

## Session Completion

This protocol applies when ending a Beads implementation workflow. It is subordinate to explicit user, repository, and orchestrator instructions.

1. **File issues for remaining work** - Create beads for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **Handle git/sync by active profile**:
   ```bash
   # Conservative/minimal/default: report status and proposed commands; wait for approval.
   git status

   # Team-maintainer opt-in only, unless current instructions forbid it:
   git pull --rebase
   git push
   git status
   ```
5. **Hand off** - Summarize changes, validation, issue status, and any blocked sync/commit/push step

**Critical rules:**
- Explicit user or orchestrator instructions override this Beads block.
- Do not commit or push without clear authority from the active profile or the current user request.
- If a required sync or push is blocked, stop and report the exact command and error.
<!-- END BEADS INTEGRATION -->
