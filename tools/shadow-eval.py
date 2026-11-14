"""그림자 답을 슬랙에 올리지 않고 돌려 원본 스레드의 실제 답과 대조한다 (G5).

    ~/.<봇>/venv/bin/python tools/shadow-eval.py <프로필> [--target C...] [--days N]
        [--limit N] [--ts TS ...] [--workers N] [--out DIR]

마메치 shadow_eval.py 를 코어 모듈(shadow/offline.py)로 옮긴 것이다. 채널 지침을
바꾼 뒤 실제 답 수준으로 나오는지 여러 질문으로 시험하는 용도다. 답은 실제
그림자 답과 같은 프롬프트, 같은 그림자 채널 지침, 같은 조회 전용 턴으로 만든다.

결과는 <상태 디렉터리>/shadow_eval/<시각>.json 파일로만 남는다. 원본은 소유자
사용자 세션으로 읽고, 읽기 메서드 밖의 슬랙 호출은 실행 전에 거부한다. 엔진
턴은 실행 중인 봇과 같은 동시 실행 자리를 기다린다.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from slack_cli_agent.cli import resolver_for
from slack_cli_agent.config.profile import Profile
from slack_cli_agent.core.application import Application
from slack_cli_agent.core.timezones import KST
from slack_cli_agent.plugin.hooks import engine_slot
from slack_cli_agent.shadow.offline import OfflineEvaluator, read_only, write_results
from slack_cli_agent.shadow.service import shadow_pairs

PROFILE_DIR = Path(__file__).resolve().parent.parent / "profiles"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("profile")
    ap.add_argument("--target", help="그림자 채널. shadow_source 가 지정된 채널이 하나면 생략한다")
    ap.add_argument("--days", type=float, default=14)
    ap.add_argument("--limit", type=int, default=12)
    ap.add_argument("--ts", nargs="*", default=())
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()

    profile = Profile.load(a.profile, [PROFILE_DIR])
    app = Application.from_profile(profile, resolver=resolver_for(profile))
    try:
        pairs = shadow_pairs(app.channels.all())
        if a.target is None and len(pairs) != 1:
            print(f"그림자 채널이 {len(pairs)}개다. --target 으로 지정한다 : {sorted(pairs)}", file=sys.stderr)
            return 2
        target = a.target or next(iter(pairs))
        source = pairs.get(target)
        if source is None:
            print(f"{target} 에 shadow_source 가 없다", file=sys.stderr)
            return 2
        owner = app._owner_client
        if owner is None:
            print("사용자 세션 토큰이 없어 원본 채널을 읽지 못한다", file=sys.stderr)
            return 2

        def source_api(method: str, **params: Any) -> Any:
            return owner.api_call(method, params=params).data

        evaluator = OfflineEvaluator(
            source_api=read_only(source_api),
            run_turn=app._shadow_turn,
            name_of=app.user_namer(),
            bot_name=profile.display_name,
            source=source,
            target=target,
            engine=engine_slot(app.concurrency_slots(), app._settings.request_timeout_sec),
            is_self=lambda m: bool(m.get("user")) and m.get("user") == app.identity.user_id,
        )
        msgs = evaluator.questions(days=a.days, limit=a.limit, only_ts=a.ts or ())
        results = evaluator.run(msgs, workers=a.workers)
        run_id = datetime.now(KST).strftime("%Y%m%d-%H%M%S")
        path = write_results(results, a.out or profile.paths.root / "shadow_eval", run_id)
    finally:
        app.close()

    for r in results:
        v = r["verdict"]
        print(f"{r['asked_at']} | 내용 {v.get('content', '판정 없음')} | 말투 {v.get('tone', '판정 없음')} "
              f"| 위반 {len(r['violations'])} | {r['question'][:40]!r}")
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
