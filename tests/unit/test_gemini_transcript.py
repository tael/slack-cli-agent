"""Antigravity CLI(gemini)의 대화 기록 리더 (sca-ebp).

제미나이만 리더가 없어 NullTranscriptReader 로 갔고, 그래서 rei 의 느린 보고는
시간 분해가 늘 비어 있었다. 2026-09-19 03:01 의 실측 부검에서 관측했다.

기록 형식은 문서화돼 있지 않은 CLI 내부 구조다. 여기 고정한 값은 실제 파일
~/.rei/engine/gemini-home/.gemini/antigravity-cli/conversations/*.db 에서
읽어 확인한 것이다. CLI 판이 올라가면 깨질 수 있어, 깨진 것을 관측할 수
있게 하는 것까지가 이 시험의 대상이다.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from slack_cli_agent.engine.transcript import GeminiTranscriptReader

USER_STEP = 14
MODEL_STEP = 15
TOOL_STEP = 132


def varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def metadata_with_ts(ts: int) -> bytes:
    """실제 파일과 같은 모양 - field 1 이 protobuf Timestamp 하위 메시지다."""
    inner = b"\x08" + varint(ts)
    return b"\x0a" + varint(len(inner)) + inner


def metadata_with_precise_ts(seconds: int, nanos: int) -> bytes:
    inner = b"\x08" + varint(seconds) + b"\x10" + varint(nanos)
    return b"\x0a" + varint(len(inner)) + inner


def field(number: int, body: bytes) -> bytes:
    return varint(number << 3 | 2) + varint(len(body)) + body


def tool_payload(call_id: str, tool: str) -> bytes:
    """실제 파일의 중첩 - step_payload 5 -> 4 안에 call id(1)와 이름(2)이 있다."""
    call = field(1, call_id.encode()) + field(2, tool.encode())
    return field(5, field(1, b"\x08\x01") + field(4, call))


def write_db(root: Path, session_id: str, rows: list[tuple[int, int, bytes, bytes]]) -> Path:
    conversations = root / ".gemini" / "antigravity-cli" / "conversations"
    conversations.mkdir(parents=True, exist_ok=True)
    path = conversations / f"{session_id}.db"
    con = sqlite3.connect(path)
    con.execute(
        "create table steps (`idx` integer, `step_type` integer, `status` integer,"
        " `metadata` blob, `step_payload` blob, primary key (`idx`))"
    )
    con.executemany("insert into steps (`idx`,`step_type`,`metadata`,`step_payload`) values (?,?,?,?)", rows)
    con.commit()
    con.close()
    return path


class Test제미나이_기록을_읽는다:
    def test_step_마다_시각을_낸다(self, tmp_path: Path) -> None:
        write_db(tmp_path, "S1", [
            (0, USER_STEP, metadata_with_ts(1789754523), b""),
            (1, MODEL_STEP, metadata_with_ts(1789754530), b""),
        ])
        events = GeminiTranscriptReader(tmp_path / "일", home=tmp_path).read("S1")

        assert [event.ts for event in events] == [1789754523.0, 1789754530.0]

    def test_초_아래_자리까지_읽는다(self) -> None:
        """초로 자르면 구간 합이 총 구간을 넘어 비중이 100퍼센트를 넘는다."""
        from slack_cli_agent.engine.transcript import _gemini_step_ts

        assert _gemini_step_ts(metadata_with_precise_ts(1789754523, 140929000)) == 1789754523.140929

    def test_나노초가_없으면_초만_쓴다(self) -> None:
        from slack_cli_agent.engine.transcript import _gemini_step_ts

        assert _gemini_step_ts(metadata_with_ts(1789754523)) == 1789754523.0

    def test_step_종류를_역할로_옮긴다(self, tmp_path: Path) -> None:
        write_db(tmp_path, "S1", [
            (0, USER_STEP, metadata_with_ts(1), b""),
            (1, MODEL_STEP, metadata_with_ts(2), b""),
            (2, TOOL_STEP, metadata_with_ts(3), tool_payload("call_1", "list_dir")),
        ])
        events = GeminiTranscriptReader(tmp_path / "일", home=tmp_path).read("S1")

        assert [(event.role, event.kind) for event in events] == [
            ("user", "text"), ("assistant", "text"), ("assistant", "tool_use"),
        ]

    def test_도구_이름을_담는다(self, tmp_path: Path) -> None:
        write_db(tmp_path, "S1", [
            (0, TOOL_STEP, metadata_with_ts(3), tool_payload("call_44010", "find_by_name")),
        ])
        events = GeminiTranscriptReader(tmp_path / "일", home=tmp_path).read("S1")

        assert events[0].brief == "find_by_name"

    def test_기록이_없으면_빈_목록이다(self, tmp_path: Path) -> None:
        assert GeminiTranscriptReader(tmp_path / "일", home=tmp_path).read("없는세션") == []

    def test_시각을_못_읽은_step_은_버린다(self, tmp_path: Path) -> None:
        """형식이 바뀌면 여기로 온다. 예외를 내면 이미 끝난 요청 처리가 망가진다."""
        write_db(tmp_path, "S1", [
            (0, USER_STEP, b"\xff\xff", b""),
            (1, MODEL_STEP, metadata_with_ts(1789754530), b""),
        ])
        events = GeminiTranscriptReader(tmp_path / "일", home=tmp_path).read("S1")

        assert [event.ts for event in events] == [1789754530.0]

    def test_읽은_것보다_버린_것이_많으면_경고를_낸다(self, tmp_path: Path, caplog) -> None:
        """조용히 빈 목록이 되면 지금과 같아지고 아무도 모른다(코덱스 지적)."""
        write_db(tmp_path, "S1", [
            (0, USER_STEP, b"\xff\xff", b""),
            (1, MODEL_STEP, b"\xff\xff", b""),
            (2, TOOL_STEP, metadata_with_ts(3), b""),
        ])
        with caplog.at_level("WARNING"):
            GeminiTranscriptReader(tmp_path / "일", home=tmp_path).read("S1")

        assert any("기록 형식" in record.message for record in caplog.records)

    def test_파일이_깨져도_예외를_내지_않는다(self, tmp_path: Path) -> None:
        conversations = tmp_path / ".gemini" / "antigravity-cli" / "conversations"
        conversations.mkdir(parents=True)
        (conversations / "S1.db").write_bytes("이것은 sqlite 가 아니다".encode())

        assert GeminiTranscriptReader(tmp_path / "일", home=tmp_path).read("S1") == []


class Test도구_시간을_가를_수_없다고_알린다:
    """제미나이 기록은 도구 호출과 결과가 한 step 이라 그 둘을 못 가른다.
    가를 수 있는 척하면 보고가 "도구 실행 0.0초" 라는 거짓을 낸다."""

    def test_제미나이_리더는_도구_시간을_못_가른다(self, tmp_path: Path) -> None:
        assert GeminiTranscriptReader(tmp_path / "일", home=tmp_path).splits_tool_time is False

    def test_제미나이_리더는_출력_토큰을_안_낸다(self, tmp_path: Path) -> None:
        """DB 를 훑어 확인했다 - gen_metadata·executor_metadata·step_payload
        어디에도 토큰 수가 없다(2026-09-19)."""
        assert GeminiTranscriptReader(tmp_path / "일", home=tmp_path).reports_output_tokens is False

    def test_제미나이_리더는_캐시_사용량을_안_낸다(self, tmp_path: Path) -> None:
        """재시도 판정은 요청별 캐시 사용량 역산이다. 그 값이 없으면 판정 열이
        영원히 비어 있어, 재시도가 없는 것과 구분이 안 된다(sca-ron)."""
        assert GeminiTranscriptReader(tmp_path / "일", home=tmp_path).reports_cache_usage is False

    def test_다른_리더는_가른다(self, tmp_path: Path) -> None:
        from slack_cli_agent.engine.transcript import ClaudeTranscriptReader

        reader = ClaudeTranscriptReader(tmp_path / "일", home=tmp_path)
        assert reader.splits_tool_time is True
        assert reader.reports_output_tokens is True


class Test레지스트리에_붙어_있다:
    """리더를 만들어도 레지스트리에 안 붙이면 NullTranscriptReader 로 가서
    붙기 전과 똑같아진다. 단위 시험은 그것을 안 본다."""

    def test_gemini_이름으로_제미나이_리더가_나온다(self, tmp_path: Path) -> None:
        from slack_cli_agent.engine.transcript import TranscriptReaderRegistry

        reader = TranscriptReaderRegistry().create("gemini", tmp_path)

        assert isinstance(reader, GeminiTranscriptReader)

    def test_알려진_이름에_gemini_가_들어_있다(self) -> None:
        from slack_cli_agent.engine.transcript import TranscriptReaderRegistry

        assert "gemini" in TranscriptReaderRegistry().known_names()


class Test방어적으로_읽는다:
    """형식은 CLI 내부 구조다. 어긋난 바이트에 예외를 내면 이미 끝난 요청의
    보고가 통째로 날아간다(코덱스 지적)."""

    def test_시각_앞에_모르는_필드가_있어도_읽는다(self, tmp_path: Path) -> None:
        """protobuf 는 필드 순서를 보장하지 않는다. 머리 바이트만 보면 판이
        올라가 필드가 하나 붙는 순간 전부 버린다."""
        meta = field(9, b"\x01\x02") + metadata_with_ts(1789754523)
        write_db(tmp_path, "S1", [(0, USER_STEP, meta, b"")])

        events = GeminiTranscriptReader(tmp_path / "일", home=tmp_path).read("S1")

        assert [event.ts for event in events] == [1789754523.0]

    def test_길이가_남은_바이트보다_크면_버린다(self, tmp_path: Path) -> None:
        """잘린 값을 온전한 것으로 읽으면 없는 내용을 지어낸다."""
        meta = b"\x0a\x40" + b"\x08\x01"
        write_db(tmp_path, "S1", [(0, USER_STEP, meta, b"")])

        assert GeminiTranscriptReader(tmp_path / "일", home=tmp_path).read("S1") == []

    def test_끝나지_않는_varint_를_버린다(self, tmp_path: Path) -> None:
        meta = b"\x0a\x03" + b"\x08\xff\xff"
        write_db(tmp_path, "S1", [(0, USER_STEP, meta, b"")])

        assert GeminiTranscriptReader(tmp_path / "일", home=tmp_path).read("S1") == []

    def test_varint_이_지나치게_길면_버린다(self, tmp_path: Path) -> None:
        """길이 상한이 없으면 깨진 바이트가 터무니없는 수로 읽힌다."""
        inner = b"\x08" + b"\xff" * 12 + b"\x01"
        meta = b"\x0a" + varint(len(inner)) + inner
        write_db(tmp_path, "S1", [(0, USER_STEP, meta, b"")])

        assert GeminiTranscriptReader(tmp_path / "일", home=tmp_path).read("S1") == []

    def test_고정폭_필드를_건너뛴다(self, tmp_path: Path) -> None:
        """wire type 5(4바이트)와 1(8바이트)을 잘못 건너뛰면 뒤 필드가 밀린다."""
        meta = b"\x0d\x01\x02\x03\x04" + b"\x11" + b"\x00" * 8 + metadata_with_ts(1789754530)
        write_db(tmp_path, "S1", [(0, USER_STEP, meta, b"")])

        events = GeminiTranscriptReader(tmp_path / "일", home=tmp_path).read("S1")

        assert [event.ts for event in events] == [1789754530.0]


    def test_64비트를_넘는_varint_를_버린다(self, tmp_path: Path) -> None:
        """10바이트여도 마지막 바이트가 0x01 을 넘으면 64비트를 넘는다."""
        inner = b"\x08" + b"\x81" * 9 + b"\x02"
        meta = b"\x0a" + varint(len(inner)) + inner
        write_db(tmp_path, "S1", [(0, USER_STEP, meta, b"")])

        assert GeminiTranscriptReader(tmp_path / "일", home=tmp_path).read("S1") == []

    def test_epoch_0_도_시각으로_본다(self, tmp_path: Path) -> None:
        """0 을 없는 값으로 보면 1970-01-01 의 기록이 통째로 사라진다."""
        write_db(tmp_path, "S1", [
            (0, USER_STEP, metadata_with_ts(0), b""),
            (1, MODEL_STEP, metadata_with_ts(10), b""),
        ])
        events = GeminiTranscriptReader(tmp_path / "일", home=tmp_path).read("S1")

        assert [event.ts for event in events] == [0.0, 10.0]

    def test_음수_시각을_부호_있는_값으로_읽는다(self, tmp_path: Path) -> None:
        """Timestamp.seconds 는 int64 다. 부호를 안 풀면 1970 이전이 먼 미래가 된다."""
        inner = b"\x08" + varint((1 << 64) - 1)
        meta = b"\x0a" + varint(len(inner)) + inner
        write_db(tmp_path, "S1", [(0, USER_STEP, meta, b"")])

        events = GeminiTranscriptReader(tmp_path / "일", home=tmp_path).read("S1")

        assert [event.ts for event in events] == [-1.0]

    def test_그룹이_앞에_있으면_그_뒤를_안_읽는다(self, tmp_path: Path) -> None:
        """deprecated 그룹은 CLI 가 안 쓴다. 만나면 멈추는 것이 지금 선택이고,
        추측으로 건너뛰어 엉뚱한 필드를 읽는 것보다 낫다."""
        meta = b"\x0b" + metadata_with_ts(1789754530)
        write_db(tmp_path, "S1", [(0, USER_STEP, meta, b"")])

        assert GeminiTranscriptReader(tmp_path / "일", home=tmp_path).read("S1") == []


class Test실제_바이트로_고정한다:
    """조립한 모형만 쓰면 CLI 형식이 바뀌어도 시험이 안 깨진다(코덱스 지적).
    실물에서 뜬 바이트를 넣어 형식 변경이 시험으로 드러나게 한다."""

    def _raw(self, name: str) -> bytes:
        return (Path(__file__).parent.parent / "fixtures" / "gemini" / name).read_bytes()

    def test_실제_metadata_에서_시각을_읽는다(self, tmp_path: Path) -> None:
        write_db(tmp_path, "S1", [
            (0, USER_STEP, self._raw("step0_type14.metadata.bin"), b""),
            (1, MODEL_STEP, self._raw("step1_type15.metadata.bin"), b""),
            (2, TOOL_STEP, self._raw("step2_type132.metadata.bin"), b""),
        ])
        events = GeminiTranscriptReader(tmp_path / "일", home=tmp_path).read("S1")

        # nanos 를 버리면 세 step 이 모두 초 경계로 붙어 구간이 최대 1초씩
        # 어긋난다. 실측에서 비중이 101퍼센트로 나온 원인이다(sca-be2).
        assert [event.ts for event in events] == [
            1789754523.140929, 1789754523.203539, 1789754525.885261,
        ]

    def test_실제_payload_에서_도구_이름을_읽는다(self, tmp_path: Path) -> None:
        write_db(tmp_path, "S1", [(
            0, TOOL_STEP,
            self._raw("step2_type132.metadata.bin"),
            self._raw("step2_type132.payload.bin"),
        )])
        events = GeminiTranscriptReader(tmp_path / "일", home=tmp_path).read("S1")

        assert events[0].brief == "find_by_name"
