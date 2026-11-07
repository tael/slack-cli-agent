"""Codex 가 이번 턴에 만들고 올리지 않은 그림을 코드가 올리는가 (D2).

마메치 bot.py new_generated_images 와 generated_image_autopost 이관이다.
2026-10-25 같은 요청을 두 번 시험했는데 한 번은 모델이 image_post_from_file 로
올렸고, 한 번은 그림만 만들고 "방 이미지예요" 라고만 답했다.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from test_pipeline import build_pipeline, make_ctx, ok_response

from slack_cli_agent.engine.base import EngineResponse
from slack_cli_agent.observability.audit import IncidentKind
from slack_cli_agent.slack.generated_images import (
    NOTICE_IMAGE_UPLOAD_FAILED,
    SlackGeneratedImagePoster,
    new_generated_images,
)


def _image(home: Path, thread_id: str, name: str, mtime: float) -> Path:
    d = home / "generated_images" / thread_id
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_bytes(b"png")
    os.utime(f, (mtime, mtime))
    return f


def codex(body: str, thread_id: str = "th-1", tools: list[str] | None = None,
          engine: str = "codex") -> EngineResponse:
    base = ok_response(body, thread_id)
    return EngineResponse(**{**base.__dict__, "engine": engine, "raw": {"tools": tools or []}})


class FakeClient:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.uploads: list[dict[str, Any]] = []

    def files_upload_v2(self, **kwargs: Any) -> dict[str, Any]:
        if self.fail:
            raise RuntimeError("upload down")
        self.uploads.append(kwargs)
        return {"ok": True}


class FakeAudit:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def __call__(self, **kw: Any) -> None:
        self.records.append(kw)


class Test새그림고르기:
    def test_이번_턴_뒤에_생긴_그림만_만든_순서로(self, tmp_path: Path) -> None:
        _image(tmp_path, "th-1", "old.png", 100.0)
        b = _image(tmp_path, "th-1", "b.png", 300.0)
        a = _image(tmp_path, "th-1", "a.webp", 200.0)
        _image(tmp_path, "th-1", "note.txt", 250.0)
        assert new_generated_images(tmp_path, "th-1", 150.0) == [a, b]

    def test_폴더가_없으면_빈_목록(self, tmp_path: Path) -> None:
        assert new_generated_images(tmp_path, "th-x", 0.0) == []
        assert new_generated_images(tmp_path, "", 0.0) == []


def _poster(tmp_path: Path, client: FakeClient, audit: FakeAudit) -> SlackGeneratedImagePoster:
    return SlackGeneratedImagePoster(
        client, codex_home_for=lambda engine: tmp_path if engine == "codex" else None,
        audit=audit,
    )


class Test게시기:
    def test_codex_턴의_새_그림을_스레드에_올린다(self, tmp_path: Path) -> None:
        f = _image(tmp_path, "th-1", "exec-1.png", 200.0)
        client, audit = FakeClient(), FakeAudit()
        poster = _poster(tmp_path, client, audit)
        images = poster.pending(150.0, [codex("x")])
        assert images == [f]
        assert poster.upload("C1", "1.0", images) is True
        up = client.uploads[0]
        assert up["channel"] == "C1" and up["thread_ts"] == "1.0"
        assert up["file_uploads"][0]["file"] == str(f)
        assert "initial_comment" not in up
        assert audit.records[0]["kind"] == IncidentKind.GENERATED_IMAGE_AUTOPOST.value

    def test_올리는_도구를_불렀으면_건드리지_않는다(self, tmp_path: Path) -> None:
        _image(tmp_path, "th-1", "exec-1.png", 200.0)
        client = FakeClient()
        resp = codex("x", tools=["mermaid.image_post_from_file"])
        assert _poster(tmp_path, client, FakeAudit()).pending(150.0, [resp]) == []

    def test_실패한_게시_도구는_올린_것으로_보지_않는다(self, tmp_path: Path) -> None:
        _image(tmp_path, "th-1", "exec-1.png", 200.0)
        client = FakeClient()
        resp = EngineResponse(**{**codex("x").__dict__, "raw": {
            "tools": ["mermaid.image_post_from_file"],
            "tool_errors": ["mermaid.image_post_from_file : POST_CHANNEL 밖이다"],
        }})
        assert len(_poster(tmp_path, client, FakeAudit()).pending(150.0, [resp])) == 1

    def test_codex_가_아닌_엔진은_건드리지_않는다(self, tmp_path: Path) -> None:
        _image(tmp_path, "th-1", "exec-1.png", 200.0)
        client = FakeClient()
        resp = codex("x", engine="claude")
        assert _poster(tmp_path, client, FakeAudit()).pending(150.0, [resp]) == []

    def test_업로드가_실패하면_거짓과_감사(self, tmp_path: Path) -> None:
        f = _image(tmp_path, "th-1", "exec-1.png", 200.0)
        audit = FakeAudit()
        assert _poster(tmp_path, FakeClient(fail=True), audit).upload("C1", "1.0", [f]) is False
        assert audit.records[0]["kind"] == IncidentKind.GENERATED_IMAGE_AUTOPOST_FAILED.value


class Fake게시기:
    def __init__(self, images: list[Path], ok: bool = True) -> None:
        self.images = images
        self.ok = ok
        self.pending_calls: list[tuple] = []
        self.uploads: list[tuple] = []

    def pending(self, since, responses):
        self.pending_calls.append((since, list(responses)))
        return list(self.images)

    def upload(self, channel, thread_ts, images):
        self.uploads.append((channel, thread_ts, list(images)))
        return self.ok


class Test파이프라인:
    def test_올린_뒤_답은_평소_경로로_나간다(self) -> None:
        poster = Fake게시기([Path("/x/a.png")])
        pipeline, parts = build_pipeline(responses=[codex("방 이미지예요")], generated_images=poster)
        pipeline.handle(make_ctx(channel="C1", thread_ts="1700000001.000100"))
        assert poster.uploads == [("C1", "1700000001.000100", [Path("/x/a.png")])]
        assert parts["publisher"].posted[-1]["text"] == "방 이미지예요"

    def test_업로드에_실패하면_본문에_사실을_덧붙인다(self) -> None:
        pipeline, parts = build_pipeline(responses=[codex("방 이미지예요")],
                                         generated_images=Fake게시기([Path("/x/a.png")], ok=False))
        pipeline.handle(make_ctx())
        posted = parts["publisher"].posted[-1]["text"]
        assert posted.startswith("방 이미지예요")
        assert posted.endswith(NOTICE_IMAGE_UPLOAD_FAILED)

    def test_새_그림이_없으면_본문_그대로(self) -> None:
        poster = Fake게시기([])
        pipeline, parts = build_pipeline(responses=[codex("답")], generated_images=poster)
        pipeline.handle(make_ctx())
        assert poster.uploads == []
        assert parts["publisher"].posted[-1]["text"] == "답"

    def test_물음이_지워졌으면_올리지_않는다(self) -> None:
        class Gone:
            def alive(self, channel, ts):
                return False

        poster = Fake게시기([Path("/x/a.png")])
        pipeline, _ = build_pipeline(responses=[codex("답")], generated_images=poster,
                                     origin_alive=Gone())
        pipeline.handle(make_ctx())
        assert poster.uploads == []
