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

    def override(self, values: Mapping[str, Any]) -> "RuntimeSettings":
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
