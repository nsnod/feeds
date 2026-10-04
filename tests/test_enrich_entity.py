"""Game resolution: hard ids, link expansion, title candidates and the Resolver."""

from __future__ import annotations

import time
from datetime import timedelta

import httpx
import pytest
import respx

from gembot.config import ResolverSource
from gembot.enrich.entity import (
    LinkExpander,
    Resolver,
    apply_llm_title,
    apply_steam_info,
    canonical_hard_id,
    developer_hint,
    extract_urls,
    hard_ids_for,
    normalize_title,
    title_candidates,
    title_game_id,
)
from gembot.http import Budget
from gembot.models import Features, Game, Mention, SteamInfo
from tests.factories import NOW, make_game, make_http, make_mention

SHORTENERS = ["bit.ly", "t.co", "tinyurl.com", "s.team"]
STEAM_URL = "https://store.steampowered.com/app/777/Gorilla_Pizza_Panic/"


def steam_extra(
    appid: int = 777, name: str = "Gorilla Pizza Panic", dev: str = "Gorilla Dev", **more
) -> dict:
    return {"steam": {"appid": appid, "name": name, "developers": [dev], **more}}


def steam_mention(
    appid: int = 777, name: str = "Gorilla Pizza Panic", dev: str = "Gorilla Dev", **kw
) -> Mention:
    extra = steam_extra(appid, name, dev, **kw.pop("steam", {}))
    kw.setdefault("author", None)
    kw.setdefault("hours_ago", 1)
    return make_mention(
        "steam",
        str(appid),
        title=name,
        url=f"https://store.steampowered.com/app/{appid}/",
        channel="steam:coming-soon",
        extra=extra,
        **kw,
    )


def resolver(games: dict[str, Game], mentions: list[Mention] | None = None, **kw) -> Resolver:
    store = {m.key: m for m in mentions or []}
    kw.setdefault("now", NOW)
    kw.setdefault("settings", ResolverSource())
    return Resolver(games, store, **kw)


def resolve(games: dict[str, Game], mentions: list[Mention], **kw):
    return resolver(games, mentions, **kw).resolve(mentions)


# ---------------------------------------------------------------- canonical hard ids


@pytest.mark.parametrize(
    "url",
    [
        "https://store.steampowered.com/app/1234560/Gorilla_Pizza_Panic/",
        "https://store.steampowered.com/app/1234560",
        "http://store.steampowered.com/app/1234560/",
        "https://store.steampowered.com/app/1234560/Gorilla_Pizza_Panic/?snr=1_7_15__13",
        "https://store.steampowered.com/app/1234560/#app_reviews_hash",
        "https://store.steampowered.com/app/1234560?l=english&utm_source=reddit&utm_medium=social",
        "store.steampowered.com/app/1234560",
        "STORE.STEAMPOWERED.COM/APP/1234560",
        "https://www.steampowered.com/app/1234560/",
        "https://steampowered.com/app/1234560",
        "https://steamcommunity.com/app/1234560",
        "https://steamcommunity.com/app/1234560/discussions/0/123/",
        "https://store.steampowered.com/agecheck/app/1234560/",
        "https://store.steampowered.com/news/app/1234560",
        "https://store.steampowered.com/widget/1234560/",
        "https://s.team/a/1234560",
        "https://s.team/a/1234560/",
        "steam://store/1234560",
        "steam://openurl/https://store.steampowered.com/app/1234560",
        "https://steamcommunity.com/linkfilter/?u=https%3A%2F%2Fstore.steampowered.com%2Fapp%2F1234560%2F",
        "https://www.youtube.com/redirect?event=video_description&q=https%3A%2F%2Fstore.steampowered.com%2Fapp%2F1234560",
        "https://www.google.com/url?q=https://store.steampowered.com/app/1234560/&sa=D",
        "//store.steampowered.com/app/1234560",
        " <https://store.steampowered.com/app/1234560> ",
    ],
)
def test_steam_url_variants_canonicalize_to_one_id(url):
    assert canonical_hard_id(url) == "steam:1234560"


@pytest.mark.parametrize(
    "url",
    [
        "https://gorilladev.itch.io/gorilla-pizza-panic",
        "http://gorilladev.itch.io/gorilla-pizza-panic/",
        "https://GorillaDev.itch.io/Gorilla-Pizza-Panic",
        "gorilladev.itch.io/gorilla-pizza-panic",
        "https://gorilladev.itch.io/gorilla-pizza-panic?secret=abc",
        "https://gorilladev.itch.io/gorilla-pizza-panic/devlog/123/first-devlog",
        "https://gorilladev.itch.io/gorilla-pizza-panic/download/xyz",
        "https://gorilladev.itch.io/gorilla-pizza-panic#comments",
    ],
)
def test_itch_url_variants_canonicalize_to_one_id(url):
    assert canonical_hard_id(url) == "itch:gorilladev/gorilla-pizza-panic"


@pytest.mark.parametrize(
    "url",
    [
        "https://itch.io",
        "https://itch.io/games/tag-co-op",
        "https://itch.io/jam/gmtk-2026",
        "https://itch.io/profile/gorilladev",
        "https://gorilladev.itch.io/",
        "https://gorilladev.itch.io",
        "https://www.itch.io/games",
        "https://static.itch.io/app.js",
        "https://gorilladev.itch.io/rss.xml",
        "https://store.steampowered.com/",
        "https://store.steampowered.com/search/?tags=1685",
        "https://store.steampowered.com/app/abc",
        "https://store.steampowered.com/app/0",
        "https://store.steampowered.com/sub/12345",
        "https://steamcommunity.com/id/someone",
        "https://steamcommunity.com/linkfilter/?u=https%3A%2F%2Fexample.com",
        "https://example.com/app/123",
        "https://www.reddit.com/r/IndieDev/comments/abc/my_game/",
        "ftp://store.steampowered.com/app/1",
        "steam://friends",
        "https://[broken",
        "not a url",
        "   ",
        "",
    ],
)
def test_non_game_urls_have_no_hard_id(url):
    assert canonical_hard_id(url) is None


def test_extract_urls_handles_markdown_punctuation_and_bare_links():
    text = (
        "Check [my game](https://store.steampowered.com/app/1/Game/) and (https://dev.itch.io/x). "
        "Also store.steampowered.com/app/2, gorilla.itch.io/pizza! https://bit.ly/abc, <https://t.co/x> "
        '"https://example.com/a?b=1&amp;c=2" **https://s.team/a/3** escaped https://store.steampowered.com/app/4/My\\_Game/'
    )
    assert extract_urls(text) == [
        "https://store.steampowered.com/app/1/Game/",
        "https://dev.itch.io/x",
        "https://store.steampowered.com/app/2",
        "https://gorilla.itch.io/pizza",
        "https://bit.ly/abc",
        "https://t.co/x",
        "https://example.com/a?b=1&c=2",
        "https://s.team/a/3",
        "https://store.steampowered.com/app/4/My_Game/",
    ]


def test_extract_urls_keeps_balanced_parentheses_and_dedupes():
    text = "see https://en.wikipedia.org/wiki/Foo_(bar) and [https://a.com/x](https://a.com/x) twice: https://a.com/x"
    assert extract_urls(text) == ["https://en.wikipedia.org/wiki/Foo_(bar)", "https://a.com/x"]
    assert extract_urls("") == []
    assert extract_urls("email me@dev.itch.io or visit itch.io") == []


# ---------------------------------------------------------------- link expansion


@respx.mock
def test_shortened_link_resolves_within_budget_and_is_cached():
    route = respx.head("https://bit.ly/gpp").mock(
        return_value=httpx.Response(301, headers={"Location": STEAM_URL})
    )
    budget = Budget("resolver", 5)
    expander = LinkExpander(make_http(), budget, SHORTENERS)
    assert expander.expand("https://bit.ly/gpp") == STEAM_URL
    assert expander.expand("https://bit.ly/gpp") == STEAM_URL
    assert route.call_count == 1 and budget.used == 1
    mention = make_mention("bluesky", "b1", text="new trailer! https://bit.ly/gpp")
    assert hard_ids_for(mention, expander) == ["steam:777"]
    assert route.call_count == 1  # served from the cache


@respx.mock
def test_two_hop_chain_with_budget_of_one_returns_partial_result_without_raising():
    respx.head("https://bit.ly/a").mock(
        return_value=httpx.Response(301, headers={"Location": "https://t.co/b"})
    )
    second = respx.head("https://t.co/b").mock(
        return_value=httpx.Response(302, headers={"Location": STEAM_URL})
    )
    budget = Budget("resolver", 1)
    expander = LinkExpander(make_http(), budget, SHORTENERS)
    assert expander.expand("https://bit.ly/a") == "https://t.co/b"
    assert second.call_count == 0 and budget.used == 1
    assert "https://bit.ly/a" not in expander.cache  # partial results are not cached
    assert expander.expand("https://bit.ly/a") == "https://bit.ly/a"  # budget gone: original back


@respx.mock
def test_full_chain_follows_up_to_three_hops():
    respx.head("https://bit.ly/a").mock(
        return_value=httpx.Response(301, headers={"Location": "https://t.co/b"})
    )
    respx.head("https://t.co/b").mock(
        return_value=httpx.Response(302, headers={"Location": "https://tinyurl.com/c"})
    )
    respx.head("https://tinyurl.com/c").mock(
        return_value=httpx.Response(307, headers={"Location": "https://bit.ly/d"})
    )
    last = respx.head("https://bit.ly/d").mock(
        return_value=httpx.Response(301, headers={"Location": STEAM_URL})
    )
    expander = LinkExpander(make_http(), Budget("resolver", 10), SHORTENERS)
    assert expander.expand("https://bit.ly/a") == "https://bit.ly/d"  # stopped after 3 hops
    assert last.call_count == 0


@respx.mock
def test_head_405_falls_back_to_get_and_relative_location():
    respx.head("https://tinyurl.com/x").mock(return_value=httpx.Response(405))
    respx.get("https://tinyurl.com/x").mock(
        return_value=httpx.Response(302, headers={"Location": "https://store.steampowered.com/app/5/"})
    )
    respx.head("https://bit.ly/rel").mock(return_value=httpx.Response(301, headers={"Location": "/next"}))
    respx.head("https://bit.ly/next").mock(return_value=httpx.Response(200))
    budget = Budget("resolver", 10)
    expander = LinkExpander(make_http(), budget, SHORTENERS)
    assert expander.expand("https://tinyurl.com/x") == "https://store.steampowered.com/app/5/"
    assert budget.used == 2
    assert expander.expand("https://bit.ly/rel") == "https://bit.ly/next"


@respx.mock
def test_meta_refresh_page_is_followed():
    respx.head("https://t.co/m").mock(return_value=httpx.Response(405))
    respx.get("https://t.co/m").mock(
        return_value=httpx.Response(
            200,
            text='<html><head><meta http-equiv="refresh" content="0;URL=https://store.steampowered.com/app/9/"></head>',
        )
    )
    expander = LinkExpander(make_http(), Budget("resolver", 5), SHORTENERS)
    assert expander.expand("https://t.co/m") == "https://store.steampowered.com/app/9/"


@respx.mock
def test_errors_return_the_original_url_and_are_cached():
    route = respx.head("https://bit.ly/broken").mock(return_value=httpx.Response(500))
    respx.head("https://bit.ly/limited").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "900"})
    )
    respx.head("https://bit.ly/404").mock(return_value=httpx.Response(404))
    respx.head("https://bit.ly/noloc").mock(return_value=httpx.Response(301))
    expander = LinkExpander(make_http(retries=1), Budget("resolver", 20), SHORTENERS)
    assert expander.expand("https://bit.ly/broken") == "https://bit.ly/broken"
    assert expander.expand("https://bit.ly/broken") == "https://bit.ly/broken"
    assert route.call_count == 2  # one attempt + one retry, then cached
    assert expander.expand("https://bit.ly/limited") == "https://bit.ly/limited"
    assert expander.expand("https://bit.ly/404") == "https://bit.ly/404"
    assert expander.expand("https://bit.ly/noloc") == "https://bit.ly/noloc"
    assert len(expander.errors) == 3


@respx.mock
def test_non_shortened_links_are_never_requested():
    expander = LinkExpander(make_http(), Budget("resolver", 5), ["www.bit.ly", " ", "t.co"])
    assert expander.expand("https://example.com/x") == "https://example.com/x"
    assert expander.expand("") == ""
    assert expander.is_shortened("https://bit.ly/x") and not expander.is_shortened("https://bit.lyx/x")
    assert expander.budget.used == 0


# ---------------------------------------------------------------- hard ids for a mention


def test_hard_ids_for_collects_links_text_and_own_ids_steam_first():
    m = make_mention(
        "reddit",
        "r1",
        title="My game is on itch https://gorilladev.itch.io/gpp",
        text="Wishlist: store.steampowered.com/app/777 (also https://store.steampowered.com/app/777/Gorilla/)",
        links=["https://example.com/blog", "https://GorillaDev.itch.io/gpp/devlog/1"],
    )
    assert hard_ids_for(m) == ["steam:777", "itch:gorilladev/gpp"]
    assert hard_ids_for(steam_mention(42, text="see also https://store.steampowered.com/app/43/")) == [
        "steam:42",
        "steam:43",
    ]
    no_extra = make_mention("steam", "55", url="https://store.steampowered.com/app/55/")
    assert hard_ids_for(no_extra) == ["steam:55"]
    odd = make_mention("steam", "weird-id", url="https://store.steampowered.com/app/56/")
    assert hard_ids_for(odd) == ["steam:56"]
    assert hard_ids_for(make_mention("steam", "weird-id", url="https://example.com/")) == []
    itch = make_mention("itch", "GorillaDev/Gorilla-Pizza-Panic", url="https://example.com/feed-item")
    assert hard_ids_for(itch) == ["itch:gorilladev/gorilla-pizza-panic"]
    itch_url = make_mention("itch", "123", url="https://gorilladev.itch.io/gpp")
    assert hard_ids_for(itch_url) == ["itch:gorilladev/gpp"]
    assert hard_ids_for(make_mention("itch", "123", url="https://itch.io/")) == []


# ---------------------------------------------------------------- titles


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Gorilla Pizza Panic", "gorilla pizza panic"),
        ("Gorilla Pizza Panic — announce trailer", "gorilla pizza panic"),
        ("Gorilla Pizza Panic - Official Trailer", "gorilla pizza panic"),
        ("Gorilla Pizza Panic: Official Gameplay Trailer", "gorilla pizza panic"),
        ("Gorilla Pizza Panic (Demo)", "gorilla pizza panic"),
        ("Gorilla Pizza Panic Demo", "gorilla pizza panic"),
        ("Gorilla Pizza Panic Playtest", "gorilla pizza panic"),
        ("Gorilla Pizza Panic Early Access", "gorilla pizza panic"),
        ("Gorilla Pizza Panic on Steam", "gorilla pizza panic"),
        ("Gorilla Pizza Panic | Steam", "gorilla pizza panic"),
        ("Gorilla Pizza Panic | A co-op game about pizza", "gorilla pizza panic"),
        ("Devlog #3 | Gorilla Pizza Panic", "gorilla pizza panic"),
        ("[Devlog] Gorilla Pizza Panic", "gorilla pizza panic"),
        ("Gorilla Pizza Panic devlog 12", "gorilla pizza panic"),
        ("GORILLA PIZZA PANIC 🍕🦍", "gorilla pizza panic"),
        ("Gorilla  Pizza   Panic!!!", "gorilla pizza panic"),
        ("Gorilla Pizza Panic - A Chaotic Co-op Pizza Game", "gorilla pizza panic"),
        ("Gorilla Pizza Panic: Deluxe Edition", "gorilla pizza panic deluxe edition"),
        ("Pokémon Café Mix", "pokemon cafe mix"),
        ("Don't Starve Together", "dont starve together"),
        ("Bread & Butter", "bread and butter"),
        ("Official Trailer", "official trailer"),
        ("Demo", "demo"),
        ("(Demo)", "demo"),
        ("🍕", ""),
        ("", ""),
    ],
)
def test_normalize_title(title, expected):
    assert normalize_title(title) == expected


@pytest.mark.parametrize(
    ("source", "title", "text", "best"),
    [
        ("reddit", "My game Gorilla Pizza Panic is a co-op pizza delivery game", "", "Gorilla Pizza Panic"),
        ("reddit", "My Game Gorilla Pizza Panic Is A Co-op Pizza Delivery Game", "", "Gorilla Pizza Panic"),
        ("bluesky", "Gorilla Pizza Panic — announce trailer", "", "Gorilla Pizza Panic"),
        ("youtube", "Moonlit Mayhem - Official Trailer", "", "Moonlit Mayhem"),
        ("youtube", "Crab Rave Racers | Steam Next Fest Demo", "", "Crab Rave Racers"),
        (
            "reddit",
            'Our co-op horror game "Night Shift Janitors" just got a Steam page',
            "",
            "Night Shift Janitors",
        ),
        ("reddit", "Bonk Brigade demo is out! Grab your friends", "", "Bonk Brigade"),
        ("reddit", "Wishlist Goblin Guts on Steam — 4 player co-op chaos", "", "Goblin Guts"),
        (
            "x",
            "Stack Overflowers is a physics-based co-op game about building towers with friends",
            "",
            "Stack Overflowers",
        ),
        ("reddit", "After 2 years of work, Cave Divers finally has a Steam page!", "", "Cave Divers"),
        ("bluesky", "Introducing “Mop Squad”, a proximity chat cleaning game", "", "Mop Squad"),
        ("reddit", "[Devlog] Haunted Hotdog Stand: week 12 update", "", "Haunted Hotdog Stand"),
        ("reddit", "I've been working on my first game, Lantern Keepers, for 2 years", "", "Lantern Keepers"),
        ("reddit", "Me and my friends made SLOPCORE, a 4-player co-op chaos game", "", "SLOPCORE"),
        ("x", "my co-op game Pizza Panic is coming to Steam", "", "Pizza Panic"),
        ("reddit", "Our game is called Bread Boys and it's a co-op baking disaster", "", "Bread Boys"),
        ("reddit", "What do you think of our new trailer for Spooky Spelunkers?", "", "Spooky Spelunkers"),
        ("reddit", "Lethal Company meets Overcooked: our game Freezer Burn", "", "Freezer Burn"),
        (
            "reddit",
            "Check out the Gorilla Pizza Panic demo, it's like R.E.P.O. but with pizza",
            "",
            "Gorilla Pizza Panic",
        ),
        ("reddit", "Escape From Gorilla Island - Official Trailer", "", "Escape From Gorilla Island"),
        ("reddit", "The 'Tiny Tavern Brawl' playtest starts Friday", "", "Tiny Tavern Brawl"),
        (
            "bluesky",
            "",
            "We just released the demo for Tiny Tavern Brawl on Steam! #indiedev https://store.steampowered.com/app/1/X/",
            "Tiny Tavern Brawl",
        ),
        (
            "reddit",
            "Feedback wanted",
            "Hi all! My game, Moss Movers, is a co-op moving sim. Thoughts?",
            "Moss Movers",
        ),
        ("youtube", "Mop Squad: Official Announcement Trailer", "", "Mop Squad"),
        ("tiktok", "POV: you and the boys in Ghost Gas Station 👻", "game demo out now", "Ghost Gas Station"),
    ],
)
def test_title_candidates_best_first(source, title, text, best):
    candidates = title_candidates(make_mention(source, "x", title=title, text=text))
    assert candidates, title
    assert candidates[0] == best


@pytest.mark.parametrize(
    "title",
    [
        "What engine should I use?",
        "Screenshot Saturday #612",
        "How do you market a co-op game?",
        "Unity vs Godot for multiplayer?",
        "Looking for playtesters for my game!",
        "New enemy animation for my co-op horror game",
        "I made a co-op game where you deliver pizza as gorillas",
        "Is a Lethal Company-like game still worth making?",
        "Indie Game Steam Page Tips",
        "My Game",
        "Monday Devlog",
        "We need more games like R.E.P.O.",
        "Anyone want to play Lethal Company tonight?",
    ],
)
def test_title_candidates_reject_generic_posts(title):
    assert title_candidates(make_mention("reddit", "x", title=title)) == []


def test_title_candidates_for_store_listings_use_the_title_itself():
    assert title_candidates(
        steam_mention(1, name="Gorilla Pizza Panic: The Extra Cheesy Deluxe Edition")
    ) == ["Gorilla Pizza Panic: The Extra Cheesy Deluxe Edition"]
    steam_no_extra = make_mention("steam", "2", title="Some Steam Game", extra={})
    assert title_candidates(steam_no_extra) == ["Some Steam Game"]
    assert title_candidates(make_mention("itch", "dev/x", title="Tiny Tavern Brawl")) == ["Tiny Tavern Brawl"]
    assert title_candidates(make_mention("itch", "dev/x", title="")) == []


def test_title_candidates_are_deduplicated_and_capped():
    m = make_mention(
        "reddit",
        "x",
        title='"Gorilla Pizza Panic" - Official Trailer',
        text="My game Gorilla Pizza Panic demo is out! Gorilla Pizza Panic is a co-op game.",
    )
    assert title_candidates(m) == ["Gorilla Pizza Panic"]


@pytest.mark.parametrize(
    ("mention", "expected"),
    [
        (steam_mention(1, dev="Gorilla Dev"), "Gorilla Dev"),
        (make_mention("steam", "3", extra={"steam": {"appid": 3}}), None),
        (make_mention("itch", "gorilladev/gpp", author="Gorilla Dev Display"), "gorilladev"),
        (make_mention("itch", "123", url="https://example.com", author="Some Dev"), "Some Dev"),
        (
            make_mention("reddit", "a", title="My game Gorilla Pizza Panic", author="gorilla_dev"),
            "gorilla_dev",
        ),
        (make_mention("reddit", "b", text="I've been working on this for years", author="dev1"), "dev1"),
        (
            make_mention("bluesky", "c", text="We're making a co-op game!", author="studio.bsky.social"),
            "studio.bsky.social",
        ),
        (make_mention("x", "d", text="we just released our demo", author="devx"), "devx"),
        (make_mention("reddit", "e", text="Our studio is tiny", author="s"), "s"),
        (make_mention("reddit", "f", title="I made a co-op game", author="maker"), "maker"),
        (make_mention("reddit", "g", title="Look at this cool game I found", author="fan"), None),
        (make_mention("reddit", "h", title="My favorite game is Lethal Company", author="fan"), None),
        (make_mention("reddit", "i", title="my game", author=None), None),
    ],
)
def test_developer_hint(mention, expected):
    assert developer_hint(mention) == expected


# ---------------------------------------------------------------- resolver


def gorilla_reddit(**kw) -> Mention:
    kw.setdefault("author", "gorilla_dev")
    kw.setdefault("hours_ago", 5)
    return make_mention(
        "reddit",
        kw.pop("source_id", "r1"),
        title="My game Gorilla Pizza Panic is a co-op pizza delivery game",
        channel="r/IndieDev",
        **kw,
    )


def gorilla_bluesky(
    author: str = "gorilla_dev", text: str = "Our co-op game finally has a trailer!", **kw
) -> Mention:
    kw.setdefault("hours_ago", 3)
    return make_mention(
        "bluesky",
        kw.pop("source_id", "b1"),
        title="Gorilla Pizza Panic — announce trailer",
        text=text,
        author=author,
        **kw,
    )


def pizza_panic(author: str = "pizza_guy", **kw) -> Mention:
    kw.setdefault("hours_ago", 1)
    return make_mention(
        "reddit",
        kw.pop("source_id", "p1"),
        title="my game Pizza Panic is a co-op cooking game",
        author=author,
        **kw,
    )


def test_reddit_and_bluesky_posts_merge_into_one_game():
    games: dict[str, Game] = {}
    reddit, bsky = gorilla_reddit(), gorilla_bluesky()
    result = resolve(games, [bsky, reddit])
    assert len(games) == 1
    (game,) = games.values()
    assert game.game_id.startswith("t:gorilla-pizza-panic-")
    assert result.new_games == [game.game_id]
    assert result.assignments == {reddit.key: game.game_id, bsky.key: game.game_id}
    assert game.mention_keys == [reddit.key, bsky.key]  # oldest first
    assert reddit.game_id == bsky.game_id == game.game_id
    assert game.title == "Gorilla Pizza Panic"
    assert game.developer == "gorilla_dev"
    assert game.first_seen == reddit.created_at and game.last_seen == NOW


def test_bluesky_with_unknown_developer_merges_in_a_later_run():
    games: dict[str, Game] = {}
    resolve(games, [gorilla_reddit()])
    later = gorilla_bluesky(author="fan.bsky.social", text="this looks so fun", hours_ago=-2)
    result = resolve(games, [later], now=NOW + timedelta(hours=3))
    assert len(games) == 1 and result.new_games == []
    (game,) = games.values()
    assert later.key in game.mention_keys and game.last_seen == NOW + timedelta(hours=3)


def test_bluesky_handle_matches_reddit_username_as_same_developer():
    games: dict[str, Game] = {}
    resolve(games, [gorilla_reddit()])
    # different handle format, title differs a bit, both first-person
    bsky = make_mention(
        "bluesky",
        "b9",
        text="Our game Gorilla Pizza Panic Deluxe is coming to Steam",
        author="gorilladev.bsky.social",
    )
    resolve(games, [bsky])
    assert len(games) == 1


def test_pizza_panic_from_a_different_known_developer_does_not_merge():
    games: dict[str, Game] = {}
    resolve(games, [gorilla_reddit(), gorilla_bluesky()])
    result = resolve(games, [pizza_panic()])
    assert len(games) == 2
    gid = result.assignments["reddit:p1"]
    assert games[gid].title == "Pizza Panic" and games[gid].developer == "pizza_guy"


def test_pizza_panic_by_unknown_developer_needs_a_close_title_ratio():
    games: dict[str, Game] = {}
    resolve(games, [gorilla_reddit()])
    fan_post = make_mention("reddit", "fan", title="Pizza Panic demo is out", author="fan")
    resolve(games, [fan_post])
    assert len(games) == 2


def test_identical_distinctive_title_merges_despite_different_known_developers():
    games: dict[str, Game] = {}
    resolve(games, [gorilla_reddit()])
    other = make_mention(
        "x", "x1", text="my game Gorilla Pizza Panic just got a trailer", author="teammate42"
    )
    resolve(games, [other])
    assert len(games) == 1


def test_later_steam_mention_attaches_hard_id_to_existing_title_game():
    games: dict[str, Game] = {}
    resolve(games, [gorilla_reddit(media_thumb="https://img/reddit.jpg")])
    (gid,) = games
    steam = steam_mention(
        777,
        dev="Gorilla Dev",
        steam={"publishers": ["Banana Pub"], "header_image": "https://img/header.jpg"},
    )
    result = resolve(games, [steam], now=NOW + timedelta(hours=6))
    assert list(games) == [gid]  # no duplicate, id unchanged
    game = games[gid]
    assert result.assignments == {steam.key: gid} and result.new_games == []
    assert game.steam_appid == 777 and game.hard_ids == ["steam:777"]
    assert game.steam is not None and game.steam.appid == 777
    assert game.developer == "Gorilla Dev" and game.publisher == "Banana Pub"
    assert game.thumb == "https://img/header.jpg"
    assert game.best_url() == "https://store.steampowered.com/app/777/"
    # and from now on a Reddit link to the store page joins by hard id
    linked = make_mention(
        "reddit", "r2", title="Wow", text=f"look {STEAM_URL}", author="someone", hours_ago=-5
    )
    resolve(games, [linked], now=NOW + timedelta(hours=7))
    assert linked.game_id == gid


def test_steam_mention_from_another_developer_does_not_attach_to_similar_title():
    games: dict[str, Game] = {}
    resolve(games, [gorilla_reddit()])
    steam = steam_mention(888, name="Pizza Panic", dev="Totally Different Studio")
    result = resolve(games, [steam])
    assert set(games) == {next(g for g in games if g.startswith("t:")), "steam:888"}
    assert result.new_games == ["steam:888"]
    assert games["steam:888"].hard_ids == ["steam:888"]
    assert games["steam:888"].title == "Pizza Panic"


def test_game_with_a_different_steam_app_never_absorbs_another_app():
    games = {"steam:1": make_game("steam:1", "Gorilla Pizza Panic", steam_appid=1, hard_ids=["steam:1"])}
    result = resolve(games, [steam_mention(2, name="Gorilla Pizza Panic", dev="Gorilla Dev")])
    assert set(games) == {"steam:1", "steam:2"} and result.new_games == ["steam:2"]


def test_two_games_sharing_a_hard_id_are_merged_into_the_older():
    old_mention = gorilla_reddit()
    new_mention = steam_mention(777)
    older = make_game(
        "t:gorilla-pizza-panic-aaaaaa",
        "Gorilla Pizza Panic",
        first_seen=NOW - timedelta(days=3),
        last_seen=NOW - timedelta(days=1),
        hard_ids=["steam:777"],
        mention_keys=[old_mention.key],
        developer="gorilla_dev",
    )
    newer = make_game(
        "steam:777",
        "Gorilla Pizza Panic!",
        first_seen=NOW - timedelta(days=1),
        last_seen=NOW,
        steam_appid=777,
        hard_ids=["steam:777", "itch:gorilladev/gpp"],
        mention_keys=[new_mention.key],
        thumb="https://img/t.jpg",
        aliases=["GPP"],
        steam=SteamInfo(appid=777, name="Gorilla Pizza Panic: Deluxe"),
        last_features=Features(hype=0.5),
        last_reasons=["hype"],
    )
    games = {older.game_id: older, newer.game_id: newer}
    store = {old_mention.key: old_mention, new_mention.key: new_mention}
    new_mention.game_id = "steam:777"
    result = Resolver(games, store, now=NOW, settings=ResolverSource()).resolve([])
    assert result.merged == {"steam:777": older.game_id}
    assert list(games) == [older.game_id]
    assert older.mention_keys == [old_mention.key, new_mention.key]
    assert older.hard_ids == ["steam:777", "itch:gorilladev/gpp"]
    assert older.steam_appid == 777 and older.itch_url == "https://gorilladev.itch.io/gpp"
    assert older.first_seen == NOW - timedelta(days=3) and older.last_seen == NOW
    assert older.thumb == "https://img/t.jpg"
    assert older.steam is not None and older.title == "Gorilla Pizza Panic: Deluxe"
    assert older.last_features == Features(hype=0.5) and older.last_reasons == ["hype"]
    assert {"GPP", "Gorilla Pizza Panic"} <= set(older.aliases)
    assert new_mention.game_id == older.game_id
    # a new mention of the absorbed id lands on the survivor
    again = steam_mention(777, hours_ago=-1)
    result = Resolver(games, store, now=NOW, settings=ResolverSource()).resolve([again])
    assert result.assignments == {again.key: older.game_id}


def test_mention_linking_steam_and_itch_bridges_two_games():
    steam_game = make_game("steam:5", "Moss Movers", first_seen=NOW - timedelta(days=2), hard_ids=["steam:5"])
    itch_game = make_game(
        "itch:mossdev/moss-movers",
        "Moss Movers",
        first_seen=NOW - timedelta(days=4),
        hard_ids=["itch:mossdev/moss-movers"],
        steam=SteamInfo(appid=5, name="Moss Movers (Steam)"),
    )
    games = {g.game_id: g for g in (steam_game, itch_game)}
    bridge = make_mention(
        "reddit",
        "bridge",
        title="Moss Movers is out",
        text="Steam: https://store.steampowered.com/app/5/ itch: https://mossdev.itch.io/moss-movers",
    )
    result = resolve(games, [bridge])
    assert result.merged == {"steam:5": "itch:mossdev/moss-movers"}
    assert list(games) == ["itch:mossdev/moss-movers"]
    survivor = games["itch:mossdev/moss-movers"]
    assert set(survivor.hard_ids) == {"steam:5", "itch:mossdev/moss-movers"}
    assert result.assignments == {bridge.key: "itch:mossdev/moss-movers"}


def test_post_listing_many_games_only_attaches_its_leading_link():
    games: dict[str, Game] = {}
    roundup = make_mention(
        "reddit",
        "list",
        title="Five co-op games to wishlist",
        text="https://store.steampowered.com/app/11/ https://store.steampowered.com/app/12/ https://a.itch.io/b",
    )
    result = resolve(games, [roundup])
    assert list(games) == ["steam:11"] and result.new_games == ["steam:11"]
    assert games["steam:11"].hard_ids == ["steam:11"]
    assert games["steam:11"].title == "Steam app 11"


def test_link_only_mention_creates_hard_id_game_titled_from_the_url_slug():
    games: dict[str, Game] = {}
    m = make_mention("bluesky", "s", text=f"ok {STEAM_URL}", author="fan")
    itch = make_mention("x", "i", text="https://gorilladev.itch.io/tiny-tavern-brawl")
    result = resolve(games, [m, itch])
    assert games["steam:777"].title == "Gorilla Pizza Panic"
    assert games["itch:gorilladev/tiny-tavern-brawl"].title == "Tiny Tavern Brawl"
    assert (
        games["itch:gorilladev/tiny-tavern-brawl"].itch_url == "https://gorilladev.itch.io/tiny-tavern-brawl"
    )
    assert sorted(result.new_games) == ["itch:gorilladev/tiny-tavern-brawl", "steam:777"]


def test_itch_listing_title_and_developer():
    games: dict[str, Game] = {}
    reddit = make_mention("reddit", "r", title="Our game Tavern Brawl demo is out", author="brawldev")
    resolve(games, [reddit])
    itch = make_mention(
        "itch",
        "brawldev/tavern-brawl",
        title="Tavern Brawl: Remastered",
        url="https://brawldev.itch.io/tavern-brawl",
        author=None,
    )
    resolve(games, [itch])
    assert len(games) == 1
    (game,) = games.values()
    assert game.itch_url == "https://brawldev.itch.io/tavern-brawl" and game.hard_ids == [
        "itch:brawldev/tavern-brawl"
    ]
    assert game.title == "Tavern Brawl: Remastered"  # itch title beats a heuristic candidate
    assert game.aliases == ["Tavern Brawl"]  # the old title stays matchable
    assert game.developer == "brawldev"


def test_unresolvable_mentions_are_dropped():
    games: dict[str, Game] = {}
    dropped = make_mention("reddit", "q", title="What engine should I use?", author="newbie")
    result = resolve(games, [dropped])
    assert games == {} and result.dropped == [dropped.key] and result.assignments == {}
    assert dropped.game_id is None


def test_game_ids_are_deterministic_regardless_of_input_order():
    def run(order: list[int]):
        mentions = [
            gorilla_reddit(),
            gorilla_bluesky(),
            pizza_panic(),
            steam_mention(999, name="Cave Divers", dev="Deep Co"),
            make_mention("reddit", "cd", title="Cave Divers finally has a Steam page", author="x"),
        ]
        games: dict[str, Game] = {}
        result = resolve(games, [mentions[i] for i in order])
        return sorted(games), result.assignments

    first = run([0, 1, 2, 3, 4])
    assert first == run([4, 3, 2, 1, 0]) == run([2, 0, 4, 1, 3])
    ids, assignments = first
    assert "steam:999" in ids and assignments["reddit:cd"] == "steam:999"
    assert title_game_id("Gorilla Pizza Panic", "gorilla_dev") == title_game_id(
        "Gorilla Pizza Panic — demo", "Gorilla Games"
    )
    assert title_game_id("Gorilla Pizza Panic", "a") != title_game_id("Gorilla Pizza Panic", "b")
    assert title_game_id("🍕") == title_game_id("🍕", None)


def test_llm_titles_take_precedence_over_heuristics():
    games: dict[str, Game] = {}
    m = make_mention("reddit", "llm", title="this thing i made with friends", author="maker", hours_ago=1)
    result = resolve(games, [m], llm_titles={m.key: "Sock Goblins"})
    assert games[result.assignments[m.key]].title == "Sock Goblins"
    generic = make_mention("reddit", "llm2", title="this other thing", author="maker")
    result = resolve(games, [generic], llm_titles={generic.key: "Steam"})
    assert result.dropped == [generic.key]


def test_lookback_window_limits_fuzzy_matching_but_same_id_revives():
    old = make_game(
        "t:old",
        "Gorilla Pizza Panic",
        first_seen=NOW - timedelta(days=60),
        last_seen=NOW - timedelta(days=45),
        developer="someone_else",
    )
    games = {"t:old": old}
    result = resolve(games, [gorilla_bluesky(author="fan", text="cool")])
    assert len(games) == 2 and result.new_games != ["t:old"]
    # a title game whose deterministic id already exists is reused, even outside the window
    gid = title_game_id("Gorilla Pizza Panic", "gorilla_dev")
    revived = make_game(
        gid, "Gorilla Pizza Panic", first_seen=NOW - timedelta(days=90), last_seen=NOW - timedelta(days=60)
    )
    games = {gid: revived}
    resolve(games, [gorilla_reddit()])
    assert list(games) == [gid] and revived.last_seen == NOW


def test_already_resolved_mentions_keep_their_game():
    games: dict[str, Game] = {}
    reddit = gorilla_reddit()
    resolve(games, [reddit])
    (gid,) = games
    fresh_copy = gorilla_reddit()  # same key, as returned by the collector next run
    store = {reddit.key: reddit}
    result = Resolver(games, store, now=NOW + timedelta(hours=1), settings=ResolverSource()).resolve(
        [fresh_copy]
    )
    assert result.assignments == {fresh_copy.key: gid} and fresh_copy.game_id == gid
    assert games[gid].mention_keys == [reddit.key]
    # key known only through the game record
    orphan = gorilla_reddit()
    result = Resolver(games, {}, now=NOW, settings=ResolverSource()).resolve([orphan])
    assert result.assignments == {orphan.key: gid}


def test_existing_title_game_matched_by_title_merges_with_hard_id_owner():
    title_game = make_game(
        "t:moss-movers-abc123",
        "Moss Movers",
        first_seen=NOW - timedelta(days=5),
        mention_keys=["reddit:old"],
    )
    hard_game = make_game("steam:5", "Moss Movers", first_seen=NOW - timedelta(days=1), hard_ids=["steam:5"])
    games = {g.game_id: g for g in (title_game, hard_game)}
    old = make_mention("reddit", "old", title="Moss Movers", hours_ago=100)
    old.game_id = title_game.game_id
    old.text = "https://store.steampowered.com/app/5/"
    result = Resolver(games, {old.key: old}, now=NOW, settings=ResolverSource()).resolve([old])
    assert result.merged == {"steam:5": title_game.game_id}
    assert title_game.steam_appid == 5 and list(games) == [title_game.game_id]


def test_merge_chain_and_alias_bookkeeping():
    a = make_game("t:a", "Alpha Game One", first_seen=NOW - timedelta(days=9), hard_ids=["steam:1"])
    b = make_game(
        "t:b", "Alpha Game Two", first_seen=NOW - timedelta(days=8), hard_ids=["steam:1", "itch:d/s"]
    )
    c = make_game("t:c", "Alpha Game Three", first_seen=NOW - timedelta(days=7), hard_ids=["itch:d/s"])
    games = {g.game_id: g for g in (c, b, a)}
    result = resolve(games, [])
    assert result.merged == {"t:b": "t:a", "t:c": "t:a"}
    assert list(games) == ["t:a"]
    assert a.aliases == ["Alpha Game Two", "Alpha Game Three"]


def test_apply_steam_info_sets_title_and_details_once():
    game = make_game("t:x", "Gorilla Pizza Panik", aliases=["Gorilla Pizza Panic"])
    info = SteamInfo(
        appid=3, name="Gorilla Pizza Panic", developers=["G"], publishers=["P"], header_image="h"
    )
    apply_steam_info(game, info)
    assert game.title == "Gorilla Pizza Panic" and game.aliases == ["Gorilla Pizza Panik"]
    assert (game.steam_appid, game.hard_ids, game.developer, game.publisher, game.thumb) == (
        3,
        ["steam:3"],
        "G",
        "P",
        "h",
    )
    apply_steam_info(game, SteamInfo(appid=4, name="Another"))
    assert game.title == "Gorilla Pizza Panic"  # details of another app are ignored


def test_steam_details_of_a_linked_other_app_are_ignored():
    game = make_game("steam:1", "One", steam_appid=1, hard_ids=["steam:1"])
    games = {"steam:1": game}
    m = make_mention(
        "reddit",
        "r",
        text="https://store.steampowered.com/app/1/",
        extra={"steam": {"appid": 2, "name": "Two"}},
    )
    resolve(games, [m])
    assert game.title == "One" and game.steam is None


def test_bad_steam_extra_is_ignored_and_first_seen_uses_mention_first_seen():
    first_seen = NOW - timedelta(days=2)
    m = make_mention(
        "steam", "77", title="Broken Info", extra={"steam": {"appid": "not-a-number"}}, first_seen=first_seen
    )
    m.extra["steam"] = {"name": "x", "appid": None}
    games: dict[str, Game] = {}
    resolve(games, [m])
    assert games["steam:77"].steam is None and games["steam:77"].first_seen == first_seen
    info_obj = make_mention("steam", "78", title="Obj", extra={"steam": SteamInfo(appid=78, name="Obj Game")})
    resolve(games, [info_obj])
    assert games["steam:78"].title == "Obj Game"


@respx.mock
def test_budget_running_out_on_the_get_fallback_returns_original_uncached():
    respx.head("https://bit.ly/x").mock(return_value=httpx.Response(405))
    get = respx.get("https://bit.ly/x").mock(
        return_value=httpx.Response(301, headers={"Location": STEAM_URL})
    )
    expander = LinkExpander(make_http(), Budget("resolver", 1), SHORTENERS)
    assert expander.expand("https://bit.ly/x") == "https://bit.ly/x"
    assert get.call_count == 0 and expander.cache == {} and expander.errors == []


def test_bare_links_inside_a_wrapper_url_are_not_duplicated():
    url = "https://www.google.com/url?q=store.steampowered.com/app/1&sa=D"
    assert extract_urls(f"see {url} now") == [url]
    assert canonical_hard_id(url) == "steam:1"


def test_custom_domain_handles_count_as_the_same_developer():
    games: dict[str, Game] = {}
    resolve(games, [gorilla_reddit()])
    bsky = make_mention(
        "bluesky", "cd", text="Our game Gorilla Pizza Panic Remix is out", author="gorilla.games", hours_ago=1
    )
    resolve(games, [bsky])
    assert len(games) == 1


def test_merge_unions_hard_ids_but_keeps_the_older_store_ids():
    a = make_game(
        "t:a", "Alpha", first_seen=NOW - timedelta(days=9), hard_ids=["steam:1", "itch:d/s"], steam_appid=1
    )
    b = make_game(
        "t:b", "Beta", first_seen=NOW - timedelta(days=8), hard_ids=["itch:d/s", "steam:2"], steam_appid=2
    )
    games = {g.game_id: g for g in (a, b)}
    result = resolve(games, [])
    assert result.merged == {"t:b": "t:a"}
    assert a.steam_appid == 1 and a.hard_ids == ["steam:1", "itch:d/s", "steam:2"]
    later = steam_mention(2, name="Beta", dev="x")
    result = resolve(games, [later])
    assert result.assignments == {later.key: "t:a"}


# ---------------------------------------------------------------- review follow-ups


def test_apply_llm_title_retitles_only_games_without_an_authoritative_name():
    game = make_game("t:added-ragdolls-abc123", "Added Ragdolls")
    assert apply_llm_title(game, "  Spooky   Shift ") is True
    assert game.title == "Spooky Shift" and game.aliases == ["Added Ragdolls"]
    assert apply_llm_title(game, "Spooky Shift") is False  # nothing to change
    for title in (None, "", "Steam", "My Game", "Lethal Company", "LethalCompany", "Indie Game Demo"):
        assert apply_llm_title(game, title) is False, title
    assert game.title == "Spooky Shift"
    steam_game = make_game("steam:1", "Real Steam Name", steam=SteamInfo(appid=1, name="Real Steam Name"))
    itch_game = make_game("itch:dev/x", "Real Itch Name", hard_ids=["itch:dev/x"])
    itch_url_game = make_game("t:x", "Real Itch Name", itch_url="https://dev.itch.io/x")
    for authoritative in (steam_game, itch_game, itch_url_game):
        before = authoritative.title
        assert apply_llm_title(authoritative, "Something Else") is False
        assert authoritative.title == before
    linked_only = make_game("steam:2", "Steam app 2", steam_appid=2, hard_ids=["steam:2"])
    assert apply_llm_title(linked_only, "Moss Movers") is True and linked_only.title == "Moss Movers"


def test_game_first_seen_is_when_gembot_first_saw_it_not_when_it_was_posted():
    itch = make_mention(
        "itch",
        "olddev/old-game",
        title="Old Game",
        url="https://olddev.itch.io/old-game",
        created_at=NOW - timedelta(days=10),
        first_seen=NOW,
        author=None,
    )
    games: dict[str, Game] = {}
    resolve(games, [itch])
    assert games["itch:olddev/old-game"].first_seen == NOW


def test_huge_title_case_titles_resolve_quickly():
    long_title = " ".join(["Foo"] * 3000)  # ~12 KB
    m = make_mention("reddit", "huge", title=long_title, text=" ".join(["Bar"] * 3000), author="dev")
    start = time.perf_counter()
    resolve({}, [m])
    title_candidates(make_mention("youtube", "huge2", title=" ".join(["Foo Bar"] * 8000)))
    assert time.perf_counter() - start < 1.0


def test_steam_and_itch_links_are_bridged_only_when_their_names_agree():
    same = make_mention(
        "reddit",
        "same",
        title="Dread Shift demo is out",
        text="https://store.steampowered.com/app/9/Dread_Shift/ and https://dreaddev.itch.io/dread-shift",
    )
    games: dict[str, Game] = {}
    resolve(games, [same])
    assert games["steam:9"].hard_ids == ["steam:9", "itch:dreaddev/dread-shift"]
    other = make_mention(
        "reddit",
        "other",
        title="Moss Movers demo is out",
        text="https://store.steampowered.com/app/8/Gorilla_Tag_Two/ https://mossdev.itch.io/moss-movers",
    )
    games = {}
    result = resolve(games, [other])
    # the Steam slug contradicts the post's title, so the itch page leads and nothing is bridged
    assert result.assignments[other.key] == "itch:mossdev/moss-movers"
    assert games["itch:mossdev/moss-movers"].hard_ids == ["itch:mossdev/moss-movers"]
    unnamed = make_mention(
        "reddit",
        "unnamed",
        title="Bonk Brigade demo is out",
        text="https://store.steampowered.com/app/7/ https://bonkdev.itch.io/bonk-brigade",
    )
    games = {}
    result = resolve(games, [unnamed])  # no Steam slug: the post's own title vouches for both links
    game = games[result.assignments[unnamed.key]]
    assert game.game_id == "itch:bonkdev/bonk-brigade"  # the link whose slug names the game leads
    assert game.hard_ids == ["itch:bonkdev/bonk-brigade", "steam:7"] and game.steam_appid == 7


@pytest.mark.parametrize(
    ("text", "kept", "dropped"),
    [
        ("I'd like you to try https://dreaddev.itch.io/dread-shift", "itch:dreaddev/dread-shift", None),
        (
            "Inspired by years of late night horror sessions with friends, we made "
            "https://dreaddev.itch.io/dread-shift",
            "itch:dreaddev/dread-shift",
            None,
        ),
        (
            "Fans of https://store.steampowered.com/app/1966720/ should try https://dreaddev.itch.io/dread-shift",
            "itch:dreaddev/dread-shift",
            "steam:1966720",
        ),
        (
            "It's like R.E.P.O. (https://store.steampowered.com/app/3241660/REPO/) - ours: "
            "https://store.steampowered.com/app/9/Dread_Shift/",
            "steam:9",
            "steam:3241660",
        ),
        (
            "Similar to [Lethal Company](https://store.steampowered.com/app/1966720/). "
            "Wishlist https://store.steampowered.com/app/9/",
            "steam:9",
            "steam:1966720",
        ),
    ],
)
def test_reference_links_are_ignored_but_ordinary_links_are_kept(text, kept, dropped):
    mention = make_mention(
        "reddit",
        "ref",
        title="Dread Shift demo",
        text=text,
        links=[
            "https://store.steampowered.com/app/1966720/",
            "https://store.steampowered.com/app/3241660/REPO/",
        ],
    )
    ids = hard_ids_for(mention)
    assert ids[0] == kept
    if dropped:
        assert dropped not in ids


def test_reference_link_repeated_as_the_posts_own_link_is_kept():
    mention = make_mention(
        "reddit",
        "both",
        title="Lethal Company update",
        text=f"Inspired by {STEAM_URL}? Wishlist {STEAM_URL}",
    )
    assert hard_ids_for(mention) == ["steam:777"]


def test_truncated_display_links_are_not_extracted():
    assert extract_urls("demo: dev.itch.io/gorilla-pizz... and store.steampowered.com/app/35272…)") == []
    assert extract_urls("https://store.steampowered.com/app/35272... cool") == []
    assert extract_urls("full: https://store.steampowered.com/app/352720/. Done") == [
        "https://store.steampowered.com/app/352720/"
    ]


@pytest.mark.parametrize(
    ("title", "best"),
    [
        ("Cooking Chaos - Official Trailer", "Cooking Chaos"),
        ("Shipping Simulator Deluxe is a co-op game", "Shipping Simulator Deluxe"),
        ("Viking Party Panic demo is out!", "Viking Party Panic"),
        ('"7 Days to Pizza" demo out now', "7 Days to Pizza"),
        ("60 Seconds of Panic - Announce Trailer", "60 Seconds of Panic"),
        ("Wicked Waiters demo is out", "Wicked Waiters"),
        ("Lethal Company but with pizza: Pizza Panic demo out now", "Pizza Panic"),
        ("Like Lethal Company? Meet Dread Shift, our co-op horror game", "Dread Shift"),
        ("Our game Dread Shift (inspired by Lethal Company) demo is live", "Dread Shift"),
        ("Postmortem: launching Moss Movers in Early Access", "Moss Movers"),
        ("Bonk Brigade - Devlog #12: New Weapons", "Bonk Brigade"),
        ("Bringing online co-op to Tiny Tavern Brawl", "Tiny Tavern Brawl"),
    ],
)
def test_names_survive_the_sentence_and_comparison_filters(title, best):
    assert title_candidates(make_mention("reddit", "x", title=title, author="dev"))[:1] == [best]


@pytest.mark.parametrize(
    "title",
    [
        "Building a Co-op Horror Game in Unity",
        "Adding Proximity Chat - Devlog 5",
        "Moss Movers Clone in Unity demo",
        "Supermarket Together style game demo",
        "Lethal Company Inspired co-op game",
        "Top Indie Co-op Games demo roundup",
    ],
)
def test_sentences_comparisons_and_listicles_give_no_candidate(title):
    candidates = title_candidates(make_mention("reddit", "x", title=title, author="dev"))
    assert candidates == [], candidates


def test_identical_but_sentence_shaped_titles_from_different_known_devs_do_not_merge():
    games = {
        "t:a": make_game(
            "t:a", "Making Pizza Together", developer="dev_a", first_seen=NOW - timedelta(days=1)
        )
    }
    other = make_mention("reddit", "b", title="my game Making Pizza Together demo is out", author="dev_b")
    result = resolve(games, [other])
    assert result.assignments[other.key] != "t:a"
