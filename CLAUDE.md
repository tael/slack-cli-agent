# slack-cli-agent 작업 지침

## 방향 (2026-09-15 사용자 지시)

설치하면 바로 쓸 수 있는 패키징을 지향한다. 소스코드와 쓰는 사람이 고쳐 가는 영역을 나눠 관리한다.

    설치물   pip 로 설치되는 것. `src/slack_cli_agent/` 와 패키지에 동봉하는 기본 자료
    운영물   봇마다 달라지는 것. 프로필 JSON, 상태 디렉터리, 플러그인 모듈

그 봇에만 해당하는 값이나 문구를 설치물에 넣지 않고, 설치물 갱신이 운영물을 덮어쓰지 않게 한다.
경계 상세와 지금 어긋난 것은 [docs/패키징-경계.md](docs/패키징-경계.md). 새 파일 위치를 정할 때 먼저 본다.

## 원본이 동작의 기준

- 이 저장소는 `~/Projects/mametchi-slack-bot/bot.py` 를 다시 쓴 것이다. 읽기 전용으로 참조한다
- 기능이 안 돌면 원인을 추정하기 전에 원본의 같은 기능과 대조한다
- 원본과 다르게 할 이유는 커밋 메시지나 `docs/` 에 적는다. 이유 없는 차이는 옮기다 생긴 차이로 본다

## 코드 규약

- 주석은 최소로 둔다. 남길 판단 근거만 짧은 영어로 쓴다
- 사용자 대면 문자열, 로그, 예외 메시지는 한국어로 쓴다
- 긴 설계 근거는 `docs/` 에 둔다
- 새 동작은 실패하는 시험을 먼저 만들고 실제로 실패하는 것을 확인한 뒤 구현한다. 구현 뒤 판정을 되돌려 시험이 실패하는지 다시 잰다

## 명령

- 가상환경 `.venv` (`uv sync --extra dev`). 맨 `python3` 는 홈브루 쪽이라 쓰지 않는다
- 시험 `uv run --no-sync python -m pytest tests`. `-n 12` 는 pyproject addopts 에 있다. CI 는 `tests/unit` 만 돈다
- 린트 `uvx ruff check src tests`. 켤 규칙은 pyproject `[tool.ruff.lint] select`
- 타입 `uvx mypy src`. 시험까지는 `uvx --with pytest mypy src tests`. pytest 를 빼면 fixture 표기가 풀려 없는 오류가 수백 건 나온다

## 이슈 추적

- 작업 추적은 bd(beads). 상세는 `bd prime`
- 커밋과 푸시는 지시가 있을 때만 한다
