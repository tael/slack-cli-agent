"""백그라운드 감시 결과 판정이 marker 값을 정확히 구분하는지 고정한다.

지금은 모델이 [[WATCH_DONE]] 태그를 붙였는지만 보므로 성공과 실패를 못 가른다.
결과 파일 끝의 __SCA_WATCH_EXIT__=<정수> marker 로 실제 종료 상태를 읽는다.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from slack_cli_agent.reliability.watchresult import (
    WatchOutcome,
    WatchResultReader,
    background_command,
)


@pytest.fixture
def 결과자리(tmp_path):
    return tmp_path / ".watch-out"


@pytest.fixture
def 리더(결과자리):
    return WatchResultReader(결과자리)


class Test경로계산:
    def test_run_id로_경로를_만든다(self, 결과자리, 리더) -> None:
        assert 리더.path_for("9f3a2b") == 결과자리 / "9f3a2b.out"

    def test_run_id가_비면_None이다(self, 결과자리, 리더) -> None:
        assert 리더.path_for("") is None


    def test_run_id에_경로구분자가_있으면_None이다(self, 결과자리, 리더) -> None:
        assert 리더.path_for("../etc/passwd") is None
        assert 리더.path_for("a/b") is None

    def test_run_id에_상위경로_표시만_있어도_None이다(self, 결과자리, 리더) -> None:
        assert 리더.path_for("..") is None


class Test판정:
    def test_run_id가_비면_UNKNOWN이다(self, 결과자리, 리더) -> None:
        assert 리더.read("") is WatchOutcome.UNKNOWN


    def test_run_id가_경로탈출을_시도하면_UNKNOWN이다(self, 결과자리, 리더) -> None:
        assert 리더.read("../secret") is WatchOutcome.UNKNOWN

    def test_파일이_없으면_NOT_LAUNCHED다(self, 결과자리, 리더) -> None:
        assert 리더.read("9f3a2b") is WatchOutcome.NOT_LAUNCHED

    def test_marker가_없으면_RUNNING이다(self, 결과자리, 리더) -> None:
        경로 = 리더.path_for("9f3a2b")
        경로.parent.mkdir(parents=True)
        경로.write_text("아직 실행 중\n")
        assert 리더.read("9f3a2b") is WatchOutcome.RUNNING

    def test_marker값이_0이면_SUCCEEDED이다(self, 결과자리, 리더) -> None:
        경로 = 리더.path_for("9f3a2b")
        경로.parent.mkdir(parents=True)
        경로.write_text("배포 완료\n__SCA_WATCH_EXIT__=0\n")
        assert 리더.read("9f3a2b") is WatchOutcome.SUCCEEDED

    def test_marker값이_0이아니면_FAILED이다(self, 결과자리, 리더) -> None:
        경로 = 리더.path_for("9f3a2b")
        경로.parent.mkdir(parents=True)
        경로.write_text("에러 발생\n__SCA_WATCH_EXIT__=1\n")
        assert 리더.read("9f3a2b") is WatchOutcome.FAILED

    def test_음수도_FAILED로_본다(self, 결과자리, 리더) -> None:
        경로 = 리더.path_for("9f3a2b")
        경로.parent.mkdir(parents=True)
        경로.write_text("__SCA_WATCH_EXIT__=-1\n")
        assert 리더.read("9f3a2b") is WatchOutcome.FAILED

    def test_marker가_여러번_나오면_마지막_값을_쓴다(self, 결과자리, 리더) -> None:
        """명령 출력 안에 같은 문자열이 우연히 섞여도 실제 종료 상태는 마지막 것이다."""
        경로 = 리더.path_for("9f3a2b")
        경로.parent.mkdir(parents=True)
        경로.write_text(
            "echo 로 찍힌 옛 marker: __SCA_WATCH_EXIT__=1\n계속 실행\n__SCA_WATCH_EXIT__=0\n"
        )
        assert 리더.read("9f3a2b") is WatchOutcome.SUCCEEDED

    def test_marker값이_정수가_아니면_그_줄을_무시한다(self, 결과자리, 리더) -> None:
        경로 = 리더.path_for("9f3a2b")
        경로.parent.mkdir(parents=True)
        경로.write_text("__SCA_WATCH_EXIT__=abc\n")
        assert 리더.read("9f3a2b") is WatchOutcome.RUNNING

    def test_무효marker_뒤에_유효marker가_있으면_그것을_쓴다(self, 결과자리, 리더) -> None:
        경로 = 리더.path_for("9f3a2b")
        경로.parent.mkdir(parents=True)
        경로.write_text("__SCA_WATCH_EXIT__=abc\n__SCA_WATCH_EXIT__=2\n")
        assert 리더.read("9f3a2b") is WatchOutcome.FAILED

    def test_인코딩오류_바이트가_섞여도_죽지_않는다(self, 결과자리, 리더) -> None:
        경로 = 리더.path_for("9f3a2b")
        경로.parent.mkdir(parents=True)
        경로.write_bytes(b"\xff\xfe\x00\xff\xff\n__SCA_WATCH_EXIT__=0\n")
        assert 리더.read("9f3a2b") is WatchOutcome.SUCCEEDED

    def test_읽기권한이_없으면_UNKNOWN이고_경고를_남긴다(
        self, 결과자리, 리더, caplog
    ) -> None:
        경로 = 리더.path_for("9f3a2b")
        경로.parent.mkdir(parents=True)
        경로.write_text("__SCA_WATCH_EXIT__=0\n")
        경로.chmod(0o000)
        try:
            with caplog.at_level(logging.WARNING):
                assert 리더.read("9f3a2b") is WatchOutcome.UNKNOWN
            assert any(
                record.levelno == logging.WARNING for record in caplog.records
            )
        finally:
            경로.chmod(0o644)


class Test백그라운드명령생성:
    def test_기본_형태를_만든다(self) -> None:
        결과 = background_command("echo hi", "9f3a2b", ".watch-out")
        assert 결과.startswith('mkdir -p ".watch-out"\n')
        assert 'cat > ".watch-out/9f3a2b.sh" <<\'SCA_CMD_EOF\'\necho hi\nSCA_CMD_EOF' in 결과
        assert 'nohup sh -c \'bash -o pipefail ".watch-out/9f3a2b.sh";' in 결과
        assert '> ".watch-out/9f3a2b.out" 2>&1 &' in 결과
        assert "__SCA_WATCH_EXIT__=%s" in 결과

    def test_결과디렉터리는_받은_값을_쓴다(self) -> None:
        """기본값을 두면 안내문의 값과 어긋나도 아무도 모른다 (sca-vokt)."""
        결과 = background_command("echo hi", "9f3a2b", "/tmp/sca-out")
        assert 'mkdir -p "/tmp/sca-out"' in 결과
        assert '> "/tmp/sca-out/9f3a2b.out" 2>&1 &' in 결과

    def test_명령안의_작은따옴표가_안전하게_처리된다(self, tmp_path) -> None:
        """명령을 `sh -c` 안에 직접 넣으면 그 인용이 작은따옴표로 열려 있어
        구문이 깨진다. 실제로 sh 에 넘겨 결과 파일에 marker 가 남는지로
        검증한다."""
        import subprocess

        명령 = "echo 'it'\\''s broken'"
        결과 = background_command(명령, "9f3a2b", str(tmp_path / ".watch-out")).rstrip(" &\n")
        subprocess.run(결과, shell=True, cwd=tmp_path, check=True)
        내용 = (tmp_path / ".watch-out" / "9f3a2b.out").read_text()
        assert "it's broken" in 내용
        assert "__SCA_WATCH_EXIT__=0" in 내용

    def test_exit해도_marker가_남는다(self) -> None:
        """명령이 스크립트 파일 안에서 exit 해도 바깥 wrapper 가 상태를 찍는다."""
        결과 = background_command("exit 3", "9f3a2b", ".watch-out")
        assert 'status=$?; printf "\\n__SCA_WATCH_EXIT__=%s\\n" "$status"; exit "$status"' in 결과

    def test_heredoc_종료표시가_명령에_있으면_거부한다(self) -> None:
        """그 줄이 명령 안에 있으면 heredoc 이 거기서 끝나 나머지가 셸로 샌다."""
        import pytest

        with pytest.raises(ValueError):
            background_command("echo a\nSCA_CMD_EOF\nrm -rf /", "9f3a2b", ".watch-out")


class Test이름검증이_명령에도_걸린다:
    """읽을 때만 거르면 쓰는 쪽이 그대로 통과한다. 결과 디렉터리 밖으로
    리다이렉트하는 명령이 만들어진다."""

    def test_경로를_품은_이름은_거부한다(self) -> None:
        import pytest

        from slack_cli_agent.reliability.watchresult import background_command

        for 나쁜이름 in ("../evil", "a/b", "", ".", ".."):
            with pytest.raises(ValueError):
                background_command("ls", 나쁜이름, ".watch-out")

    def test_정상_이름은_명령을_만든다(self) -> None:
        from slack_cli_agent.reliability.watchresult import background_command

        명령 = background_command("ls", "9f3a2b", ".watch-out")
        assert ".watch-out/9f3a2b.out" in 명령


class Test표식은_줄_단독일_때만_센다:
    """명령 출력 안에 그 문자열이 섞여 나올 수 있다. 실제 표식은 자기 줄에
    단독으로 찍힌다."""

    def test_앞에_다른_글자가_붙은_줄은_무시한다(self, tmp_path) -> None:
        from slack_cli_agent.reliability.watchresult import WatchOutcome, WatchResultReader

        자리 = tmp_path / ".watch-out"
        자리.mkdir()
        (자리 / "r1.out").write_text(
            "작업 중\necho __SCA_WATCH_EXIT__=0\n", encoding="utf-8"
        )

        assert WatchResultReader(tmp_path / ".watch-out").read("r1") is WatchOutcome.RUNNING


class Test표식_문자열이_프롬프트와_같다:
    """두 자리에 따로 박힌 문자열은 한쪽만 고치면 조용히 어긋난다. 어긋나면
    모델이 찍는 표식을 코드가 못 읽어 모든 감시가 진행 중으로 남는다."""

    def test_프롬프트_안내에_같은_표식이_있다(self) -> None:
        from pathlib import Path

        from slack_cli_agent.reliability.watchresult import MARKER_PREFIX

        본문 = (
            Path(__file__).resolve().parents[2]
            / "src/slack_cli_agent/assets/prompts/watch_background_note.md"
        ).read_text(encoding="utf-8")
        assert MARKER_PREFIX in 본문


class Test띄웠는지_판정:
    """감시 등록은 모델이 태그를 붙이는지에 달려 있었다. 실측 2026-09-17 에
    백그라운드는 띄우고 태그를 안 내서, 작업이 끝나고 종료 상태까지 남았는데
    스레드에 아무 보고도 안 갔다. 파일 존재는 사실이고 태그는 협조다 (sca-pq5).

    증거로 쓰는 것은 결과 파일 하나다. 셸 리다이렉션이 nohup 실행 즉시 그
    파일을 만들기 때문에, 그것이 있다는 것은 작업이 떴다는 뜻이다. 스크립트
    파일은 그 앞 단계라, 그것만 있으면 nohup 이 실패했거나 모델이 중간에
    멈춘 것이다 (코덱스 검토).
    """

    def test_결과_파일이_있으면_띄운_것이다(self, 결과자리, 리더) -> None:
        자리 = 결과자리
        자리.mkdir()
        (자리 / "9f3a2b.out").write_text("", encoding="utf-8")
        assert 리더.launched("9f3a2b") is True

    def test_스크립트만_있으면_안_띄운_것이다(self, 결과자리, 리더) -> None:
        """등록하면 결과 파일이 영영 안 생겨 24시간 뒤 포기 알림만 나간다."""
        자리 = 결과자리
        자리.mkdir()
        (자리 / "9f3a2b.sh").write_text("sleep 50\n", encoding="utf-8")
        assert 리더.launched("9f3a2b") is False

    def test_아무것도_없으면_안_띄운_것이다(self, 결과자리, 리더) -> None:
        assert 리더.launched("9f3a2b") is False

    def test_다른_이름의_파일은_세지_않는다(self, 결과자리, 리더) -> None:
        """이름을 코드가 발급하는 이유다. 남의 결과를 내 것으로 읽으면 안 된다."""
        자리 = 결과자리
        자리.mkdir()
        (자리 / "다른이름.out").write_text("", encoding="utf-8")
        assert 리더.launched("9f3a2b") is False

    def test_디렉터리는_결과_파일이_아니다(self, 결과자리, 리더) -> None:
        """exists 는 디렉터리에도 참이다. 읽을 수 없는 것을 증거로 쓰지 않는다."""
        자리 = 결과자리 / "9f3a2b.out"
        자리.mkdir(parents=True)
        assert 리더.launched("9f3a2b") is False

    def test_쓸_수_없는_이름은_False다(self, 결과자리, 리더) -> None:
        assert 리더.launched("../evil") is False
        assert 리더.launched("") is False


class Test고아파일정리:
    """감시 태그가 안 나오거나 등록이 실패하면 이미 띄운 작업의 결과 파일과
    스크립트 파일이 .watch-out 에 그대로 남는다. 아무도 보지 않는다(sca-y6g).
    """

    def _파일(self, 결과자리: Path, 이름: str, 나이초: float, 지금: float) -> Path:
        자리 = 결과자리
        자리.mkdir(exist_ok=True)
        경로 = 자리 / 이름
        경로.write_text("내용", encoding="utf-8")
        import os

        os.utime(경로, (지금 - 나이초, 지금 - 나이초))
        return 경로

    def test_보관_기간이_지난_파일을_지운다(self, 결과자리, 리더) -> None:
        지금 = 1_700_000_000.0
        낡은것 = self._파일(결과자리, "낡음.out", 200_000, 지금)
        지운수 = 리더.cleanup(older_than_sec=172_800, now=지금)
        assert not 낡은것.exists()
        assert 지운수 == 1

    def test_기간_안의_파일은_남긴다(self, 결과자리, 리더) -> None:
        """감시 작업은 최대 24시간 살아 있다. 그 안의 파일을 지우면 아직 도는
        작업의 결과를 못 읽는다."""
        지금 = 1_700_000_000.0
        최근것 = self._파일(결과자리, "최근.out", 3_600, 지금)
        assert 리더.cleanup(older_than_sec=172_800, now=지금) == 0
        assert 최근것.exists()

    def test_스크립트_파일도_함께_지운다(self, 결과자리, 리더) -> None:
        지금 = 1_700_000_000.0
        스크립트 = self._파일(결과자리, "낡음.sh", 200_000, 지금)
        리더.cleanup(older_than_sec=172_800, now=지금)
        assert not 스크립트.exists()

    def test_디렉터리가_없어도_터지지_않는다(self, 결과자리, 리더) -> None:
        assert 리더.cleanup(older_than_sec=172_800, now=1_700_000_000.0) == 0



class Test한_번도_안_띄운_것과_도는_중을_가른다:
    """결과 파일은 백그라운드 명령을 nohup 으로 띄울 때만 생긴다. 모델이 감시
    태그만 붙여 등록한 조건 감시는 그 파일이 애초에 없다. 둘을 같은 RUNNING
    으로 보면 조건 감시가 24시간 내내 확인 없이 버려진다 (sca-vrs)."""

    def test_파일이_없으면_NOT_LAUNCHED다(self, 결과자리, 리더) -> None:
        assert 리더.read("9f3a2b") is WatchOutcome.NOT_LAUNCHED

    def test_파일은_있고_표식만_없으면_RUNNING이다(self, 결과자리, 리더) -> None:
        경로 = 리더.path_for("9f3a2b")
        경로.parent.mkdir(parents=True)
        경로.write_text("도는 중\n", encoding="utf-8")
        assert 리더.read("9f3a2b") is WatchOutcome.RUNNING


class Test안내문대로_띄우면_실패가_실패로_남는다:
    """실측 2026-09-19 18:14 — pytest 는 인자 오류로 죽고 ruff·mypy 는 모듈이
    없어 못 돌았는데 종료 상태가 0 으로 남았다. 각 검사를 tail 로 파이프해
    상태가 tail 것이 됐기 때문이다. 안내문이 내는 실행 줄 자체가 파이프의
    실패를 올려야 한다 (sca-lofs)."""

    NOTE = (
        Path(__file__).resolve().parents[2]
        / "src/slack_cli_agent/assets/prompts/watch_background_note.md"
    )

    def _실행줄(self, run_id: str, out_dir: Path) -> str:
        for 줄 in self.NOTE.read_text(encoding="utf-8").splitlines():
            if 줄.strip().startswith("nohup "):
                return (
                    줄.strip()
                    .replace("<<WATCH_RUN_ID>>", run_id)
                    .replace("<<WATCH_OUT_DIR>>", str(out_dir))
                )
        raise AssertionError("안내문에 실행 줄이 없다")

    def _돌린다(self, tmp_path: Path, 스크립트: str) -> WatchOutcome:
        import subprocess
        import time

        자리 = tmp_path / ".watch-out"
        자리.mkdir()
        (자리 / "r1.sh").write_text(스크립트, encoding="utf-8")
        subprocess.run(self._실행줄("r1", 자리), shell=True, cwd=tmp_path, check=True)
        리더 = WatchResultReader(자리)
        for _ in range(400):
            결과 = 리더.read("r1")
            if 결과 is not WatchOutcome.RUNNING:
                return 결과
            time.sleep(0.05)
        raise AssertionError(f"표식이 안 남았다 : {(자리 / 'r1.out').read_text()!r}")

    def test_파이프_앞이_실패하면_실패로_남는다(self, tmp_path: Path) -> None:
        결과 = self._돌린다(tmp_path, "python3 -c 'import sys; sys.exit(3)' | tail -1\n")
        assert 결과 is WatchOutcome.FAILED, (tmp_path / ".watch-out/r1.out").read_text()

    def test_전부_성공하면_성공으로_남는다(self, tmp_path: Path) -> None:
        결과 = self._돌린다(tmp_path, "echo 확인 | tail -1\n")
        assert 결과 is WatchOutcome.SUCCEEDED

    def test_검사를_여러_개_이어_붙이는_법이_안내에_있다(self) -> None:
        """파이프를 고쳐도 앞 검사의 실패는 마지막 검사가 성공하면 덮인다.
        안내문에 실패를 모으는 형태가 없으면 모델이 매번 다르게 쓴다."""
        본문 = self.NOTE.read_text(encoding="utf-8")
        assert "status=$((status" in 본문 or "|| status=1" in 본문


class Test결과는_봇_상태_디렉터리에_모인다:
    """결과 파일이 작업 디렉터리에 생겨 남의 저장소를 오염시켰다 (sca-vokt).

    자리를 절대경로 하나로 정하면 workdir 와 무관해진다. run_id 는 요청마다
    새로 뽑으므로 workdir 가 달라도 한 디렉터리에서 안 섞인다.
    """

    def test_경로가_workdir_와_무관하다(self, tmp_path: Path) -> None:
        리더 = WatchResultReader(tmp_path / "watch-out")
        assert 리더.path_for("9f3a2b") == tmp_path / "watch-out" / "9f3a2b.out"

    def test_run_id가_경로_조각이면_거부한다(self, tmp_path: Path) -> None:
        리더 = WatchResultReader(tmp_path / "watch-out")
        assert 리더.path_for("../밖") is None

    def test_정리도_한_디렉터리만_본다(self, tmp_path: Path) -> None:
        자리 = tmp_path / "watch-out"
        자리.mkdir()
        (자리 / "old.out").write_text("x", encoding="utf-8")
        import os
        os.utime(자리 / "old.out", (0, 0))
        리더 = WatchResultReader(자리)
        assert 리더.cleanup(older_than_sec=60) == 1

    def test_기동_때_디렉터리를_만든다(self, tmp_path: Path) -> None:
        """지금은 모델의 mkdir -p 가 유일한 생성자다."""
        자리 = tmp_path / "watch-out"
        WatchResultReader(자리).ensure_dir()
        assert 자리.is_dir()


class Test안내문과_코드가_같은_자리를_가리킨다:
    """안내문은 모델이 쓰는 자리를, 리더는 읽는 자리를 정한다. 둘이 어긋나면
    감시가 조용히 등록되지 않는다 (sca-vokt, sca-pq5 와 같은 형태)."""

    NOTE = (
        Path(__file__).resolve().parents[2]
        / "src/slack_cli_agent/assets/prompts/watch_background_note.md"
    )

    def test_안내문에_디렉터리_슬롯이_있다(self) -> None:
        본문 = self.NOTE.read_text(encoding="utf-8")
        assert "<<WATCH_OUT_DIR>>" in 본문

    def test_안내문에_박힌_경로가_남아_있지_않다(self) -> None:
        assert ".watch-out" not in self.NOTE.read_text(encoding="utf-8")

    def test_치환한_결과가_리더가_읽는_경로와_같다(self, tmp_path: Path) -> None:
        리더 = WatchResultReader(tmp_path / "watch-out")
        본문 = (
            self.NOTE.read_text(encoding="utf-8")
            .replace("<<WATCH_OUT_DIR>>", str(리더.result_dir))
            .replace("<<WATCH_RUN_ID>>", "r1")
        )
        assert str(리더.path_for("r1")) in 본문

    def test_명령_생성기도_같은_자리를_쓴다(self, tmp_path: Path) -> None:
        리더 = WatchResultReader(tmp_path / "watch-out")
        명령 = background_command("echo hi", "r1", str(리더.result_dir))
        assert str(리더.path_for("r1")) in 명령


class Test경로에_공백이_있어도_같은_자리에_남는다:
    """상태 디렉터리는 홈 아래라 사용자 이름에 공백이 들어갈 수 있다. 안내문이
    따옴표 없이 치환되면 셸이 인자를 쪼개 엉뚱한 자리에 쓰고, 리더는 원래
    자리를 보므로 감시가 조용히 등록되지 않는다 (sca-vokt 리뷰 지적)."""

    NOTE = (
        Path(__file__).resolve().parents[2]
        / "src/slack_cli_agent/assets/prompts/watch_background_note.md"
    )

    def _실행줄(self, run_id: str, out_dir: Path) -> str:
        for 줄 in self.NOTE.read_text(encoding="utf-8").splitlines():
            if 줄.strip().startswith("nohup "):
                return (
                    줄.strip()
                    .replace("<<WATCH_RUN_ID>>", run_id)
                    .replace("<<WATCH_OUT_DIR>>", str(out_dir))
                )
        raise AssertionError("안내문에 실행 줄이 없다")

    def _기다린다(self, 리더: WatchResultReader) -> WatchOutcome:
        import time

        for _ in range(400):
            결과 = 리더.read("r1")
            if 결과 is not WatchOutcome.RUNNING:
                return 결과
            time.sleep(0.05)
        raise AssertionError("표식이 안 남았다")

    def test_안내문대로_띄우면_결과가_제자리에_남는다(self, tmp_path: Path) -> None:
        import subprocess

        자리 = tmp_path / "tael kim" / "watch-out"
        자리.mkdir(parents=True)
        (자리 / "r1.sh").write_text("echo 확인\n", encoding="utf-8")
        subprocess.run(self._실행줄("r1", 자리), shell=True, cwd=tmp_path, check=True)
        assert self._기다린다(WatchResultReader(자리)) is WatchOutcome.SUCCEEDED

    def test_명령_생성기도_공백을_견딘다(self, tmp_path: Path) -> None:
        import subprocess

        자리 = tmp_path / "tael kim" / "watch-out"
        명령 = background_command("echo 확인", "r1", str(자리))
        subprocess.run(명령, shell=True, cwd=tmp_path, check=True)
        assert self._기다린다(WatchResultReader(자리)) is WatchOutcome.SUCCEEDED


class Test생성기와_안내문이_같은_실행_형태를_쓴다:
    """둘 중 하나만 고치면 모델이 쓰는 형태와 코드가 내는 형태가 갈린다.
    실제로 sca-lofs 의 pipefail 은 안내문에만 들어갔다."""

    NOTE = (
        Path(__file__).resolve().parents[2]
        / "src/slack_cli_agent/assets/prompts/watch_background_note.md"
    )

    def test_생성기도_파이프_실패를_올린다(self) -> None:
        본문 = self.NOTE.read_text(encoding="utf-8")
        assert "pipefail" in 본문
        assert "pipefail" in background_command("echo hi", "r1", "/tmp/o")

    def test_파이프_앞_실패가_생성기_명령에서도_실패로_남는다(self, tmp_path: Path) -> None:
        import subprocess
        import time

        자리 = tmp_path / "watch-out"
        명령 = background_command("python3 -c 'import sys; sys.exit(3)' | tail -1", "r1", str(자리))
        subprocess.run(명령, shell=True, cwd=tmp_path, check=True)
        리더 = WatchResultReader(자리)
        for _ in range(400):
            결과 = 리더.read("r1")
            if 결과 is not WatchOutcome.RUNNING:
                break
            time.sleep(0.05)
        assert 결과 is WatchOutcome.FAILED


class Test숨김_파일은_낯선_것으로_세지_않는다:
    """macOS 가 만드는 .DS_Store 가 매 정리마다 경고를 내면 그 경고가 신호로
    안 읽힌다 (sca-vokt 리뷰 지적)."""

    def test_DS_Store_는_경고를_내지_않는다(self, tmp_path: Path, caplog: Any) -> None:
        자리 = tmp_path / "watch-out"
        자리.mkdir()
        (자리 / ".DS_Store").write_bytes(b"x")
        with caplog.at_level(logging.WARNING):
            WatchResultReader(자리).cleanup(older_than_sec=60)
        assert "결과 파일이 아닌 것" not in caplog.text

    def test_낯선_보통_파일은_그대로_경고한다(self, tmp_path: Path, caplog: Any) -> None:
        자리 = tmp_path / "watch-out"
        자리.mkdir()
        (자리 / "메모.txt").write_text("x", encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            WatchResultReader(자리).cleanup(older_than_sec=60)
        assert "결과 파일이 아닌 것" in caplog.text
