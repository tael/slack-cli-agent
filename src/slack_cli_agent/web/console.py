"""Wires the console pieces together from a profile search path."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from ..config.channel import ChannelRegistry
from ..config.profile import Profile
from ..config.slug_migration import SLUG_MIGRATION_LOCK, ChannelSlugMigrator
from .api import ApiRouter
from .editor import ChannelEditor, ProfileEditor
from .files import FileEditor
from .metrics import MetricsCollector
from .roster import BotRoster
from .server import WebServer

ASSETS_DIR = Path(__file__).resolve().parent / "assets"


class WebConsole:
    def __init__(self, search_dirs: Sequence[Path]) -> None:
        self._search_dirs = [Path(p) for p in search_dirs]
        self._profiles = ProfileEditor(self._search_dirs)
        self._roster = BotRoster(self._search_dirs)

    def router(self) -> ApiRouter:
        return ApiRouter(
            profiles=self._profiles,
            channels_for=lambda bot: ChannelEditor(self._channels(bot)),
            prompts_for=lambda bot: FileEditor(self._profile(bot).paths.prompts),
            knowledge_for=lambda bot: FileEditor(self._profile(bot).paths.knowledge),
            learned_for=lambda bot: FileEditor(self._profile(bot).paths.learned),
            metrics_for=lambda bot: MetricsCollector(self._profile(bot)),
            roster=self._roster,
        )

    def server(self, *, port: int = 8787) -> WebServer:
        return WebServer(port=port, router=self.router(), assets_dir=ASSETS_DIR)

    def _channels(self, bot: str) -> ChannelRegistry:
        """Renaming a channel here moves its files too. Wiring this only in
        application.py left a web rename writing under the old name until the
        worker happened to reparse (sca-a26x)."""
        paths = self._profile(bot).paths
        return ChannelRegistry(
            paths.channels,
            on_slug_change=ChannelSlugMigrator(
                file_dirs=(paths.knowledge, paths.learned),
                tree_roots=(paths.responses,),
                lock_path=paths.root / SLUG_MIGRATION_LOCK,
            ).migrate,
        )

    def _profile(self, name: str) -> Profile:
        return Profile.load(name, self._search_dirs)
