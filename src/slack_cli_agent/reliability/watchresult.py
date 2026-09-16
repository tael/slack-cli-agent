"""백그라운드 감시 결과 판정: marker 기반으로 성공/실패를 구분한다.

지금까지는 모델이 [[WATCH_DONE]] 태그를 붙였는지만 보고 완료를 판정했다.
그래서 성공과 실패를 못 가르고, 모델이 태그를 잘못 붙이면 그대로 완료 처리
됐다. 백그라운드 명령이 결과 파일 끝에 남기는 종료 상태 marker를 직접 읽어
그 자리를 대체한다.
"""

from __future__ import annotations

import logging
import re
import time
from enum import Enum, auto
from pathlib import Path, PurePosixPath

_LOGGER = logging.getLogger(__name__)

MARKER_PREFIX = "__SCA_WATCH_EXIT__="
"""Also written into the prompt guidance; test_watchresult pins the two together."""

# Anchored at both ends: the command's own output can carry this string, and
# the real marker is printed alone on its line.
_HEREDOC_TAG = "SCA_CMD_EOF"

_MARKER_RE = re.compile(r"^\s*" + re.escape(MARKER_PREFIX) + r"(-?\d+)\s*$")


class WatchOutcome(Enum):
    RUNNING = auto()
    SUCCEEDED = auto()
    FAILED = auto()
    UNKNOWN = auto()


def _is_safe_run_id(run_id: str) -> bool:
    if run_id in ("", ".", ".."):
        return False
    # PurePosixPath 로 구분자·상위경로 조각을 걸러낸다. run_id 는 단일
    # 파일명 컴포넌트여야 한다.
    parts = PurePosixPath(run_id).parts
    return len(parts) == 1 and parts[0] == run_id


class WatchResultReader:
    def __init__(self, result_dir: str = ".watch-out") -> None:
        self._result_dir = result_dir

    def path_for(self, workdir: str, run_id: str) -> Path | None:
        if not workdir or not _is_safe_run_id(run_id):
            return None
        return Path(workdir) / self._result_dir / f"{run_id}.out"

    def launched(self, workdir: str, run_id: str) -> bool:
        """Whether the background work for this run_id was actually started.

        The result file is the evidence: the shell redirection creates it the
        moment nohup runs, so its presence means the work started. The script
        file comes one step earlier — having only that means nohup failed or
        the engine stopped halfway, and registering then leaves a watch nobody
        can ever finish (codex review).

        Registration used to depend on the engine emitting a watch tag, so work
        that ran without one finished with nobody reading its exit status
        (sca-pq5).
        """
        path = self.path_for(workdir, run_id)
        if path is None:
            return False
        return path.is_file()

    def read(self, workdir: str, run_id: str) -> WatchOutcome:
        path = self.path_for(workdir, run_id)
        if path is None:
            return WatchOutcome.UNKNOWN

        if not path.exists():
            return WatchOutcome.RUNNING

        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            _LOGGER.warning("감시 결과 파일을 못 읽었다: %s (%s)", path, exc)
            return WatchOutcome.UNKNOWN

        # Last one wins: the command may run more than once into the same file.
        status: int | None = None
        for line in body.splitlines():
            found = _MARKER_RE.match(line)
            if found:
                status = int(found.group(1))

        if status is None:
            return WatchOutcome.RUNNING
        return WatchOutcome.SUCCEEDED if status == 0 else WatchOutcome.FAILED


    def cleanup(self, workdir: str, *, older_than_sec: float, now: float | None = None) -> int:
        """Deletes result and script files older than older_than_sec, returning
        how many went.

        A run_id is issued per request, so work that started without a watch tag
        or whose registration failed leaves both files behind with nobody
        reading them (sca-y6g). The cut must stay above watch_job_max_age_sec:
        a job still in the queue needs its result file.
        """
        if not workdir:
            return 0
        cut = (now if now is not None else time.time()) - older_than_sec
        removed = 0
        try:
            for path in (Path(workdir) / self._result_dir).glob("*"):
                if not path.is_file() or path.suffix not in (".out", ".sh"):
                    continue
                if path.stat().st_mtime >= cut:
                    continue
                path.unlink()
                removed += 1
        except OSError as exc:
            _LOGGER.warning("감시 결과 파일을 정리하지 못했다 : %s (%s)", workdir, exc)
        return removed


def background_command(command: str, run_id: str, out_dir: str = ".watch-out") -> str:
    """Raises on a name the reader would refuse: building a command that writes
    where nothing will look for it is worse than failing here.

    The command goes into a script file rather than into `sh -c`: a quote in
    the command would otherwise break the wrapper's quoting, and the marker
    would never be written (codex review).
    """
    if not _is_safe_run_id(run_id):
        raise ValueError(f"결과 파일 이름으로 쓸 수 없다 : {run_id!r}")
    if any(line.strip() == _HEREDOC_TAG for line in command.splitlines()):
        raise ValueError(f"명령에 heredoc 종료 표시가 들어 있다 : {_HEREDOC_TAG}")
    script = f"{out_dir}/{run_id}.sh"
    wrapper = (
        f"sh {script}; status=$?; "
        f'printf "\\n{MARKER_PREFIX}%s\\n" "$status"; exit "$status"'
    )
    return (
        f"mkdir -p {out_dir}\n"
        f"cat > {script} <<'{_HEREDOC_TAG}'\n{command}\n{_HEREDOC_TAG}\n"
        f"nohup sh -c '{wrapper}' > {out_dir}/{run_id}.out 2>&1 &\n"
    )
