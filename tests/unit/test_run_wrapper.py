"""봇을 띄우는 wrapper 스크립트.

launchd 가 부르는 것은 이 셸이고, 그 종료코드를 보고 재기동할지 정한다.
게이트가 설정 오류로 78(EX_CONFIG)을 내면 여기서 차단 표식을 남겨야
`KeepAlive` 의 `PathState` 조건이 재기동을 멈춘다 (sca-y4q).

셸이라도 시험한다. 이 로직이 각 봇의 상태 디렉터리에만 있으면 봇마다 갈리고
아무도 못 본다 - 실제로 세 봇의 run.sh 가 이미 서로 달랐다.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TEMPLATE = REPO / "tools" / "templates" / "run.sh"


def _봇자리(tmp_path: Path, *, 종료코드: int, 지연초: float = 0, term무시: bool = False) -> Path:
    """가짜 봇 하나. venv 자리에 종료코드를 정해 두는 CLI 대역을 둔다.

    `term무시` 는 SIGTERM 을 안 죽고 버티는 본체를 흉내낸다. 종료 중 정리를
    하느라 늦는 프로세스가 실제로 그렇게 보인다.
    """
    d = tmp_path / ".testbot"
    (d / "venv" / "bin").mkdir(parents=True)
    (d / "logs").mkdir()
    cli = d / "venv" / "bin" / "slack-cli-agent"
    무시 = "trap '' TERM INT HUP\n" if term무시 else ""
    cli.write_text(
        "#!/bin/bash\n"
        + 무시
        + f'echo "$@" > "{d}/불린인자.txt"\n'
        + (f"sleep {지연초}\n" if 지연초 else "")
        + f"exit {종료코드}\n",
        encoding="utf-8",
    )
    cli.chmod(0o755)
    return d


def _wrapper(tmp_path: Path, 봇자리: Path) -> Path:
    """템플릿을 이 가짜 봇에 맞춰 렌더링한다."""
    run = 봇자리 / "run.sh"
    run.write_text(
        TEMPLATE.read_text(encoding="utf-8")
        .replace("__NAME__", "testbot")
        .replace("__DISPLAY__", "시험봇")
        .replace("__REPO__", str(REPO)),
        encoding="utf-8",
    )
    run.chmod(0o755)
    return run


def _실행(
    run: Path, tmp_path: Path, *인자: str, 대기: float = 20
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, HOME=str(tmp_path))
    return subprocess.run(  # noqa: PLW1510 - 종료코드를 시험이 직접 본다
        ["/bin/bash", str(run), *인자], capture_output=True, text=True, env=env, timeout=대기
    )


def _표식(봇자리: Path, role: str) -> Path:
    return 봇자리 / "preflight-blocked" / role


class Test차단표식:
    @pytest.mark.parametrize("role", ["worker", "ingress"])
    def test_78_이면_역할별_표식을_남긴다(self, tmp_path: Path, role: str) -> None:
        봇 = _봇자리(tmp_path, 종료코드=78)
        결과 = _실행(_wrapper(tmp_path, 봇), tmp_path, role)
        assert 결과.returncode == 78, 결과.stderr
        assert _표식(봇, role).is_file()
        # 다른 역할까지 막으면 복구 범위가 쓸데없이 커진다 (코덱스 리뷰).
        다른역할 = "ingress" if role == "worker" else "worker"
        assert not _표식(봇, 다른역할).exists()

    def test_set_e_가_78_을_삼키지_않는다(self, tmp_path: Path) -> None:
        """`set -e` 아래에서 본체를 그냥 부르면 78 에서 셸이 즉시 끝나 표식을
        남길 자리가 사라진다. 순진한 구현이 여기서 걸린다 (코덱스 리뷰)."""
        봇 = _봇자리(tmp_path, 종료코드=78)
        결과 = _실행(_wrapper(tmp_path, 봇), tmp_path, "worker")
        assert _표식(봇, "worker").is_file()
        assert 결과.returncode == 78

    @pytest.mark.parametrize("코드", [0, 1, 2])
    def test_78_이_아니면_표식을_안_남긴다(self, tmp_path: Path, 코드: int) -> None:
        """일반 런타임 실패는 계속 재기동돼야 한다. 여기서 표식을 남기면
        프로세스 충돌 한 번에 봇이 영영 안 뜬다."""
        봇 = _봇자리(tmp_path, 종료코드=코드)
        결과 = _실행(_wrapper(tmp_path, 봇), tmp_path, "worker")
        assert 결과.returncode == 코드
        assert not (봇 / "preflight-blocked").exists() or not _표식(봇, "worker").exists()

    def test_사람이_돌린_preflight_의_78_은_표식이_아니다(self, tmp_path: Path) -> None:
        """`run.sh preflight` 는 사람이 상태를 보려고 부르는 것이다. 그 78 로
        표식을 만들면 점검했다는 이유로 봇이 안 뜬다 (코덱스 리뷰)."""
        봇 = _봇자리(tmp_path, 종료코드=78)
        결과 = _실행(_wrapper(tmp_path, 봇), tmp_path, "preflight")
        assert 결과.returncode == 78
        assert not _표식(봇, "preflight").exists()

    def test_표식에_원인을_찾을_단서가_있다(self, tmp_path: Path) -> None:
        """빈 파일이어도 차단은 되지만 사람이 원인을 못 읽는다."""
        봇 = _봇자리(tmp_path, 종료코드=78)
        _실행(_wrapper(tmp_path, 봇), tmp_path, "worker")
        본문 = _표식(봇, "worker").read_text(encoding="utf-8")
        assert "78" in 본문
        assert "worker" in 본문
        assert "worker.err.log" in 본문


class Test신호전달:
    def test_SIGTERM_을_자식에게_넘긴다(self, tmp_path: Path) -> None:
        """exec 를 뺐으므로 launchd 의 SIGTERM 이 이 셸에만 온다. 직접
        넘기지 않으면 본체가 살아남아 소켓을 쥔 채로 남는다."""
        봇 = _봇자리(tmp_path, 종료코드=0, 지연초=30)
        proc = subprocess.Popen(
            ["/bin/bash", str(_wrapper(tmp_path, 봇)), "worker"],
            env=dict(os.environ, HOME=str(tmp_path)),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        time.sleep(1.5)
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)

        # 대역이 30초를 다 잤으면 신호가 안 갔다는 뜻이다.
        남은 = subprocess.run(
            ["pgrep", "-f", str(봇 / "venv" / "bin" / "slack-cli-agent")],
            capture_output=True,
            text=True,
            check=False,
        )
        assert 남은.returncode != 0, f"본체가 살아 있다 : {남은.stdout}"

    def test_TERM_을_무시하는_본체도_유예_뒤에_끝난다(self, tmp_path: Path) -> None:
        """자식이 TERM 을 안 받으면 wrapper 는 wait 에서 영영 안 돌아온다.
        그러면 launchd 는 이 서비스를 죽은 것으로도 산 것으로도 못 본다
        (코덱스 리뷰 [높음], 실측으로 확인됨).
        """
        봇 = _봇자리(tmp_path, 종료코드=0, 지연초=60, term무시=True)
        proc = subprocess.Popen(
            ["/bin/bash", str(_wrapper(tmp_path, 봇)), "worker"],
            env=dict(os.environ, HOME=str(tmp_path), SHUTDOWN_GRACE_SEC="2"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        time.sleep(1.5)
        proc.send_signal(signal.SIGTERM)
        # 유예 2초 + 여유. 안 끝나면 여기서 TimeoutExpired 로 실패한다.
        proc.wait(timeout=15)

        남은 = subprocess.run(
            ["pgrep", "-f", str(봇 / "venv" / "bin" / "slack-cli-agent")],
            capture_output=True,
            text=True,
            check=False,
        )
        assert 남은.returncode != 0, f"본체가 살아 있다 : {남은.stdout}"


class Test인자전달:
    def test_역할과_프로필_인자를_본체에_넘긴다(self, tmp_path: Path) -> None:
        봇 = _봇자리(tmp_path, 종료코드=0)
        _실행(_wrapper(tmp_path, 봇), tmp_path, "worker")
        인자 = (봇 / "불린인자.txt").read_text(encoding="utf-8")
        assert "worker" in 인자
        assert "--profile testbot" in 인자
        assert "--profile-dir" in 인자


class Test생성스크립트연결:
    def test_new_bot_이_템플릿을_쓴다(self) -> None:
        """템플릿이 옳아도 new-bot.sh 가 run.sh 를 따로 쓰면 새 봇은 여전히
        exec 판으로 태어난다."""
        본문 = (REPO / "tools" / "new-bot.sh").read_text(encoding="utf-8")
        assert "templates/run.sh" in 본문
        assert "exec " not in 본문, "생성부가 다시 wrapper 를 직접 쓰고 있다"

    def test_이미_있는_봇을_갱신할_수단이_있다(self) -> None:
        """new-bot.sh 만 고치면 새 봇만 보호된다. 이미 도는 봇들은 옛 판을
        들고 있어 차단 표식이 그 봇들에는 없다 (코덱스 리뷰)."""
        동기화 = REPO / "tools" / "sync-run-sh.sh"
        assert 동기화.is_file()
        assert "templates/run.sh" in 동기화.read_text(encoding="utf-8")

    def test_복구_도구가_표식을_먼저_지운다(self) -> None:
        """kickstart 는 KeepAlive 조건을 무시하고 띄운다. 표식을 안 지우고
        띄우면 다시 78 로 끝나고 표식만 새로 생긴다 (코덱스 리뷰)."""
        본문 = (REPO / "tools" / "unblock.sh").read_text(encoding="utf-8")
        assert 본문.index("rm -f") < 본문.index("launchctl kickstart")


class Test기존봇동기화:
    """이미 도는 봇의 wrapper 를 템플릿으로 다시 까는 자리.

    세 봇의 자격 방식이 서로 다르다 - asuka·rei 는 `.slack_*_token` 파일을
    읽어 환경변수로 넘기고, shinji 는 본체가 credentials.json 을 직접 읽는다.
    덮어쓰면서 이것이 깨지면 그 봇은 토큰 없이 뜬다 (코덱스 리뷰).
    """

    동기화 = REPO / "tools" / "sync-run-sh.sh"

    def _봇(self, tmp_path: Path, name: str, *, 토큰파일: bool) -> Path:
        d = tmp_path / f".{name}"
        (d / "venv" / "bin").mkdir(parents=True)
        (d / "logs").mkdir()
        # 옛 판을 둔다. 덮어쓰기 대상이 있는 상태를 만든다.
        (d / "run.sh").write_text("#!/bin/bash\n# 옛 판\nexec true\n", encoding="utf-8")
        if 토큰파일:
            (d / ".slack_bot_token").write_text("xoxb-시험용\n", encoding="utf-8")
            (d / ".slack_app_token").write_text("xapp-1-시험용\n", encoding="utf-8")
        cli = d / "venv" / "bin" / "slack-cli-agent"
        # 넘겨받은 환경을 그대로 적어 두는 대역. 무엇이 전달됐는지 본다.
        cli.write_text(
            "#!/bin/bash\n"
            f'echo "BOT=${{SLACK_BOT_TOKEN:-없음}}" > "{d}/받은환경.txt"\n'
            f'echo "APP=${{SLACK_APP_TOKEN:-없음}}" >> "{d}/받은환경.txt"\n'
            "exit 0\n",
            encoding="utf-8",
        )
        cli.chmod(0o755)
        return d

    def _돌린다(self, tmp_path: Path, name: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/bash", str(self.동기화), name],
            capture_output=True,
            text=True,
            timeout=60,
            env=dict(os.environ, HOME=str(tmp_path)),
            check=False,
        )

    def test_토큰_파일을_쓰는_봇의_방식이_유지된다(self, tmp_path: Path) -> None:
        봇 = self._봇(tmp_path, "asuka", 토큰파일=True)
        assert self._돌린다(tmp_path, "asuka").returncode == 0
        _실행(봇 / "run.sh", tmp_path, "worker")
        받은 = (봇 / "받은환경.txt").read_text(encoding="utf-8")
        assert "BOT=xoxb-시험용" in 받은
        assert "APP=xapp-1-시험용" in 받은

    def test_자격_파일을_쓰는_봇에_빈_토큰을_안_넘긴다(self, tmp_path: Path) -> None:
        """토큰 파일이 없는 봇에 빈 문자열을 넘기면 본체가 그것을 토큰으로
        읽어 credentials.json 으로 안 간다."""
        봇 = self._봇(tmp_path, "shinji", 토큰파일=False)
        assert self._돌린다(tmp_path, "shinji").returncode == 0
        _실행(봇 / "run.sh", tmp_path, "worker")
        받은 = (봇 / "받은환경.txt").read_text(encoding="utf-8")
        assert "BOT=없음" in 받은
        assert "APP=없음" in 받은

    def test_credentials_파일이_있어도_환경변수로_안_바꿔_넘긴다(self, tmp_path: Path) -> None:
        """자격 파일을 쓰는 봇은 본체가 그 파일을 직접 읽는다. wrapper 가
        중간에서 토큰을 꺼내 넘기면 자격 방식이 둘로 갈린다 (코덱스 리뷰)."""
        봇 = self._봇(tmp_path, "shinji", 토큰파일=False)
        (봇 / "credentials.json").write_text(
            '{"bot_token": "xoxb-자격파일", "app_token": "xapp-1-자격파일"}', encoding="utf-8"
        )
        assert self._돌린다(tmp_path, "shinji").returncode == 0
        _실행(봇 / "run.sh", tmp_path, "worker")
        받은 = (봇 / "받은환경.txt").read_text(encoding="utf-8")
        assert "자격파일" not in 받은
        assert "BOT=없음" in 받은

    def test_덮기_전_판을_남긴다(self, tmp_path: Path) -> None:
        봇 = self._봇(tmp_path, "asuka", 토큰파일=True)
        self._돌린다(tmp_path, "asuka")
        assert "옛 판" in (봇 / "run.sh.bak").read_text(encoding="utf-8")

    def test_깐_것이_차단_표식_동작을_갖는다(self, tmp_path: Path) -> None:
        """동기화의 목적이 이것이다. 문자열이 들어갔는지가 아니라 실제로
        표식을 남기는지를 본다."""
        봇 = self._봇(tmp_path, "asuka", 토큰파일=True)
        self._돌린다(tmp_path, "asuka")
        cli = 봇 / "venv" / "bin" / "slack-cli-agent"
        cli.write_text("#!/bin/bash\nexit 78\n", encoding="utf-8")
        cli.chmod(0o755)

        결과 = _실행(봇 / "run.sh", tmp_path, "worker")
        assert 결과.returncode == 78
        assert (봇 / "preflight-blocked" / "worker").is_file()

    def test_없는_봇이면_실패한다(self, tmp_path: Path) -> None:
        """조용히 통과하면 갱신된 줄 알고 넘어간다."""
        결과 = self._돌린다(tmp_path, "없는봇")
        assert 결과.returncode != 0


class Test표식을_못_쓰는_경우:
    """표식 생성 자체가 실패하면 78 차단이 무력화된다 (코덱스 리뷰 [중간]).

    운영 한계로 받아들이되, 조용히 무력화되지는 않게 한다. 표식 없이
    재기동이 계속되는 상태와 정상 차단이 로그에서 구분돼야 한다.
    """

    @pytest.fixture(autouse=True)
    def _권한이_실제로_막는지(self) -> None:
        # root 는 쓰기 권한 비트를 무시한다. 그 환경에서는 이 시험이
        # 아무것도 안 보므로 통과로 남기지 않는다 (코덱스 리뷰).
        if os.geteuid() == 0:
            pytest.skip("root 에서는 권한 비트가 쓰기를 막지 않는다")

    def _못쓰게_만든다(self, 봇자리: Path) -> None:
        (봇자리 / "preflight-blocked").mkdir()
        (봇자리 / "preflight-blocked").chmod(0o500)

    def test_사유를_stderr_에_남긴다(self, tmp_path: Path) -> None:
        봇 = _봇자리(tmp_path, 종료코드=78)
        run = _wrapper(tmp_path, 봇)
        self._못쓰게_만든다(봇)
        try:
            결과 = _실행(run, tmp_path, "worker")
        finally:
            (봇 / "preflight-blocked").chmod(0o700)
        assert "차단 표식을 쓰지 못했다" in 결과.stderr
        assert "재기동이 계속된다" in 결과.stderr

    def test_그래도_78_로_끝난다(self, tmp_path: Path) -> None:
        """표식을 못 써도 종료코드는 바꾸지 않는다. 0 으로 끝내면 launchd
        쪽에서 정상 종료로 읽힌다."""
        봇 = _봇자리(tmp_path, 종료코드=78)
        run = _wrapper(tmp_path, 봇)
        self._못쓰게_만든다(봇)
        try:
            결과 = _실행(run, tmp_path, "worker")
        finally:
            (봇 / "preflight-blocked").chmod(0o700)
        assert 결과.returncode == 78

    def test_반쯤_쓰다_만_표식을_남기지_않는다(self, tmp_path: Path) -> None:
        봇 = _봇자리(tmp_path, 종료코드=78)
        run = _wrapper(tmp_path, 봇)
        self._못쓰게_만든다(봇)
        try:
            _실행(run, tmp_path, "worker")
        finally:
            (봇 / "preflight-blocked").chmod(0o700)
        assert list((봇 / "preflight-blocked").iterdir()) == []


class Test동기화_멱등성:
    """두 번째 동기화가 run.sh.bak 을 덮으면 손으로 고친 원본이 사라진다.

    대조하려고 남기는 것인데 대조 대상이 자기 자신이 된다 (코덱스 리뷰 [중간]).
    """

    def test_두_번_돌려도_원본_백업이_남는다(self, tmp_path: Path) -> None:
        봇 = Test기존봇동기화()._봇(tmp_path, "asuka", 토큰파일=True)
        Test기존봇동기화()._돌린다(tmp_path, "asuka")
        Test기존봇동기화()._돌린다(tmp_path, "asuka")
        assert "옛 판" in (봇 / "run.sh.bak").read_text(encoding="utf-8")

    def test_같으면_건드리지_않는다(self, tmp_path: Path) -> None:
        Test기존봇동기화()._봇(tmp_path, "asuka", 토큰파일=True)
        Test기존봇동기화()._돌린다(tmp_path, "asuka")
        결과 = Test기존봇동기화()._돌린다(tmp_path, "asuka")
        assert "이미 같다" in 결과.stdout


class Test표식_자리를_못_만드는_경우:
    """`mkdir -p` 실패 분기. 권한이 아니라 같은 이름의 일반 파일로 막는다 -
    root 나 ACL 우회 환경에서도 같게 동작한다 (코덱스 리뷰 [중간]).
    """

    def _막는다(self, 봇자리: Path) -> None:
        (봇자리 / "preflight-blocked").write_text("자리를 차지한 일반 파일\n", encoding="utf-8")

    def test_사유를_stderr_에_남긴다(self, tmp_path: Path) -> None:
        봇 = _봇자리(tmp_path, 종료코드=78)
        run = _wrapper(tmp_path, 봇)
        self._막는다(봇)
        결과 = _실행(run, tmp_path, "worker")
        assert "차단 표식을 만들 자리를 확보하지 못했다" in 결과.stderr
        assert 결과.returncode == 78

    def test_막은_파일을_건드리지_않는다(self, tmp_path: Path) -> None:
        봇 = _봇자리(tmp_path, 종료코드=78)
        run = _wrapper(tmp_path, 봇)
        self._막는다(봇)
        _실행(run, tmp_path, "worker")
        assert (봇 / "preflight-blocked").is_file()


class Test표식을_옮기다_실패하는_경우:
    """임시 파일에는 썼는데 mv 만 실패하는 경우. 지우지 않으면 재기동마다
    `.worker.<pid>` 가 쌓인다 (코덱스 리뷰 [중간]).
    """

    def test_임시_파일을_남기지_않는다(self, tmp_path: Path) -> None:
        봇 = _봇자리(tmp_path, 종료코드=78)
        run = _wrapper(tmp_path, 봇)
        # wrapper 가 PATH 맨 앞에 두는 자리에 늘 실패하는 mv 를 놓는다.
        # 디렉터리 권한으로는 이 분기를 못 만든다 - 쓰기가 되면 같은 자리의
        # rename 도 된다.
        가짜 = tmp_path / ".local" / "bin"
        가짜.mkdir(parents=True)
        (가짜 / "mv").write_text("#!/bin/bash\nexit 1\n", encoding="utf-8")
        (가짜 / "mv").chmod(0o755)

        결과 = _실행(run, tmp_path, "worker")

        assert 결과.returncode == 78
        assert "차단 표식을 쓰지 못했다" in 결과.stderr
        # 표식도 임시 파일도 없다. 표식이 없으니 재기동은 계속되고,
        # 그 사실은 위 stderr 로만 드러난다.
        assert list((봇 / "preflight-blocked").iterdir()) == []


class Test표식이_가리키는_로그:
    """게이트는 점검 결과를 stdout 에 낸다. launchd 는 stdout 과 stderr 를 다른
    파일로 가르므로, 표식이 err 로그만 가리키면 복구하는 사람이 빈 파일을 보고
    원인을 못 찾는다 (코덱스 리뷰).
    """

    def test_점검_결과가_있는_쪽을_가리킨다(self, tmp_path: Path) -> None:
        봇 = _봇자리(tmp_path, 종료코드=78)
        _실행(_wrapper(tmp_path, 봇), tmp_path, "worker")
        표식내용 = _표식(봇, "worker").read_text(encoding="utf-8")
        assert "worker.out.log" in 표식내용
