"""Mechanical check that a postmortem's 5 Why table forms one causal chain.

Ported from mametchi bot.py `check_five_why` (2026-09-22 지적: 이전 단계의
답을 다음 물음으로 받으라는 지침이 지켜지지 않은 부검이 그대로 올라갔다).
A rule written only in the prompt has no effect when it is broken, so the
review flow checks the table before posting and asks for one rewrite.

Only what a machine can tell is checked: step count, empty cells, an answer
that repeats its question, adjacent answers that say the same thing, an
answer not carried into the next question, and a root cause that doesn't
summarize Why 5. The depth of the argument is not judged here.

Difference from mametchi: a first answer without a `## 5 Why` section
passes. The bundled postmortem_note.md has no 5 Why section; only a profile
prompt that asks for one (guchipachi's does) opts into the check. A rewrite
is checked with `required=True`, as mametchi always did: once the first
answer had the section, a rewrite that drops or renames it must not count
as passing.
"""

from __future__ import annotations

import re

# Step labels come as "Why 1"; spaces are removed before matching.
_STEP_RE = re.compile(r"^why([1-5])$", re.I)
# Korean words of two or more syllables, or identifier-shaped English.
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_./-]*|[가-힣]{2,}")
# Identifiers worth citing as evidence: file names and snake_case names.
_IDENT_RE = re.compile(
    r"[A-Za-z0-9_./-]+\.(?:py|md|json|sh|ya?ml|sql|toml|txt)"
    r"|[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+"
)
_SAME = 0.55


def md_section(text: str, title: str) -> str:
    """Text under '## title' up to the next '##'/'###' heading."""
    m = re.search(r"^#{2,3}\s*" + re.escape(title) + r"\s*$", text, re.M)
    if not m:
        return ""
    rest = text[m.end():]
    nxt = re.search(r"^#{2,3}\s", rest, re.M)
    return rest[: nxt.start()] if nxt else rest


def _table_rows(text: str) -> list[list[str]]:
    """The first markdown table's rows as cell lists, header rule dropped."""
    rows: list[list[str]] = []
    for line in text.splitlines():
        s = line.strip()
        if not (s.startswith("|") and s.endswith("|") and len(s) > 1):
            if rows:
                break
            continue
        cells = [c.strip() for c in s[1:-1].split("|")]
        if all(c and set(c) <= set("-: ") for c in cells):
            continue
        rows.append(cells)
    return rows


def _tokens(text: str) -> set[str]:
    """Comparison tokens. Korean particles and endings change a word's form
    (검사합니다 vs 검사), so the two-syllable stem is added too."""
    out: set[str] = set()
    for t in _TOKEN_RE.findall(text or ""):
        low = t.lower()
        out.add(low)
        if len(t) >= 3 and "가" <= t[0] <= "힣":
            out.add(low[:2])
    return out


def _two_grams(text: str) -> set[str]:
    s = re.sub(r"\s+", "", text or "")
    return {s[i : i + 2] for i in range(len(s) - 1)}


def _overlap(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def check_five_why(detail: str, *, required: bool = False) -> list[str]:
    """Problems found in the detail's 5 Why chain, as sentences. Empty means pass.

    `required` counts a missing section as a problem; without it a detail
    with no section passes.
    """
    section = md_section(detail or "", "5 Why")
    if not section.strip():
        return ["상세에 `## 5 Why` 절이 없다."] if required else []

    steps: list[tuple[int, str, str, str]] = []
    for cells in _table_rows(section):
        if len(cells) < 4:
            continue
        m = _STEP_RE.match(cells[0].replace(" ", ""))
        if m:
            steps.append((int(m.group(1)), cells[1], cells[2], cells[3]))

    bad: list[str] = []
    got = [n for n, _, _, _ in steps]
    if got != [1, 2, 3, 4, 5]:
        bad.append(f"Why 1 부터 5 까지 다섯 단계가 있어야 하는데 {got or '없음'} 이다.")
        if len(steps) < 2:
            return bad

    for n, q, a, ev in steps:
        if not q or not a:
            bad.append(f"Why {n} 의 물음이나 답 칸이 비어 있다.")
        elif _overlap(_two_grams(q), _two_grams(a)) >= _SAME or len(_tokens(a) - _tokens(q)) < 2:
            # Restating the question keeps 2-gram similarity near 0.5, so
            # the count of new tokens catches what similarity alone misses.
            bad.append(f"Why {n} 의 답이 물음을 되풀이한다. 답에 원인이 없다.")
        if not ev:
            bad.append(f"Why {n} 의 근거 칸이 비어 있다. 확인하지 못했으면 미확인이라 쓴다.")

    for (n, _, a, _), (n2, q2, a2, _) in zip(steps, steps[1:]):
        if a and q2 and not (_tokens(a) & _tokens(q2)):
            bad.append(
                f"Why {n} 의 답과 Why {n2} 의 물음이 이어지지 않는다. "
                "앞 단계의 답을 다음 물음으로 받는다."
            )
        if a and a2 and _overlap(_two_grams(a), _two_grams(a2)) >= _SAME:
            bad.append(
                f"Why {n} 과 Why {n2} 의 답이 같은 말이다. "
                "단계마다 앞에 없던 사실이 하나씩 더 나와야 한다."
            )

    # The root cause summarizes the last step. Falling back to an earlier
    # step means the five steps never reach the conclusion.
    if steps and steps[-1][0] == 5:
        _, _, last_answer, last_evidence = steps[-1]
        root = md_section(detail, "근본원인")
        if not root.strip():
            bad.append("`## 근본원인` 절이 없다.")
        else:
            idents = set(_IDENT_RE.findall(last_answer + " " + last_evidence))
            if idents and not (idents & set(_IDENT_RE.findall(root))):
                bad.append(
                    "근본원인이 Why 5 를 정리하지 않는다. "
                    f"Why 5 가 짚은 {', '.join(sorted(idents)[:3])} 가 근본원인에 없다."
                )
            elif not (_tokens(last_answer) & _tokens(root)):
                bad.append("근본원인이 Why 5 의 답과 이어지지 않는다.")
    return bad


def broken_five_why_prompt(problems: list[str], split_marker: str) -> str:
    """Rewrite request naming what broke. "Rewrite it" alone brings the same back."""
    items = "\n".join(f"- {p}" for p in problems)
    return (
        "방금 낸 부검의 5 Why 가 사슬로 이어지지 않는다.\n\n"
        f"{items}\n\n"
        "5 Why 는 다섯 칸을 채우는 서식이 아니다. 한 단계 내려갈 때마다 "
        "앞 단계에서 확인하지 못한 사실이 하나씩 더 나와야 한다.\n"
        "Why 1 의 답을 Why 2 의 물음으로 받고, 그 답을 Why 3 의 물음으로 받는다.\n"
        "답 칸에 현상을 다시 적지 않는다. 그 현상이 나온 까닭을 적는다.\n"
        "같은 사실을 말만 바꿔 여러 단계에 나누어 적지 않는다.\n"
        "근본원인 문단은 Why 5 의 답을 정리한다. 앞 단계로 되돌아가지 않는다.\n\n"
        "부검 전체를 시스템 프롬프트의 형식대로 다시 쓴다. "
        f"`{split_marker}` 구분선도 그대로 둔다.\n"
        "이미 확인한 사실은 그대로 쓴다. 근거 없는 단계를 지어내지 않는다. "
        "더 내려갈 근거가 없으면 그 칸의 근거에 미확인이라 쓴다."
    )
