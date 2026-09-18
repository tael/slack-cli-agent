"""백그라운드 감시 결과 판정이 marker 값을 정확히 구분하는지 고정한다.

지금은 모델이 [[WATCH_DONE]] 태그를 붙였는지만 보므로 성공과 실패를 못 가른다.
결과 파일 끝의 __SCA_WATCH_EXIT__=<정수> marker 로 실제 종료 상태를 읽는다.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from slack_cli_agent.reliability.watchresult import (
    WatchOutcome,
    WatchResultReader,
    background_command,
)


@pytest.fixture
def 작업디렉터리(tmp_path):
    return str(tmp_path)


@pytest.fixture
def 리더():
    return WatchResultReader()


class Test경로계산:
    def test_run_id로_경로를_만든다(self, 작업디렉터리, 리더) -> None:
        경로 = 리더.path_for(작업디렉터리, "9f3a2b")
        assert str(경로) == f"{작업디렉터리}/.watch-out/9f3a2b.out"

    def test_결과디렉터리_이름을_바꿀_수_있다(self, 작업디렉터리) -> None:
        리더 = WatchResultReader(result_dir=".custom-out")
        경로 = 리더.path_for(작업디렉터리, "abc")
        assert str(경로) == f"{작업디렉터리}/.custom-out/abc.out"

    def test_run_id가_비면_None이다(self, 작업디렉터리, 리더) -> None:
        assert 리더.path_for(작업디렉터리, "") is None

    def test_workdir가_비면_None이다(self, 리더) -> None:
        assert 리더.path_for("", "9f3a2b") is None

    def test_run_id에_경로구분자가_있으면_None이다(self, 작업디렉터리, 리더) -> None:
        assert 리더.path_for(작업디렉터리, "../etc/passwd") is None
        assert 리더.path_for(작업디렉터리, "a/b") is None

    def test_run_id에_상위경로_표시만_있어도_None이다(self, 작업디렉터리, 리더) -> None:
        assert 리더.path_for(작업디렉터리, "..") is None


class Test판정:
    def test_run_id가_비면_UNKNOWN이다(self, 작업디렉터리, 리더) -> None:
        assert 리더.read(작업디렉터리, "") is WatchOutcome.UNKNOWN

    def test_workdir가_비면_UNKNOWN이다(self, 리더) -> None:
        assert 리더.read("", "9f3a2b") is WatchOutcome.UNKNOWN

    def test_run_id가_경로탈출을_시도하면_UNKNOWN이다(self, 작업디렉터리, 리더) -> None:
        assert 리더.read(작업디렉터리, "../secret") is WatchOutcome.UNKNOWN

    def test_파일이_없으면_NOT_LAUNCHED다(self, 작업디렉터리, 리더) -> None:
        assert 리더.read(작업디렉터리, "9f3a2b") is WatchOutcome.NOT_LAUNCHED

    def test_marker가_없으면_RUNNING이다(self, 작업디렉터리, 리더) -> None:
        경로 = 리더.path_for(작업디렉터리, "9f3a2b")
        경로.parent.mkdir(parents=True)
        경로.write_text("아직 실행 중\n")
        assert 리더.read(작업디렉터리, "9f3a2b") is WatchOutcome.RUNNING

    def test_marker값이_0이면_SUCCEEDED이다(self, 작업디렉터리, 리더) -> None:
        경로 = 리더.path_for(작업디렉터리, "9f3a2b")
        경로.parent.mkdir(parents=True)
        경로.write_text("배포 완료\n__SCA_WATCH_EXIT__=0\n")
        assert 리더.read(작업디렉터리, "9f3a2b") is WatchOutcome.SUCCEEDED

    def test_marker값이_0이아니면_FAILED이다(self, 작업디렉터리, 리더) -> None:
        경로 = 리더.path_for(작업디렉터리, "9f3a2b")
        경로.parent.mkdir(parents=True)
        경로.write_text("에러 발생\n__SCA_WATCH_EXIT__=1\n")
        assert 리더.read(작업디렉터리, "9f3a2b") is WatchOutcome.FAILED

    def test_음수도_FAILED로_본다(self, 작업디렉터리, 리더) -> None:
        경로 = 리더.path_for(작업디렉터리, "9f3a2b")
        경로.parent.mkdir(parents=True)
        경로.write_text("__SCA_WATCH_EXIT__=-1\n")
        assert 리더.read(작업디렉터리, "9f3a2b") is WatchOutcome.FAILED

    def test_marker가_여러번_나오면_마지막_값을_쓴다(self, 작업디렉터리, 리더) -> None:
        """명령 출력 안에 같은 문자열이 우연히 섞여도 실제 종료 상태는 마지막 것이다."""
        경로 = 리더.path_for(작업디렉터리, "9f3a2b")
        경로.parent.mkdir(parents=True)
        경로.write_text(
            "echo 로 찍힌 옛 marker: __SCA_WATCH_EXIT__=1\n계속 실행\n__SCA_WATCH_EXIT__=0\n"
        )
        assert 리더.read(작업디렉터리, "9f3a2b") is WatchOutcome.SUCCEEDED

    def test_marker값이_정수가_아니면_그_줄을_무시한다(self, 작업디렉터리, 리더) -> None:
        경로 = 리더.path_for(작업디렉터리, "9f3a2b")
        경로.parent.mkdir(parents=True)
        경로.write_text("__SCA_WATCH_EXIT__=abc\n")
        assert 리더.read(작업디렉터리, "9f3a2b") is WatchOutcome.RUNNING

    def test_무효marker_뒤에_유효marker가_있으면_그것을_쓴다(self, 작업디렉터리, 리더) -> None:
        경로 = 리더.path_for(작업디렉터리, "9f3a2b")
        경로.parent.mkdir(parents=True)
        경로.write_text("__SCA_WATCH_EXIT__=abc\n__SCA_WATCH_EXIT__=2\n")
        assert 리더.read(작업디렉터리, "9f3a2b") is WatchOutcome.FAILED

    def test_인코딩오류_바이트가_섞여도_죽지_않는다(self, 작업디렉터리, 리더) -> None:
        경로 = 리더.path_for(작업디렉터리, "9f3a2b")
        경로.parent.mkdir(parents=True)
        경로.write_bytes(b"\xff\xfe\x00\xff\xff\n__SCA_WATCH_EXIT__=0\n")
        assert 리더.read(작업디렉터리, "9f3a2b") is WatchOutcome.SUCCEEDED

    def test_읽기권한이_없으면_UNKNOWN이고_경고를_남긴다(
        self, 작업디렉터리, 리더, caplog
    ) -> None:
        경로 = 리더.path_for(작업디렉터리, "9f3a2b")
        경로.parent.mkdir(parents=True)
        경로.write_text("__SCA_WATCH_EXIT__=0\n")
        경로.chmod(0o000)
        try:
            with caplog.at_level(logging.WARNING):
                assert 리더.read(작업디렉터리, "9f3a2b") is WatchOutcome.UNKNOWN
            assert any(
                record.levelno == logging.WARNING for record in caplog.records
            )
        finally:
            경로.chmod(0o644)


class Test백그라운드명령생성:
    def test_기본_형태를_만든다(self) -> None:
        결과 = background_command("echo hi", "9f3a2b")
        assert 결과.startswith("mkdir -p .watch-out\n")
        assert "cat > .watch-out/9f3a2b.sh <<'SCA_CMD_EOF'\necho hi\nSCA_CMD_EOF" in 결과
        assert "nohup sh -c 'sh .watch-out/9f3a2b.sh;" in 결과
        assert "> .watch-out/9f3a2b.out 2>&1 &" in 결과
        assert "__SCA_WATCH_EXIT__=%s" in 결과

    def test_결과디렉터리를_바꿀_수_있다(self) -> None:
        결과 = background_command("echo hi", "9f3a2b", out_dir=".custom-out")
        assert "mkdir -p .custom-out" in 결과
        assert "> .custom-out/9f3a2b.out 2>&1 &" in 결과

    def test_명령안의_작은따옴표가_안전하게_처리된다(self, tmp_path) -> None:
        """명령을 `sh -c` 안에 직접 넣으면 그 인용이 작은따옴표로 열려 있어
        구문이 깨진다. 실제로 sh 에 넘겨 결과 파일에 marker 가 남는지로
        검증한다."""
        import subprocess

        명령 = "echo 'it'\\''s broken'"
        결과 = background_command(명령, "9f3a2b").rstrip(" &\n")
        subprocess.run(결과, shell=True, cwd=tmp_path, check=True)
        내용 = (tmp_path / ".watch-out" / "9f3a2b.out").read_text()
        assert "it's broken" in 내용
        assert "__SCA_WATCH_EXIT__=0" in 내용

    def test_exit해도_marker가_남는다(self) -> None:
        """명령이 스크립트 파일 안에서 exit 해도 바깥 wrapper 가 상태를 찍는다."""
        결과 = background_command("exit 3", "9f3a2b")
        assert 'status=$?; printf "\\n__SCA_WATCH_EXIT__=%s\\n" "$status"; exit "$status"' in 결과

    def test_heredoc_종료표시가_명령에_있으면_거부한다(self) -> None:
        """그 줄이 명령 안에 있으면 heredoc 이 거기서 끝나 나머지가 셸로 샌다."""
        import pytest

        with pytest.raises(ValueError):
            background_command("echo a\nSCA_CMD_EOF\nrm -rf /", "9f3a2b")


class Test이름검증이_명령에도_걸린다:
    """읽을 때만 거르면 쓰는 쪽이 그대로 통과한다. 결과 디렉터리 밖으로
    리다이렉트하는 명령이 만들어진다."""

    def test_경로를_품은_이름은_거부한다(self) -> None:
        import pytest

        from slack_cli_agent.reliability.watchresult import background_command

        for 나쁜이름 in ("../evil", "a/b", "", ".", ".."):
            with pytest.raises(ValueError):
                background_command("ls", 나쁜이름)

    def test_정상_이름은_명령을_만든다(self) -> None:
        from slack_cli_agent.reliability.watchresult import background_command

        명령 = background_command("ls", "9f3a2b")
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

        assert WatchResultReader().read(str(tmp_path), "r1") is WatchOutcome.RUNNING


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

    def test_결과_파일이_있으면_띄운_것이다(self, 작업디렉터리, 리더) -> None:
        자리 = Path(작업디렉터리) / ".watch-out"
        자리.mkdir()
        (자리 / "9f3a2b.out").write_text("", encoding="utf-8")
        assert 리더.launched(작업디렉터리, "9f3a2b") is True

    def test_스크립트만_있으면_안_띄운_것이다(self, 작업디렉터리, 리더) -> None:
        """등록하면 결과 파일이 영영 안 생겨 24시간 뒤 포기 알림만 나간다."""
        자리 = Path(작업디렉터리) / ".watch-out"
        자리.mkdir()
        (자리 / "9f3a2b.sh").write_text("sleep 50\n", encoding="utf-8")
        assert 리더.launched(작업디렉터리, "9f3a2b") is False

    def test_아무것도_없으면_안_띄운_것이다(self, 작업디렉터리, 리더) -> None:
        assert 리더.launched(작업디렉터리, "9f3a2b") is False

    def test_다른_이름의_파일은_세지_않는다(self, 작업디렉터리, 리더) -> None:
        """이름을 코드가 발급하는 이유다. 남의 결과를 내 것으로 읽으면 안 된다."""
        자리 = Path(작업디렉터리) / ".watch-out"
        자리.mkdir()
        (자리 / "다른이름.out").write_text("", encoding="utf-8")
        assert 리더.launched(작업디렉터리, "9f3a2b") is False

    def test_디렉터리는_결과_파일이_아니다(self, 작업디렉터리, 리더) -> None:
        """exists 는 디렉터리에도 참이다. 읽을 수 없는 것을 증거로 쓰지 않는다."""
        자리 = Path(작업디렉터리) / ".watch-out" / "9f3a2b.out"
        자리.mkdir(parents=True)
        assert 리더.launched(작업디렉터리, "9f3a2b") is False

    def test_쓸_수_없는_이름은_False다(self, 작업디렉터리, 리더) -> None:
        assert 리더.launched(작업디렉터리, "../evil") is False
        assert 리더.launched(작업디렉터리, "") is False
        assert 리더.launched("", "9f3a2b") is False


class Test고아파일정리:
    """감시 태그가 안 나오거나 등록이 실패하면 이미 띄운 작업의 결과 파일과
    스크립트 파일이 .watch-out 에 그대로 남는다. 아무도 보지 않는다(sca-y6g).
    """

    def _파일(self, 작업디렉터리: str, 이름: str, 나이초: float, 지금: float) -> Path:
        자리 = Path(작업디렉터리) / ".watch-out"
        자리.mkdir(exist_ok=True)
        경로 = 자리 / 이름
        경로.write_text("내용", encoding="utf-8")
        import os

        os.utime(경로, (지금 - 나이초, 지금 - 나이초))
        return 경로

    def test_보관_기간이_지난_파일을_지운다(self, 작업디렉터리, 리더) -> None:
        지금 = 1_700_000_000.0
        낡은것 = self._파일(작업디렉터리, "낡음.out", 200_000, 지금)
        지운수 = 리더.cleanup(작업디렉터리, older_than_sec=172_800, now=지금)
        assert not 낡은것.exists()
        assert 지운수 == 1

    def test_기간_안의_파일은_남긴다(self, 작업디렉터리, 리더) -> None:
        """감시 작업은 최대 24시간 살아 있다. 그 안의 파일을 지우면 아직 도는
        작업의 결과를 못 읽는다."""
        지금 = 1_700_000_000.0
        최근것 = self._파일(작업디렉터리, "최근.out", 3_600, 지금)
        assert 리더.cleanup(작업디렉터리, older_than_sec=172_800, now=지금) == 0
        assert 최근것.exists()

    def test_스크립트_파일도_함께_지운다(self, 작업디렉터리, 리더) -> None:
        지금 = 1_700_000_000.0
        스크립트 = self._파일(작업디렉터리, "낡음.sh", 200_000, 지금)
        리더.cleanup(작업디렉터리, older_than_sec=172_800, now=지금)
        assert not 스크립트.exists()

    def test_디렉터리가_없어도_터지지_않는다(self, 작업디렉터리, 리더) -> None:
        assert 리더.cleanup(작업디렉터리, older_than_sec=172_800, now=1_700_000_000.0) == 0

    def test_작업디렉터리가_비면_아무것도_안_한다(self, 리더) -> None:
        assert 리더.cleanup("", older_than_sec=172_800, now=1_700_000_000.0) == 0


class Test한_번도_안_띄운_것과_도는_중을_가른다:
    """결과 파일은 백그라운드 명령을 nohup 으로 띄울 때만 생긴다. 모델이 감시
    태그만 붙여 등록한 조건 감시는 그 파일이 애초에 없다. 둘을 같은 RUNNING
    으로 보면 조건 감시가 24시간 내내 확인 없이 버려진다 (sca-vrs)."""

    def test_파일이_없으면_NOT_LAUNCHED다(self, 작업디렉터리, 리더) -> None:
        assert 리더.read(작업디렉터리, "9f3a2b") is WatchOutcome.NOT_LAUNCHED

    def test_파일은_있고_표식만_없으면_RUNNING이다(self, 작업디렉터리, 리더) -> None:
        경로 = 리더.path_for(작업디렉터리, "9f3a2b")
        경로.parent.mkdir(parents=True)
        경로.write_text("도는 중\n", encoding="utf-8")
        assert 리더.read(작업디렉터리, "9f3a2b") is WatchOutcome.RUNNING
