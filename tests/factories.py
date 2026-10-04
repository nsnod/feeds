"""Small builders shared by the test-suite. Keep them dumb and explicit."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from gembot.config import Config, load_config
from gembot.http import Budget, HttpClient
from gembot.models import Comment, Engagement, Game, Mention

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
GOLDEN = ROOT / "tests" / "golden"
NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


def fixture_path(*parts: str) -> Path:
    return FIXTURES.joinpath(*parts)


def read_fixture(*parts: str) -> str:
    return fixture_path(*parts).read_text(encoding="utf-8")


# The pinned config the tests run against (never the user's editable config/ folder).
TEST_CONFIG_DIR = ROOT / "tests" / "fixtures" / "config"


def make_config(env: dict[str, str] | None = None, **overrides: Any) -> Config:
    """The pinned test config with optional env secrets; overrides replace top-level parts."""
    config = load_config(TEST_CONFIG_DIR, env=env or {})
    return config.model_copy(update=overrides) if overrides else config


# The feeds.yaml a user produced in GitHub's web editor on 2026-10-04 (byte for byte).
INCIDENT_FEEDS = FIXTURES / "feeds_yaml" / "incident_2026-10-04.yaml"


def config_dir_with_feeds(tmp_path: Path, feeds_yaml: Path | str) -> Path:
    """A copy of the pinned test config whose feeds.yaml is ``feeds_yaml`` (a file, or its text)."""
    directory = tmp_path / "config"
    directory.mkdir(parents=True, exist_ok=True)
    for source in TEST_CONFIG_DIR.glob("*.yaml"):
        (directory / source.name).write_bytes(source.read_bytes())
    text = feeds_yaml.read_text(encoding="utf-8") if isinstance(feeds_yaml, Path) else feeds_yaml
    (directory / "feeds.yaml").write_text(text, encoding="utf-8")
    return directory


def no_sleep(_: float) -> None:
    return None


def make_http(transport: httpx.BaseTransport | None = None, **kwargs: Any) -> HttpClient:
    return HttpClient(
        user_agent="GemBot-test/0.1",
        transport=transport,
        sleep=kwargs.pop("sleep", no_sleep),
        clock=lambda: NOW,
        **kwargs,
    )


def budget(name: str = "test", limit: int = 100) -> Budget:
    return Budget(name, limit)


def make_mention(
    source: str = "reddit",
    source_id: str = "abc",
    *,
    title: str = "",
    text: str = "",
    url: str | None = None,
    author: str | None = "dev_person",
    audience: int | None = None,
    created_at: datetime | None = None,
    hours_ago: float | None = None,
    likes: int = 0,
    comments: int = 0,
    shares: int = 0,
    links: list[str] | None = None,
    channel: str | None = None,
    **extra: Any,
) -> Mention:
    if created_at is None:
        created_at = NOW - timedelta(hours=hours_ago if hours_ago is not None else 2)
    return Mention(
        source=source,
        source_id=source_id,
        url=url or f"https://example.com/{source}/{source_id}",
        title=title,
        text=text,
        author=author,
        author_audience=audience,
        created_at=created_at,
        engagement=Engagement(likes=likes, comments=comments, shares=shares),
        links=links or [],
        channel=channel,
        **extra,
    )


def make_game(game_id: str = "t:test-game-000000", title: str = "Test Game", **kwargs: Any) -> Game:
    kwargs.setdefault("first_seen", NOW - timedelta(hours=3))
    kwargs.setdefault("last_seen", NOW)
    return Game(game_id=game_id, title=title, **kwargs)


def make_comments(texts: list[str], *, authors: list[str] | None = None) -> list[Comment]:
    authors = authors or [f"user{i}" for i in range(len(texts))]
    return [
        Comment(id=f"c{i}", author=a, text=t) for i, (a, t) in enumerate(zip(authors, texts, strict=True))
    ]
