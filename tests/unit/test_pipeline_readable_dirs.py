"""읽기 허용 경로가 요청까지 전달되는지 본다.

허용 도구 목록과 같은 자리에서 readable_dirs 도 비어 있었다. claude 는
--add-dir 로, codex·gemini 는 시스템 지침 문장으로 이 값을 받으므로, 비면
작업 디렉터리 밖의 페르소나·프롬프트 파일을 못 읽는다.
"""

from __future__ import annotations

from pathlib import Path

from test_pipeline import build_pipeline, make_ctx, ok_response


def test_설정한_읽기_경로가_요청에_실린다() -> None:
    dirs = (Path("/tmp/읽기-가-고유"), Path("/tmp/읽기-나-고유"))
    pipeline, parts = build_pipeline(responses=[ok_response()], readable_dirs=dirs)

    pipeline.handle(make_ctx(user="U1"))

    assert parts["runner"].calls[0].readable_dirs == dirs


def test_설정이_없으면_비어_있다() -> None:
    pipeline, parts = build_pipeline(responses=[ok_response()])

    pipeline.handle(make_ctx(user="U1"))

    assert parts["runner"].calls[0].readable_dirs == ()


def test_Application이_상태_경로를_파이프라인에_넘긴다(tmp_path) -> None:
    """부품을 만든 것과 조립에 연결한 것은 다르다. 리뷰 엔진은 이 값을 넘기는데
    본 파이프라인은 안 넘겼다."""
    from test_application import FakeSlackClient, write_profile

    from slack_cli_agent.core.application import Application

    profile = write_profile(tmp_path)
    app = Application(profile, FakeSlackClient())

    assert app.pipeline().readable_dirs == (profile.paths.persona, profile.paths.prompts)
