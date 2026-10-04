"""Source collectors (one module per platform) and the registry the pipeline uses."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gembot.collectors.base import CollectContext, Collector


def collector_classes() -> list[type[Collector]]:
    """Every collector, in the order they run (Steam first: other posts link to it)."""
    from gembot.collectors.itch import ItchCollector
    from gembot.collectors.rss import RssCollector
    from gembot.collectors.steam import SteamCollector
    from gembot.collectors.x import XCollector

    return [SteamCollector, ItchCollector, RssCollector, XCollector]


def build_collectors(ctx: CollectContext) -> dict[str, Collector]:
    return {cls.name: cls(ctx) for cls in collector_classes()}
