"""재개 턴이 세션이 이미 받은 지침 블록을 다시 싣지 않는다 (C15).

codex 와 gemini 는 재개 턴마다 이번 턴 지침을 본문에 다시 싣는다(sca-ivs).
private-taeil 지식 65KB 가 재개마다 다시 나간다. 내용이 같은 큰 블록만 참조로
바꾸고, 바뀐 블록과 작은 블록은 그대로 보낸다.
"""

from __future__ import annotations

from pathlib import Path

from test_engine import SETTINGS, codex_profile, gemini_profile, request

from slack_cli_agent.engine.base import EngineResponse
from slack_cli_agent.engine.codex import CodexEngine
from slack_cli_agent.engine.footprint import utf8_bytes
from slack_cli_agent.engine.gemini import GeminiEngine
from slack_cli_agent.engine.instruction_replay import InstructionLedger, split_blocks

지식 = "# 이 채널의 지식\n\n" + "정산 마감은 CREATED_AT 기준이다. " * 600 + "\n\n"
페르소나 = "# 페르소나\n\n" + "건조체로 답한다. " * 300 + "\n\n"


def 지침(질문자: str = "김태일") -> str:
    return 페르소나 + 지식 + f"# 이번 요청\n\n질문자 : {질문자}\n\n"


def 성공(session_id: str) -> EngineResponse:
    return EngineResponse(ok=True, body="답", session_id=session_id, model_actual=None,
                          elapsed=1.0, turns=None, usage=None)


def 실패(session_id: str) -> EngineResponse:
    return EngineResponse(ok=False, body="", session_id=session_id, model_actual=None,
                          elapsed=1.0, turns=None, usage=None, failure_reason="nonzero_exit")


def 첫턴(sid: str = "T1"):
    return request(resume=False, session_id=sid, system_prompt=지침(), model="m")


def 재개(sid: str = "T1", 질문자: str = "김태일"):
    return request(resume=True, session_id=sid, system_prompt=지침(질문자), model="m")


class Test블록_분할:
    def test_합치면_원문과_같다(self) -> None:
        assert "".join(split_blocks(지침())) == 지침()

    def test_제목마다_나눈다(self) -> None:
        assert len(split_blocks(지침())) == 3


class Test원장:
    def test_기록이_없으면_그대로_보낸다(self) -> None:
        assert InstructionLedger().reduce("T1", 지침()) == 지침()

    def test_같은_큰_블록만_참조로_바꾼다(self) -> None:
        원장 = InstructionLedger()
        원장.record("T1", 지침("김태일"))
        줄인것 = 원장.reduce("T1", 지침("정희윤"))
        assert "정산 마감은" not in 줄인것
        assert "「# 이 채널의 지식」" in 줄인것
        assert "질문자 : 정희윤" in 줄인것

    def test_바뀐_블록은_다시_싣는다(self) -> None:
        원장 = InstructionLedger()
        원장.record("T1", 지침())
        바뀐 = 지침().replace("CREATED_AT", "SUBMITTED_AT")
        assert "SUBMITTED_AT" in 원장.reduce("T1", 바뀐)

    def test_다른_세션에는_적용하지_않는다(self) -> None:
        원장 = InstructionLedger()
        원장.record("T1", 지침())
        assert 원장.reduce("T2", 지침()) == 지침()

    def test_오래된_세션부터_잊는다(self) -> None:
        원장 = InstructionLedger(max_sessions=1)
        원장.record("T1", 지침())
        원장.record("T2", 지침())
        assert 원장.reduce("T1", 지침()) == 지침()


class Test코덱스_재개턴:
    def test_재개턴_바이트가_첫턴의_3분의1_이하다(self, tmp_path: Path) -> None:
        엔진 = CodexEngine(codex_profile(tmp_path), SETTINGS)
        첫 = 엔진.footprint_for(첫턴())
        엔진.note_delivered(첫턴(), 성공("T1"))
        다음 = 엔진.footprint_for(재개())
        assert 다음.total_bytes * 3 <= 첫.total_bytes
        prompt = 엔진.build_command(재개())[-1]
        assert utf8_bytes(prompt) * 3 <= 첫.total_bytes
        assert "질문자 : 김태일" in prompt

    def test_실패한_턴은_기록하지_않는다(self, tmp_path: Path) -> None:
        엔진 = CodexEngine(codex_profile(tmp_path), SETTINGS)
        엔진.note_delivered(첫턴(), 실패("T1"))
        assert "정산 마감은" in 엔진.build_command(재개())[-1]

    def test_재개턴_내용은_기록하지_않는다(self, tmp_path: Path) -> None:
        """재개 턴 본문은 압축으로 세션에서 줄어들 수 있다. 첫 턴의
        developer_instructions 만 세션에 고정된다."""
        엔진 = CodexEngine(codex_profile(tmp_path), SETTINGS)
        엔진.note_delivered(재개(), 성공("T1"))
        assert "정산 마감은" in 엔진.build_command(재개())[-1]

    def test_첫턴은_줄이지_않는다(self, tmp_path: Path) -> None:
        엔진 = CodexEngine(codex_profile(tmp_path), SETTINGS)
        엔진.note_delivered(첫턴(), 성공("T1"))
        명령 = " ".join(엔진.build_command(첫턴()))
        assert "정산 마감은" in 명령


class Test제미나이_재개턴:
    def test_재개턴_바이트가_첫턴의_3분의1_이하다(self, tmp_path: Path) -> None:
        엔진 = GeminiEngine(gemini_profile(tmp_path), SETTINGS)
        첫 = 엔진.footprint_for(첫턴())
        엔진.note_delivered(첫턴(), 성공("C1"))
        다음턴 = 재개("C1")
        assert 엔진.footprint_for(다음턴).total_bytes * 3 <= 첫.total_bytes
        cmd = 엔진.build_command(다음턴)
        prompt = cmd[cmd.index("-p") + 1]
        assert "정산 마감은" not in prompt
        assert "질문자 : 김태일" in prompt

    def test_엔진이_발급한_대화_id로_기록한다(self, tmp_path: Path) -> None:
        """첫 턴의 session_id 는 자리표시자이고 실제 id 는 응답에서 온다."""
        엔진 = GeminiEngine(gemini_profile(tmp_path), SETTINGS)
        엔진.note_delivered(첫턴("자리표시자"), 성공("실제"))
        cmd = 엔진.build_command(재개("실제"))
        assert "정산 마감은" not in cmd[cmd.index("-p") + 1]


class Test하위_제목:
    def test_하위_제목에서는_나누지_않는다(self) -> None:
        """지식 파일 하나 안의 ## 마다 나누면 작은 블록이 기준 아래로 떨어져
        대부분 다시 실린다."""
        본문 = "# 파일\n\n" + "가" * 400 + "\n\n## 소제목\n\n" + "나" * 400 + "\n\n"
        assert split_blocks(본문) == [본문]


class Test실행기가_기록한다:
    def test_성공한_턴_뒤_재개턴이_줄어든다(self, tmp_path: Path) -> None:
        from test_engine import FakeCompleted, 통과정책

        from slack_cli_agent.engine.runner import EngineRunner

        class 응답고정(GeminiEngine):
            def prepare(self, request) -> None:
                pass

            def parse(self, stdout: str, stderr: str, returncode: int) -> EngineResponse:
                return 성공("C1")

        엔진 = 응답고정(gemini_profile(tmp_path), SETTINGS)
        보낸것: list[list[str]] = []

        def fake_subprocess(cmd, cwd, timeout, env=None):
            보낸것.append(cmd)
            return FakeCompleted(stdout="", returncode=0)

        실행기 = EngineRunner(SETTINGS, subprocess_runner=fake_subprocess,
                           environment_policy=통과정책())
        실행기.run(엔진, 첫턴("C1"))
        실행기.run(엔진, 재개("C1"))
        첫, 다음 = (cmd[cmd.index("-p") + 1] for cmd in 보낸것)
        assert "정산 마감은" in 첫
        assert "정산 마감은" not in 다음
        assert utf8_bytes(다음) * 3 <= utf8_bytes(첫)
