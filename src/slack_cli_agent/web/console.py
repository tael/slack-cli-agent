"""Wires the console pieces together from a profile search path."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from ..config.channel import ChannelRegistry
from ..config.profile import Profile
from .api import ApiRouter
from .editor import ChannelEditor, ProfileEditor
from .files import FileEditor
from .metrics import MetricsCollector
from .server import WebServer

ASSETS_DIR = Path(__file__).resolve().parent / "assets"


class WebConsole:
    def __init__(self, search_dirs: Sequence[Path]) -> None:
        self._search_dirs = [Path(p) for p in search_dirs]
        self._profiles = ProfileEditor(self._search_dirs)

    def router(self) -> ApiRouter:
        return ApiRouter(
            profiles=self._profiles,
            channels_for=lambda bot: ChannelEditor(ChannelRegistry(self._profile(bot).paths.channels)),
            prompts_for=lambda bot: FileEditor(self._profile(bot).paths.prompts),
            knowledge_for=lambda bot: FileEditor(self._profile(bot).paths.knowledge),
            metrics_for=lambda bot: MetricsCollector(self._profile(bot)),
        )

    def server(self, *, port: int = 8787) -> WebServer:
        return WebServer(port=port, router=self.router(), assets_dir=ASSETS_DIR)

    def _profile(self, name: str) -> Profile:
        return Profile.load(name, self._search_dirs)
