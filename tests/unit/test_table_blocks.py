from slack_cli_agent.render import table_blocks


def test_no_table_returns_none():
    assert table_blocks.to_blocks("본문\n- 불릿") is None


def test_table_wrapped_and_br_newline():
    text = "앞 문단\n\n| 이슈 | 원인 |\n|---|---|\n| A | 첫 줄<br>둘째 **굵게** [링크](https://x.io/a) |\n\n뒤 문단"
    blocks = table_blocks.to_blocks(text)
    assert [b["type"] for b in blocks] == ["markdown", "table", "markdown"]
    table = blocks[1]
    assert table["column_settings"] == [{"is_wrapped": True}, {"is_wrapped": True}]
    els = table["rows"][1][1]["elements"][0]["elements"]
    assert els[0] == {"type": "text", "text": "첫 줄\n둘째 "}
    assert els[1] == {"type": "text", "text": "굵게", "style": {"bold": True}}
    assert els[3] == {"type": "link", "url": "https://x.io/a", "text": "링크"}


def test_table_inside_fence_untouched():
    text = "```\n| a | b |\n|---|---|\n| 1 | 2 |\n```"
    assert table_blocks.to_blocks(text) is None


def test_mention_cell_and_ragged_row():
    text = "| 담당 | 비고 |\n|---|---|\n| <@U0ACANBHY3U> |"
    rows = table_blocks.to_blocks(text)[0]["rows"]
    assert rows[1][0]["elements"][0]["elements"][0] == {"type": "user", "user_id": "U0ACANBHY3U"}
    assert len(rows[1]) == 2


def test_oversize_falls_back():
    body = "| a | b |\n|---|---|\n" + "\n".join(f"| {i} | x |" for i in range(120))
    assert table_blocks.to_blocks(body) is None
