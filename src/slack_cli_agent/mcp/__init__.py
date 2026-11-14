"""리포에 내장된 MCP 서버.

봇마다 따로 복사해 두던 조회 도구(예: 마메치의 `~/.mametchi/mcp/`)를 설치물
안으로 들여온 자리다. 조직 고유값(채널 ID·토큰·도구 등재 여부)은 여기 두지
않는다 — 프로필의 `mcp_servers` 와 환경변수로만 받는다(docs/패키징-경계.md).
"""

from __future__ import annotations
