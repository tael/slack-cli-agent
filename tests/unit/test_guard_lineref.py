"""BareLineRefGuard 시험.

2026-11-17 private-taeil 에서 지적받은 실제 문장을 그대로 입력으로 쓴다.
"""

from __future__ import annotations

from slack_cli_agent.guard.base import GuardContext
from slack_cli_agent.guard.lineref import (
    BareLineRefGuard,
    bare_line_ref_prompt,
    expand_bare_refs,
    find_bare_refs,
)

REPORTED = """확인 범위를 그대로 보고 범위로 쓴 것이 원인임.

- PERSONA.md:127 : "없다", "하나뿐이다" 는 무엇을 전부 확인했는지 적을 수 있을 때만 쓴다
- PERSONA.md:159 : 보유한 정보를 전부 내지 않는다

"미결은 1건" 으로 단정하려면 127 때문에 넓게 확인해야 함. 그 12건을 확인한 폭이
그대로 보고 폭이 되면서 159 쪽이 밀렸음."""


class TestFindBareRefs:
    def test_reported_sentence_is_caught(self) -> None:
        assert find_bare_refs(REPORTED) == {
            "127": "PERSONA.md:127",
            "159": "PERSONA.md:159",
        }

    def test_no_label_is_noop(self) -> None:
        assert find_bare_refs("127 때문에 넓게 확인해야 함") == {}

    def test_label_without_bare_reuse_is_noop(self) -> None:
        body = "- PERSONA.md:127 : 확인 범위를 적을 수 있을 때만 쓴다\n- 그 줄 때문에 넓게 확인함"
        assert find_bare_refs(body) == {}

    def test_number_with_a_unit_attached_is_not_a_reference(self) -> None:
        body = "PERSONA.md:127 을 봤다. 127줄 뒤에도 같은 내용이 있고 127건을 확인했다"
        assert find_bare_refs(body) == {}

    def test_longer_number_is_not_a_match(self) -> None:
        assert find_bare_refs("pipeline.py:162 와 1629 는 다른 값이다") == {}

    def test_code_span_is_ignored(self) -> None:
        body = "PERSONA.md:127 을 고쳤다.\n```\nsed -n 127p PERSONA.md\n```"
        assert find_bare_refs(body) == {}

    def test_inline_code_is_ignored(self) -> None:
        assert find_bare_refs("PERSONA.md:127 확인. `head -127` 로 봤다") == {}

    def test_two_digit_line_is_not_checked(self) -> None:
        # 50개 응답 아카이브 스캔에서 두 자리 단독 숫자는 모두 수치였다.
        assert find_bare_refs("engine/environment.py:88 을 봤다. 전체 88 가운데 일부다") == {}

    def test_slash_separated_series_is_not_a_reference(self) -> None:
        body = "sections.py:264 를 고쳤다. 시간당 집계는 264/360/360 으로 일정하다"
        assert find_bare_refs(body) == {}

    def test_url_is_ignored(self) -> None:
        body = "sections.py:264 확인. https://example.com/a/264/b 에도 같은 값이 있다"
        assert find_bare_refs(body) == {}

    def test_number_far_from_its_label_is_not_shorthand(self) -> None:
        body = "PERSONA.md:127 을 고쳤다." + ("가" * 1600) + " 처리량은 127 이다"
        assert find_bare_refs(body) == {}

    def test_line_range_label_is_read(self) -> None:
        body = "bot.py:8249-8255 를 이식했다. 8249 쪽이 먼저다"
        assert find_bare_refs(body) == {"8249": "bot.py:8249"}


class TestExpandBareRefs:
    def test_bare_number_becomes_the_full_label(self) -> None:
        out = expand_bare_refs(REPORTED)
        assert find_bare_refs(out) == {}
        assert "PERSONA.md:127 때문에" in out
        assert "PERSONA.md:159 쪽이 밀렸음" in out

    def test_existing_label_is_untouched(self) -> None:
        out = expand_bare_refs(REPORTED)
        assert out.count("PERSONA.md:127") == 2
        assert "PERSONA.md:PERSONA.md:127" not in out

    def test_clean_body_is_unchanged(self) -> None:
        body = "- PERSONA.md:127 : 확인 범위를 적을 수 있을 때만 쓴다"
        assert expand_bare_refs(body) == body


class TestBareLineRefGuard:
    def test_requests_a_rewrite_first(self) -> None:
        result = BareLineRefGuard().apply(REPORTED, GuardContext())
        assert result.changed is False
        assert result.body == REPORTED
        assert result.detail["bare_line_refs"] == ["127", "159"]
        assert result.rerun is not None
        assert result.rerun.reason == "bare_line_ref"
        assert result.rerun.guard_name == "bare_line_ref"
        assert "127" in result.rerun.rewrite_prompt

    def test_expands_when_the_rewrite_still_has_it(self) -> None:
        result = BareLineRefGuard().apply(REPORTED, GuardContext(is_rewrite_retry=True))
        assert result.rerun is None
        assert result.changed is True
        assert "PERSONA.md:127 때문에" in result.body
        assert result.detail["bare_line_ref_retry_failed"] == ["127", "159"]

    def test_clean_body_is_a_noop(self) -> None:
        body = "확인 범위를 적을 수 있어야 한다는 줄 때문에 넓게 확인해야 함"
        result = BareLineRefGuard().apply(body, GuardContext())
        assert result.changed is False
        assert result.rerun is None
        assert result.body == body

    def test_empty_body_is_a_noop(self) -> None:
        result = BareLineRefGuard().apply("", GuardContext())
        assert result.changed is False
        assert result.rerun is None


class TestRewritePrompt:
    def test_prompt_names_the_values_and_keeps_the_draft(self) -> None:
        prompt = bare_line_ref_prompt(REPORTED, find_bare_refs(REPORTED))
        assert "127 (PERSONA.md:127)" in prompt
        assert "159 (PERSONA.md:159)" in prompt
        assert "<draft>" in prompt and "</draft>" in prompt
        assert "이중 대괄호 표식" in prompt
