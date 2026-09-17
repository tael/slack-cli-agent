"""학습 배치의 분석이 엔진과 무관하게 제안을 내는지 실측한다 (sca-dyb.5).

    ~/.<봇>/venv/bin/python tools/learning-probe.py shinji rei asuka

봇마다 venv 가 따로라 저장소의 python3 로는 안 돈다. 슬랙 토큰은 프로필의
자격 파일에서 읽으므로 환경변수를 따로 넣지 않아도 된다.

읽기만 한다. ProposalAnalyzer.analyze_channel 은 엔진을 부르고 결과를 해독할
뿐이라 학습 지식·제안 저장소·소유자 알림에 닿지 않는다.

판정 함정 — ok=False 가 곧 엔진 결함은 아니다. 사용량 한도와 형식 불일치를
나눠 봐야 하므로 failure.kind 와 원문 앞부분을 함께 찍는다.
"""
import sys
from pathlib import Path

from slack_cli_agent.config.profile import Profile
from slack_cli_agent.core.application import Application
from slack_cli_agent.learning.analyzer import ProposalAnalyzer

PROFILE_DIR = Path(__file__).resolve().parent.parent / "profiles"

기록 = """\
[10:01] <사용자> 어제 배포한 거 지표 어떻게 나왔어?
[10:02] <봇> 어제 배포분은 아직 관측 구간이 짧습니다. 오늘 18시에 다시 보겠습니다.
[10:05] <사용자> 그렇게 길게 쓰지 말고 짧게 답해라
[10:06] <봇> 알겠습니다.
"""


def 돌린다(이름: str) -> None:
    from slack_cli_agent.cli import resolver_for

    프로필 = Profile.load(이름, [PROFILE_DIR])
    app = Application.from_profile(프로필, resolver=resolver_for(프로필))
    try:
        분석기 = ProposalAnalyzer(
            app.engine_invoker,
            model=app._settings.learning_model or None,
            effort=app._settings.learning_effort,
            workdir=프로필.work_root,
            bot_name=프로필.display_name,
        )
        결과 = 분석기.analyze_channel("2026-09-17", "테스트채널", 기록, [])
    finally:
        app.close()

    엔진 = 프로필.primary_engine.type
    if 결과.ok:
        r = 결과.result
        print(f"[{이름}/{엔진}] 제안 나옴 — 문체 {len(r.writing_style)}건, "
              f"채널사실 {len(r.channel_facts)}건, 교정 {len(r.corrections)}건")
        for 항목 in list(r.writing_style) + list(r.corrections):
            print(f"    · {항목}")
        if r.note:
            print(f"    note: {r.note[:200]}")
    else:
        f = 결과.failure
        print(f"[{이름}/{엔진}] 실패 — kind={f.kind} reason={str(f.reason)[:400]}")


for 봇 in sys.argv[1:]:
    돌린다(봇)
