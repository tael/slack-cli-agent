"""render/table.py 의 파이프 표 조립.

기대값은 원본 bot.py 의 as_table()/cell() 을 AST 추출해 실제로 실행해서 얻었다.
"""

from __future__ import annotations

from slack_cli_agent.render.table import as_table, cell


class Test표조립도우미:
    def test_cell은파이프와줄바꿈을치환한다(self) -> None:
        assert cell("a|b\nc") == "a/b c"

    def test_cell은빈값을대시로바꾼다(self) -> None:
        assert cell("") == "-"

    def test_as_table은빈목록이면빈문자열이다(self) -> None:
        assert as_table([]) == ""

    def test_as_table은파이프표를만든다(self) -> None:
        rows = [("a", "b|c\nd"), ("e", "")]
        assert as_table(rows) == "| 항목 | 값 |\n|---|---|\n| a | b/c d |\n| e | - |"

    def test_머리말을바꿀수있다(self) -> None:
        assert as_table([("a", "b")], head=("왼", "오")) == "| 왼 | 오 |\n|---|---|\n| a | b |"
