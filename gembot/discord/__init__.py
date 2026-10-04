"""GemBot's Discord layer: REST client (+ in-memory fake), embeds, publisher, feedback, setup.

GemBot runs on GitHub Actions cron, so it talks to Discord only over the REST API with a
bot token (discord.py is used once, in :func:`gembot.discord.setup.connect_gateway_once`).
"""

from gembot.discord.fake import FakeDiscord
from gembot.discord.publish import Publisher, PublishError, build_card
from gembot.discord.rest import DiscordAPI, DiscordClient, DiscordError

__all__ = [
    "DiscordAPI",
    "DiscordClient",
    "DiscordError",
    "FakeDiscord",
    "PublishError",
    "Publisher",
    "build_card",
]
