"""`python -m slack_cli_agent.registry <root>`: 소스·workflow 레지스트리 1회 점검."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from .check import format_table, run_check
from .health import DEFAULT_TIMEOUT_SEC


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m slack_cli_agent.registry", description=__doc__)
    parser.add_argument("root", type=Path, help="sources/ 와 workflows/ 가 있는 디렉터리")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SEC, help="health 명령 제한 시간(초)")
    args = parser.parse_args(argv)
    rows, code = run_check(args.root.expanduser(), timeout=args.timeout)
    print(format_table(rows))
    failed = sum(1 for r in rows if r.status in ("fail", "reject"))
    print(f"\n합계 {len(rows)}건, 실패·거부 {failed}건")
    return code


if __name__ == "__main__":
    sys.exit(main())
