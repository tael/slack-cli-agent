"""실행 상수. 기본값은 원본 운영에서 측정한 값이고 근거를 주석으로 남긴다."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any


@dataclass(frozen=True)
class RuntimeSettings:
    # 300초는 150건 중 2건을 잘랐고 중앙값은 34초였다
    request_timeout_sec: float = 900
    slow_report_sec: float = 800
    # 벽시계와 monotonic 의 차이. 43분 보고가 실제 42초였던 사례
    sleep_gap_suspect_sec: float = 30
    max_concurrent: int = 10

    slack_chunk: int = 3500
    markdown_block_limit: int = 12000

    session_ttl_hours: int = 24
    channel_session_ttl_days: int = 7

    catchup_window_sec: float = 7200
    catchup_max_window_sec: float = 86400
    catchup_thread_lookback_sec: float = 7 * 86400
    # 슬랙 읽기 지연만 덮는 값. 3600 은 틀린 값이었다
    catchup_grace_sec: float = 120

    # 너무 빨리 부르면 429 대신 ok 와 빈 목록이 온다
    history_min_interval_sec: float = 2.0
    history_read_tries: int = 3
    history_read_pause_sec: float = 2.0
    history_max_msgs: int = 40
    history_max_chars: int = 12000
    linked_thread_max: int = 3

    # 정상 4시간 35분에 0회, 장애 22분에 128회
    socket_reconnect_limit: int = 4
    # 이전 20건/180초는 실제 발생률 16.8건보다 높아 한 번도 발화하지 않았다
    socket_error_limit: int = 8
    health_interval_sec: float = 30
    # 봇 신원(`auth_test`) 조회가 실패했을 때 다시 부르기까지의 간격.
    # 실패를 영구 캐시하면 일시 장애가 프로세스가 사는 내내 이어지는 오판이
    # 되고, 판정마다 다시 부르면 장애 중에 요청 수만큼 API 호출이 늘어난다.
    identity_retry_interval_sec: float = 60
    # 큐가 비었을 때 다음 조회까지 쉬는 시간. 0 으로 두면 워커가 빈 큐를
    # 쉬지 않고 조회해 한 코어를 계속 쓴다. 이 값만큼 응답이 늦어질 수
    # 있어, 사람이 못 느끼는 범위에서 가장 크게 잡는다.
    queue_idle_sleep_sec: float = 0.5
    shutdown_grace_sec: float = 330

    watch_check_interval_sec: float = 300
    watch_job_min_gap_sec: float = 300
    watch_job_max_age_sec: float = 24 * 3600

    # 원본 bot.py:80 PEOPLE_REFRESH_SEC. 계정 핸들-이름 명부를 다시 만드는 주기
    roster_refresh_sec: float = 12 * 3600

    late_rewrite_min_ratio: float = 0.6
    late_rewrite_min_chars: int = 200

    progress_tick_sec: float = 3
    progress_idle_sec: float = 45

    # 워커 갱신 주기와 정체 판정 기준. 판정은 주기의 3배
    heartbeat_interval_sec: float = 5
    heartbeat_stale_sec: float = 15
    job_max_attempts: int = 3
    # 끝난 작업을 보관하는 기간. 이 값을 넘긴 행은 지운다. 안 지우면
    # jobs 표가 계속 커진다. 되짚기 최대 창(catchup_max_window_sec)보다
    # 길어야 한다 — 짧으면 되짚기가 이미 답한 메시지를 미응답으로 보고
    # 다시 등록해 같은 답이 두 번 나간다.
    job_retention_sec: float = 7 * 86400
    # 끝난 작업을 정리하는 주기.
    job_purge_interval_sec: float = 3600
    # 받아 놓은 첨부를 지우는 간격. 원본은 기동 시 한 번만 지웠다 — 며칠 도는
    # 프로세스에서는 그 뒤에 받은 것이 계속 남는다.
    attachment_cleanup_interval_sec: float = 3600
    # 마치지 못한 되짚기를 다시 보는 간격. 원본은 건강 점검 주기에 얹어 돌렸다.
    catchup_retry_interval_sec: float = 30
    # 보내지 못한 보고를 다시 보내는 간격. 원본은 건강 점검 주기에 얹어 돌렸다.
    pending_report_flush_interval_sec: float = 30
    # 학습 배치를 돌릴지 판정하는 간격. 판정 자체는 파일 확인뿐이라 싸다 —
    # 실제로 도는 것은 하루 한 번이고, 그 날짜 판정은 learning/schedule.py 가 한다.
    learning_batch_interval_sec: float = 600
    # 그날 학습을 시작하는 KST 시각. 이 시각 전에는 그날 기록이 아직 다 안
    # 쌓였으므로 돌리지 않는다. 원본은 launchd 일정에 이 값이 있었다.
    learning_run_hour: int = 22
    # 학습 분석에 쓸 모델과 노력 수준. 원본 learn.py 는 `--model sonnet` 고정이었다.
    learning_model: str = "sonnet"
    learning_effort: str = "medium"
    # 반응을 모을 때 스레드 하나에서 읽는 메시지 수. 원본 learn.py 의 limit 50.
    learning_thread_reply_limit: int = 50

    # 추측한 값으로 퍼센트를 만들지 않는다. 비워 둔다
    context_limit: Mapping[str, int] = field(default_factory=dict)

    # 토큰 사용량 노출을 허용하는 채널 집합. 원본 bot.py:673 OWNER_ONLY_CHANNELS
    # 와 같다. 트러블슈팅 채널이 이 집합에 없으면 사용량 행을 아예 안 낸다.
    # 조직 고유 채널 ID 를 코드에 두지 않기 위해 기본값은 빈 집합이다.
    owner_only_channels: frozenset[str] = frozenset()

    # 느린 요청 시간 분해에서 한 구간의 사고 시간 상한을 어림하는 값.
    # 원본 bot.py:3193 ASSUMED_TOKENS_PER_SEC = 40 과 같다. 실제 처리량은
    # 요청마다 달라 이 값으로 나눈 결과는 근사치일 뿐 정밀 계측이 아니다.
    assumed_tokens_per_sec: float = 40

    # 답변에서 줄 단위로 지울 문구의 머리말 목록. 원본 bot.py drop_vooster 의
    # VOOSTER_HEAD(조직 텔레메트리 인사 줄)를 일반화했다. 그 문구 자체는
    # 조직 고유값이라 코드에 두지 않는다 — 기본값은 빈 목록이다.
    dropped_line_heads: tuple[str, ...] = ()

    def override(self, values: Mapping[str, Any]) -> RuntimeSettings:
        """프로필이 지정한 항목만 덮어쓴다. 모르는 키는 무시한다.

        집합 항목은 형식을 맞춰 넣는다. JSON 에는 집합 형식이 없어 목록으로
        오는데, 그대로 두면 선언한 타입과 실제 값이 달라진다.
        """
        known = set(self.__dataclass_fields__)
        taken = {k: v for k, v in values.items() if k in known}
        for key, value in taken.items():
            current = getattr(self, key)
            if isinstance(current, frozenset) and not isinstance(value, frozenset):
                taken[key] = frozenset(value)
            elif isinstance(current, tuple) and not isinstance(value, tuple):
                taken[key] = tuple(value)
        return replace(self, **taken)
