"""원본 대비 대조 시험.

목적은 마이그레이션 과정에서 원본(`bot.py`)의 실측 상수와 판정 규칙이
조용히 바뀌지 않았음을 코드로 고정하는 것이다. 각 시험은 원본 값을
리터럴로 적고 원본 몇 번째 줄에서 왔는지 주석으로 남긴다.

조직 고유값(채널 ID, 채널명, 개발자 경로, 사내 도구 이름)은 이 파일
어디에도 두지 않는다. 그런 값은 프로필로 분리하기로 한 설계 결정이고
대조 대상이 아니다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from slack_cli_agent.auth.policy import EFFORT_LEVELS, OWNER_EFFORT_MIN
from slack_cli_agent.config.channel import CHAT_DEFAULT
from slack_cli_agent.config.profile import Profile
from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.engine.base import (
    Engine,
    EngineRequest,
    EngineResponse,
    TrustLevel,
    UsageLimit,
)
from slack_cli_agent.engine.runner import EngineRunner, FallbackEngine
from slack_cli_agent.engine.switcher import EngineSwitcher
from slack_cli_agent.guard.watch import (
    PROMISE_WITHOUT_WATCH_RE,
    WATCH_DONE_TAG,
    WATCH_MARK_EMOJI,
    WATCH_RE,
    WATCH_STILL_TAG,
)
from slack_cli_agent.observability.slow_report import SessionContextCalculator
from slack_cli_agent.reliability.catchup import (
    CATCHUP_ALERT_AFTER_SEC,
    CATCHUP_MAX_THREADS_PER_CHANNEL,
    CATCHUP_RETRY_BACKOFF,
    DONE_EMOJI,
)
from slack_cli_agent.reliability.health import SOCKET_ERROR_WINDOW_SEC
from slack_cli_agent.render.blocks import SPLIT_MARKER, BlockBuilder
from slack_cli_agent.render.splitter import ATOMIC_HEADS
from slack_cli_agent.review.base import REVIEW_SPLIT
from slack_cli_agent.slack.attachments import AttachmentStore
from slack_cli_agent.slack.gate import ASKED_BACK, REACTION_MAX_LEN, ResponseGate
from slack_cli_agent.slack.reactions import (
    DEBUG_TRACE_EMOJI,
    FORMAT_REVIEW_EMOJI,
    POSTMORTEM_EMOJI,
    SILENT_MARK_EMOJI,
    UNFINISHED_EMOJI,
)

SETTINGS = RuntimeSettings()


class Test타임아웃과_대기_상수:
    """원본 bot.py:583, 587, 591 의 시간 상수와 같아야 한다."""

    def test_엔진_실행_타임아웃이_900초다(self) -> None:
        # 원본 bot.py:583 TIMEOUT_SEC = 900
        assert SETTINGS.request_timeout_sec == 900

    def test_느린_요청_보고_문턱이_800초다(self) -> None:
        # 원본 bot.py:587 SLOW_REPORT_SEC = 800
        assert SETTINGS.slow_report_sec == 800

    def test_수면_공백_의심_문턱이_30초다(self) -> None:
        # 원본 bot.py:591 SLEEP_GAP_SUSPECT_SEC = 30
        assert SETTINGS.sleep_gap_suspect_sec == 30


class Test동시성과_출력_상수:
    """원본 bot.py:679, 680, 4025 와 같아야 한다."""

    def test_동시_처리_상한이_10이다(self) -> None:
        # 원본 bot.py:679 MAX_CONCURRENT = 10 (2026-09-01 3에서 상향)
        assert SETTINGS.max_concurrent == 10

    def test_평문_채널_한_덩어리_상한이_3500자다(self) -> None:
        # 원본 bot.py:680 SLACK_CHUNK = 3500
        assert SETTINGS.slack_chunk == 3500

    def test_리치_채널_markdown_블록_상한이_12000자다(self) -> None:
        # 원본 bot.py:4025 MARKDOWN_BLOCK_LIMIT = 12000
        assert SETTINGS.markdown_block_limit == 12000


class Test세션_유지_기간:
    """원본 bot.py:677, 678 과 같아야 한다."""

    def test_스레드_단위_세션_유지가_24시간이다(self) -> None:
        # 원본 bot.py:677 SESSION_TTL_HOURS = 24
        assert SETTINGS.session_ttl_hours == 24

    def test_채널_단위_세션_유지가_7일이다(self) -> None:
        # 원본 bot.py:678 CHANNEL_SESSION_TTL_DAYS = 7
        assert SETTINGS.channel_session_ttl_days == 7


class Test되짚기_창_상수:
    """원본 bot.py:859, 861, 909, 936 과 같아야 한다."""

    def test_되짚기_기본_창이_7200초다(self) -> None:
        # 원본 bot.py:859 CATCHUP_WINDOW_SEC = 7200
        assert SETTINGS.catchup_window_sec == 7200

    def test_되짚기_최대_창이_86400초다(self) -> None:
        # 원본 bot.py:861 CATCHUP_MAX_WINDOW_SEC = 86400
        assert SETTINGS.catchup_max_window_sec == 86400

    def test_스레드_되짚기_창이_7일이다(self) -> None:
        # 원본 bot.py:909 CATCHUP_THREAD_LOOKBACK_SEC = 7 * 86400
        assert SETTINGS.catchup_thread_lookback_sec == 7 * 86400

    def test_슬랙_읽기_지연_유예가_120초다(self) -> None:
        # 원본 bot.py:936 CATCHUP_GRACE_SEC = 120
        assert SETTINGS.catchup_grace_sec == 120


class Test기록_읽기_상수:
    """원본 bot.py:938, 939, 950, 2546, 2547, 2985 와 같아야 한다."""

    def test_기록_읽기_재시도가_3회다(self) -> None:
        # 원본 bot.py:938 HISTORY_READ_TRIES = 3
        assert SETTINGS.history_read_tries == 3

    def test_기록_읽기_재시도_사이_대기가_2초다(self) -> None:
        # 원본 bot.py:939 HISTORY_READ_PAUSE_SEC = 2.0
        assert SETTINGS.history_read_pause_sec == 2.0

    def test_기록_읽기_최소_간격이_2초다(self) -> None:
        # 원본 bot.py:950 HISTORY_MIN_INTERVAL_SEC = 2.0 (너무 빨리 부르면 429 대신
        # ok 와 빈 목록이 온다)
        assert SETTINGS.history_min_interval_sec == 2.0

    def test_기록에_담을_최대_메시지_수가_40이다(self) -> None:
        # 원본 bot.py:2546 HISTORY_MAX_MSGS = 40
        assert SETTINGS.history_max_msgs == 40

    def test_기록에_담을_최대_글자_수가_12000이다(self) -> None:
        # 원본 bot.py:2547 HISTORY_MAX_CHARS = 12000
        assert SETTINGS.history_max_chars == 12000

    def test_링크로_엮는_스레드_상한이_3이다(self) -> None:
        # 원본 bot.py:2985 LINKED_THREAD_MAX = 3
        assert SETTINGS.linked_thread_max == 3


class Test소켓_안정성_상수:
    """원본 bot.py:864, 848, 880, 887, 872 와 같아야 한다."""

    def test_상태_점검_주기가_30초다(self) -> None:
        # 원본 bot.py:864 HEALTH_INTERVAL_SEC = 30
        assert SETTINGS.health_interval_sec == 30

    def test_종료_유예가_330초다(self) -> None:
        # 원본 bot.py:848 SHUTDOWN_GRACE_SEC = 330
        assert SETTINGS.shutdown_grace_sec == 330

    def test_재연결_상한이_4회다(self) -> None:
        # 원본 bot.py:880 SOCKET_RECONNECT_LIMIT = 4
        # (정상 4시간 35분에 0회, 장애 22분에 128회 관측)
        assert SETTINGS.socket_reconnect_limit == 4

    def test_소켓_오류_상한이_8회다(self) -> None:
        # 원본 bot.py:887 SOCKET_ERROR_LIMIT = 8
        # (이전 20건/180초는 실제 발생률 16.8건보다 높아 한 번도 발화하지 않았다)
        assert SETTINGS.socket_error_limit == 8

    def test_소켓_오류_집계_창이_180초다(self) -> None:
        # 원본 bot.py:872 SOCKET_ERROR_WINDOW_SEC = 180
        assert SOCKET_ERROR_WINDOW_SEC == 180


class Test감시_큐_상수:
    """원본 bot.py:970, 973, 975 와 같아야 한다."""

    def test_감시_점검_주기가_300초다(self) -> None:
        # 원본 bot.py:970 WATCH_CHECK_INTERVAL_SEC = 300
        assert SETTINGS.watch_check_interval_sec == 300

    def test_감시_작업_최소_간격이_300초다(self) -> None:
        # 원본 bot.py:973 WATCH_JOB_MIN_GAP_SEC = 300
        assert SETTINGS.watch_job_min_gap_sec == 300

    def test_감시_작업_최대_수명이_24시간이다(self) -> None:
        # 원본 bot.py:975 WATCH_JOB_MAX_AGE_SEC = 24 * 3600
        assert SETTINGS.watch_job_max_age_sec == 24 * 3600


class Test늦은_재작성_상수:
    """원본 bot.py:2962, 2964 와 같아야 한다."""

    def test_늦은_재작성_최소_비율이_0点6이다(self) -> None:
        # 원본 bot.py:2962 LATE_REWRITE_MIN_RATIO = 0.6
        assert SETTINGS.late_rewrite_min_ratio == 0.6

    def test_늦은_재작성_최소_글자_수가_200이다(self) -> None:
        # 원본 bot.py:2964 LATE_REWRITE_MIN_CHARS = 200
        assert SETTINGS.late_rewrite_min_chars == 200


class Test진행_표시_상수:
    """원본 bot.py:4735, 4738 과 같아야 한다."""

    def test_진행_표시_갱신_주기가_3초다(self) -> None:
        # 원본 bot.py:4735 PROGRESS_TICK_SEC = 3
        assert SETTINGS.progress_tick_sec == 3

    def test_진행_표시_유휴_판정이_45초다(self) -> None:
        # 원본 bot.py:4738 PROGRESS_IDLE_SEC = 45
        assert SETTINGS.progress_idle_sec == 45


class TestEffort_단계와_소유자_하한:
    """원본 bot.py:176, 181 과 같아야 한다."""

    def test_effort_단계_순서와_구성이_같다(self) -> None:
        # 원본 bot.py:176 EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
        assert EFFORT_LEVELS == ("low", "medium", "high", "xhigh", "max")

    def test_소유자_effort_하한이_medium이다(self) -> None:
        # 원본 bot.py:181 OWNER_EFFORT_MIN = "medium"
        assert OWNER_EFFORT_MIN == "medium"


class Test채팅_모드_기본값:
    def test_채널_기본_대화량이_normal이다(self) -> None:
        # 원본 bot.py:1388 CHAT_DEFAULT = "normal"
        assert CHAT_DEFAULT == "normal"


class Test첨부_저장_상수:
    """원본 bot.py:88, 89, 91 과 같아야 한다. AttachmentStore 기본값으로 옮겨졌다."""

    def _store(self, tmp_path: Path) -> AttachmentStore:
        return AttachmentStore(
            attach_dir=tmp_path, token_provider=lambda: "tok",
            downloader=lambda url, token: None,  # type: ignore[return-value]
        )

    def test_최대_첨부_파일_수가_5다(self, tmp_path: Path) -> None:
        # 원본 bot.py:88 ATTACH_MAX_FILES = 5
        assert self._store(tmp_path)._max_files == 5

    def test_최대_첨부_바이트가_20메가바이트다(self, tmp_path: Path) -> None:
        # 원본 bot.py:89 ATTACH_MAX_BYTES = 20 * 1024 * 1024
        assert self._store(tmp_path)._max_bytes == 20 * 1024 * 1024

    def test_첨부_보관_기간이_48시간이다(self, tmp_path: Path) -> None:
        # 원본 bot.py:91 ATTACH_KEEP_HOURS = 48
        assert self._store(tmp_path)._keep_hours == 48


class Test상태_표식_이모지:
    """원본 bot.py:602, 616, 618, 621, 631, 640 과 같아야 한다."""

    def test_침묵_표식_이모지가_zipper_mouth_face다(self) -> None:
        # 원본 bot.py:602 SILENT_MARK_EMOJI = "zipper_mouth_face"
        assert SILENT_MARK_EMOJI == "zipper_mouth_face"

    def test_완료_표식_집합에_체크와_침묵이_들어있다(self) -> None:
        # 원본 bot.py:616 DONE_EMOJI = frozenset({"white_check_mark", SILENT_MARK_EMOJI})
        assert DONE_EMOJI == frozenset({"white_check_mark", "zipper_mouth_face"})

    def test_미완료_표식_집합이_eyes_hourglass_x다(self) -> None:
        # 원본 bot.py:618 UNFINISHED_EMOJI = frozenset({"eyes", "hourglass", "x"})
        assert UNFINISHED_EMOJI == frozenset({"eyes", "hourglass", "x"})

    def test_부검_이모지가_dango다(self) -> None:
        # 원본 bot.py:621 POSTMORTEM_EMOJI = "dango"
        assert POSTMORTEM_EMOJI == "dango"

    def test_디버그_추적_이모지가_brain이다(self) -> None:
        # 원본 bot.py:631 DEBUG_TRACE_EMOJI = "brain"
        assert DEBUG_TRACE_EMOJI == "brain"

    def test_서식_점검_이모지가_pencil2다(self) -> None:
        # 원본 bot.py:640 FORMAT_REVIEW_EMOJI = "pencil2"
        assert FORMAT_REVIEW_EMOJI == "pencil2"


class Test점검_구분선:
    def test_부검_디버그_서식점검_구분선이_모두_같다(self) -> None:
        # 원본 bot.py:625, 635, 644 POSTMORTEM_SPLIT/DEBUG_TRACE_SPLIT/
        # FORMAT_REVIEW_SPLIT 셋 모두 "===상세===" 로 같은 문자열이었다.
        # 신규 코드는 REVIEW_SPLIT 하나로 모았다.
        assert REVIEW_SPLIT == "===상세==="


class Test감시_태그와_정규식:
    """원본 bot.py:977, 978, 981, 983, 992 와 같아야 한다."""

    def test_watch_done_tag가_같다(self) -> None:
        assert WATCH_DONE_TAG == "[[WATCH_DONE]]"

    def test_watch_still_tag가_같다(self) -> None:
        assert WATCH_STILL_TAG == "[[WATCH_STILL]]"

    def test_watch_mark_emoji가_mag다(self) -> None:
        assert WATCH_MARK_EMOJI == "mag"

    def test_watch_태그_정규식_패턴_원문이_같다(self) -> None:
        # 원본 bot.py:983
        assert WATCH_RE.pattern == r"\n{0,2}\[\[WATCH:\s*(.+?)\s*\]\]\s*\Z"

    def test_빈_약속_검출_정규식_원문이_같다(self) -> None:
        # 원본 bot.py:992-995
        assert PROMISE_WITHOUT_WATCH_RE.pattern == (
            r"(지켜보|끝나면|완료되면|확인되면|반영되면|반영후|배포\s*후)"
            r".{0,20}(보고|말씀|알려|다시\s*답)"
        )


class Test되물음과_맞장구_판정:
    """원본 bot.py:983 이하 ASKED_BACK, REACTION_MAX_LEN 과 같아야 한다."""

    def test_asked_back_정규식_원문이_같다(self) -> None:
        # 원본 bot.py:5416 ASKED_BACK
        assert ASKED_BACK.pattern == r"(?:[?？]|까요|을까|ㄹ까|나요|린가요|드릴까)[\s.!]*$"

    def test_반응_최대_길이가_120자다(self) -> None:
        # 원본 bot.py:5476 REACTION_MAX_LEN = 120
        assert REACTION_MAX_LEN == 120

    def test_봇이_되물은_자리에서는_맞장구도_승인이다(self) -> None:
        """판정 규칙 : bot_asked=True 면 길이·어휘와 무관하게 답변 대상이다."""
        gate = ResponseGate()
        assert gate.worth_answering("ㅇㅇ", bot_asked=True) is True

    def test_괄호로_감싼_혼잣말은_되물은_자리에서도_거른다(self) -> None:
        """판정 순서 : 혼잣말 검사가 bot_asked 확인보다 먼저다."""
        gate = ResponseGate()
        assert gate.worth_answering("(하품)", bot_asked=True) is False


class Test분할_마커와_상수:
    """원본 bot.py:4028, 4156, 4335, 4336 과 같아야 한다."""

    def test_원자_덩어리_머리가_표_코드블록_인용이다(self) -> None:
        # 원본 bot.py:4028 ATOMIC_HEADS = ("|", "```", ">")
        assert ATOMIC_HEADS == ("|", "```", ">")

    def test_분할_마커가_같다(self) -> None:
        # 원본 bot.py:4156 SPLIT_MARKER = "<<<SPLIT>>>"
        assert SPLIT_MARKER == "<<<SPLIT>>>"

    def test_보조_줄_최대_줄_수가_3이다(self) -> None:
        # 원본 bot.py:4335 CONTEXT_MAX_LINES = 3
        assert BlockBuilder.CONTEXT_MAX_LINES == 3

    def test_보조_줄_최대_글자_수가_120이다(self) -> None:
        # 원본 bot.py:4336 CONTEXT_MAX_CHARS = 120
        assert BlockBuilder.CONTEXT_MAX_CHARS == 120


class Test되짚기_재시도_상수:
    """원본 bot.py:957, 959, 900, 960 과 같아야 한다."""

    def test_재시도_대기_간격이_같다(self) -> None:
        # 원본 bot.py:957 CATCHUP_RETRY_BACKOFF = (30, 60, 120, 300, 600)
        assert CATCHUP_RETRY_BACKOFF == (30, 60, 120, 300, 600)

    def test_경보_문턱이_1800초다(self) -> None:
        # 원본 bot.py:959 CATCHUP_ALERT_AFTER_SEC = 1800
        assert CATCHUP_ALERT_AFTER_SEC == 1800

    def test_채널당_최대_스레드_수가_두_원본값_중_큰_쪽이다(self) -> None:
        """원본은 CATCHUP_MAX_PER_CHANNEL=20 과 CATCHUP_MAX_THREADS_PER_CHANNEL=60
        둘을 각각 다른 자리에 썼다(bot.py:900, 960). 신규 코드는 이 둘을
        하나로 합치며 큰 쪽인 60 을 그대로 썼다 — 의도한 통합이고 근거는
        catchup.py 모듈 주석에 있다. 작은 쪽 20 이 아니라 60 인 것을 고정한다.
        """
        assert CATCHUP_MAX_THREADS_PER_CHANNEL == 60

    def test_완료_표식_집합이_되짚기_모듈에서도_같다(self) -> None:
        assert DONE_EMOJI == frozenset({"white_check_mark", "zipper_mouth_face"})


class TestEngineProbe_주기와_문구:
    """원본 bot.py:1722, 1724 와 같아야 한다."""

    def test_기본_실행기_재확인_주기가_600초다(self) -> None:
        # 원본 bot.py:1722 ENGINE_PROBE_SEC = 600
        assert EngineSwitcher.DEFAULT_PROBE_INTERVAL_SEC == 600.0

    def test_probe_prompt_문구가_같다(self) -> None:
        # 원본 bot.py:1724 ENGINE_PROBE_PROMPT = "준비됐으면 OK 두 글자만 답해라."
        assert FallbackEngine.PROBE_PROMPT == "준비됐으면 OK 두 글자만 답해라."


# ---------------------------------------------------------------------------
# 불일치를 발견하고 고친 자리.
#
# 원본 bot.py:1725 ENGINE_PROBE_TIMEOUT = 120 은 대체 실행기가 실제로 쓸 수
# 있는지 확인하는 짧은 요청 전용 타임아웃이다(bot.py:1774 probe_engine() 의
# timeout=ENGINE_PROBE_TIMEOUT). 이 요청은 사람이 기다리는 요청 처리 경로
# 안에서 인라인으로 실행된다(FallbackEngine._run_primary → _probe_secondary).
#
# 이관 전 상태에서는 EngineRunner.run() 이 항상 RuntimeSettings.request_timeout_sec
# (900초)를 썼다. 대체 실행기가 응답 없이 걸리면, 원본은 120초 만에 그 사실을
# 알아채 한도 소진 안내를 냈을 것을 신규 코드는 최대 900초까지 기다린 뒤에야
# 같은 결론을 낸다 — 사람이 기다리는 턴이 780초 더 길어질 수 있는 회귀다.
#
# 아래 시험은 그 회귀를 고정한 뒤의 상태를 검증한다. RED 상태에서 이
# 시험을 돌리면 probe 호출도 900초로 넘어가 실패한다(보고서에 실패 출력 인용).


class FakeCompleted:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class RecordingEngine(Engine):
    name = "fake"

    def __init__(self, profile, settings, response: EngineResponse | None = None) -> None:
        super().__init__(profile, settings)
        self._response = response

    def build_command(self, request: EngineRequest) -> list[str]:
        return ["fake-bin", request.prompt]

    def parse(self, stdout: str, stderr: str, returncode: int) -> EngineResponse:
        if self._response is not None:
            return self._response
        return EngineResponse(
            ok=(returncode == 0), body=stdout, session_id=None, model_actual=None,
            elapsed=0.0, turns=None, usage=None,
        )

    def new_session_id(self) -> str:
        return "fake-session"

    def detect_usage_limit(self, response: EngineResponse) -> UsageLimit | None:
        if response.failure_reason == "usage_limit":
            return UsageLimit(detail=response.body, source="hint")
        return None


def _profile(tmp_path: Path) -> Profile:
    return Profile.from_dict({
        "name": "example",
        "primary_engine": {"type": "claude", "binary": "claude", "model": "claude-sonnet-5"},
        "fallback_engine": {"type": "codex", "binary": "codex", "model": "gpt-5.6-sol", "options": {}},
        "owner_user_id": "U1",
        "troubleshoot_channel": "C1",
        "state_dir": str(tmp_path / "state"),
    })


def _request() -> EngineRequest:
    return EngineRequest(
        prompt="안녕", system_prompt="", session_id="s", resume=False,
        model="claude-sonnet-5", effort="medium", workdir=Path("/tmp/work"),
        readable_dirs=(), allowed_tools=(), trust_level=TrustLevel.GENERAL,
    )


class TestEngineProbe_타임아웃:
    def test_대체_실행기_probe_호출은_120초_타임아웃을_쓴다(self, tmp_path: Path) -> None:
        """원본 bot.py:1725, 1774 ENGINE_PROBE_TIMEOUT=120 이 실제로 짧은
        요청에만 걸리는지 확인한다. 정상 요청은 여전히 900초를 쓴다.
        """
        limit_response = EngineResponse(
            ok=False, body="한도 소진", session_id=None, model_actual=None,
            elapsed=0, turns=None, usage=None, failure_reason="usage_limit",
        )
        probe_ok = EngineResponse(ok=True, body="OK", session_id=None,
                                  model_actual=None, elapsed=0, turns=None, usage=None)
        profile = _profile(tmp_path)
        primary = RecordingEngine(profile, SETTINGS, response=limit_response)
        primary.name = "claude"
        secondary = RecordingEngine(profile, SETTINGS, response=probe_ok)
        secondary.name = "codex"
        switcher = EngineSwitcher(tmp_path / "engine_state.json")

        seen_timeouts: list[Any] = []

        def fake_subprocess(cmd, cwd, timeout):
            seen_timeouts.append(timeout)
            return FakeCompleted(stdout="", returncode=0)

        runner = EngineRunner(SETTINGS, subprocess_runner=fake_subprocess)
        fallback = FallbackEngine(primary, secondary, switcher, runner)

        fallback.run(_request())

        # 1차 호출(정상 요청 타임아웃) 하나, probe 호출(짧은 타임아웃) 하나.
        assert len(seen_timeouts) == 2
        assert seen_timeouts[0] == SETTINGS.request_timeout_sec
        assert seen_timeouts[1] == 120


class Test사용량_노출_규칙:
    """원본 bot.py:670-673 `CONTEXT_LIMIT` 과 `OWNER_ONLY_CHANNELS`.

    둘 다 조직 고유값이라 이 저장소에는 비워 둔다. 기본값이 비어 있다는 것
    자체가 계약이다 — 채워 넣으면 사용량 행이 모든 프로필에서 나간다.
    """

    def test_컨텍스트_한도표의_기본값은_비어_있다(self) -> None:
        # 원본 bot.py:670 CONTEXT_LIMIT = {}. 추측한 값으로 비율을 만들지 않는다.
        assert dict(RuntimeSettings().context_limit) == {}

    def test_사용량_노출_채널의_기본값은_비어_있다(self) -> None:
        # 원본 bot.py:673 OWNER_ONLY_CHANNELS. 실제 채널 ID 는 프로필에서 준다.
        assert RuntimeSettings().owner_only_channels == frozenset()

    def test_한도를_모르면_비율을_안_낸다(self) -> None:
        """원본 bot.py:3400 근처 `session_context()` 의 한도 None 경로다.
        표에 없는 모델에 임의 한도를 쓰면 소진 임박 판정이 틀린다.
        """
        class 빈기록:
            def read(self, session_id: str) -> list:
                return []

        calculator = SessionContextCalculator(빈기록(), context_limit={})
        assert calculator.compute("s1", model="어떤모델").limit is None
