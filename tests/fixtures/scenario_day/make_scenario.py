"""Builds the recorded "scenario day" replayed by ``python -m gembot replay``.

Run ``python tests/fixtures/scenario_day/make_scenario.py`` to regenerate every file in this
folder (deterministic). The response bodies follow the 2026 formats documented in
docs/VERIFICATION.md (same shapes as the per-collector fixtures); the games are made up.

The story (Saturday 3 Oct 2026, all times UTC):

* 06:07 run 1 - first sightings. Steam's popular-upcoming list (including a big-publisher
  shooter that must never be posted), a coming-soon page, itch.io New & Popular, a few
  Reddit posts and a Bluesky post. The first roundup is due.
* 06:37 run 2 - "Gorilla Pizza Panic" takes off: its Reddit post jumps to 310 upvotes with
  wishlist + Roblox-joke comments, a Bluesky post links the Steam page, and it climbs
  Steam's popular list -> GEM ALARM.
* 07:07 run 3 - more upvotes, nothing new -> no double alarm, no roundup (not due).
* 08:07 run 4 - two people 👍 the alarm (weights learn), "Tiny Tavern Brawl" climbs to #1
  on itch.io and gets a Reddit post -> roundup.
* 08:37 run 5 - Steam answers 503 and itch.io shows a Cloudflare challenge: the run still
  completes (other sources keep working), nothing is posted.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import yaml

HERE = Path(__file__).resolve().parent
DAY = datetime(2026, 10, 3, tzinfo=UTC)
PDS = "https://amanita.us-east.host.bsky.network"


def at(hh: int, mm: int = 0) -> datetime:
    return DAY.replace(hour=hh, minute=mm)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def rfc1123(dt: datetime) -> str:
    return dt.strftime("%a, %d %b %Y %H:%M:%S GMT")


# --------------------------------------------------------------------------- Steam

STEAM_APPS: dict[int, dict[str, Any]] = {
    3141590: {
        "name": "Gorilla Pizza Panic",
        "dev": "Banana Bros",
        "desc": "Deliver pizzas as a team of gorillas with up to 4 friends. Proximity voice chat, ragdoll "
        "physics and total chaos.",
        "categories": [(2, "Single-player"), (1, "Multi-player"), (9, "Co-op"), (38, "Online Co-op")],
        "release": "Q1 2027",
        "tags": [492, 1685, 3843, 4136, 7178],
    },
    2999990: {
        "name": "Mega Corp Shooter 7",
        "dev": "Ubisoft Montreal",
        "publisher": "Ubisoft",
        "desc": "The next chapter of the blockbuster online shooter series.",
        "categories": [(1, "Multi-player"), (36, "Online PvP"), (38, "Online Co-op")],
        "release": "Nov 20, 2026",
        "tags": [1685, 3843, 3859],
    },
    3333330: {
        "name": "Lantern Keepers",
        "dev": "Driftwood Games",
        "desc": "A 2-4 player co-op horror game about keeping a haunted lighthouse lit through one long night.",
        "categories": [(1, "Multi-player"), (9, "Co-op"), (38, "Online Co-op")],
        "release": "Coming soon",
        "tags": [492, 1685, 3843, 1667],
    },
    3456780: {
        "name": "Orbital Janitors",
        "dev": "Mopworks",
        "desc": "Clean up a space station with your friends before the boss notices. Zero-g physics.",
        "categories": [(1, "Multi-player"), (9, "Co-op"), (38, "Online Co-op")],
        "release": "2027",
        "tags": [492, 1685, 3843, 3968],
    },
    3567800: {
        "name": "Haunted Shift",
        "dev": "Tiny Ghost Studio",
        "desc": "A 1-4 player online co-op horror game. Clock in, fix the haunted night shift, try not to scream.",
        "categories": [(2, "Single-player"), (1, "Multi-player"), (38, "Online Co-op")],
        "release": "Dec 2026",
        "tags": [492, 3843, 1667],
    },
    3600010: {
        "name": "Fishmonger Tycoon",
        "dev": "Saltwater Interactive",
        "desc": "Run the busiest fish market in town. Relaxing management sim.",
        "categories": [(2, "Single-player")],
        "release": "Q2 2027",
        "tags": [492, 1685],
    },
}


def steam_row(appid: int) -> str:
    app = STEAM_APPS[appid]
    slug = app["name"].replace(" ", "_")
    tags = ",".join(str(t) for t in app["tags"])
    img = f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{appid}/capsule_sm_120.jpg?t=1759000000"
    return (
        f'<a href="https://store.steampowered.com/app/{appid}/{slug}/?snr=1_7_7_150_1" data-ds-appid="{appid}" '
        f'data-ds-itemkey="App_{appid}" data-ds-tagids="[{tags}]" class="search_result_row ds_collapse_flag " '
        f'data-search-page="1" data-gpnav="item"><div class="col search_capsule"><img src="{img}"></div>'
        f'<div class="responsive_search_name_combined"><div class="col search_name ellipsis">'
        f'<span class="title">{app["name"]}</span></div><div class="col search_released responsive_secondrow">'
        f'{app["release"]}</div></div><div style="clear: left;"></div></a>'
    )


def steam_search(appids: list[int], total: int | None = None) -> dict[str, Any]:
    html = "<!-- List Items -->\r\n" + "\r\n".join(steam_row(a) for a in appids)
    return {"success": 1, "results_html": html, "total_count": str(total or len(appids)), "start": 0}


def appdetails(appid: int) -> dict[str, Any]:
    app = STEAM_APPS[appid]
    release = app["release"]
    exact = release[:3].isalpha() and "," in release
    return {
        str(appid): {
            "success": True,
            "data": {
                "type": "game",
                "name": app["name"],
                "steam_appid": appid,
                "required_age": 0,
                "is_free": False,
                "short_description": app["desc"],
                "header_image": f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{appid}/header.jpg?t=1759000000",
                "developers": [app["dev"]],
                "publishers": [app.get("publisher", app["dev"])],
                "categories": [{"id": cid, "description": d} for cid, d in app["categories"]],
                "genres": [{"id": "23", "description": "Indie"}, {"id": "1", "description": "Action"}],
                "release_date": {"coming_soon": not exact or True, "date": release},
            },
        }
    }


FEATURED = {
    "coming_soon": {"id": "cat_comingsoon", "name": "Coming Soon", "items": []},
    "new_releases": {"id": "cat_newreleases", "name": "New Releases", "items": []},
    "status": 1,
}

# --------------------------------------------------------------------------- itch.io

ITCH_GAMES: dict[str, dict[str, str]] = {
    "sockworks/sock-puppet-rampage": {
        "name": "Sock Puppet Rampage",
        "genre": "Action",
        "price": "$3.99",
        "desc": "A chaotic physics party game for up to 4 friends: puppets, ragdolls and couch co-op.",
        "created": "Thu, 01 Oct 2026 16:00:00 GMT",
    },
    "tavernfolk/tiny-tavern-brawl": {
        "name": "Tiny Tavern Brawl",
        "genre": "Action",
        "price": "$0.00",
        "desc": "Online co-op tavern brawler with proximity chat. Throw chairs at your friends.",
        "created": "Fri, 02 Oct 2026 11:00:00 GMT",
    },
    "pixelpond/quiet-tiles": {
        "name": "Quiet Tiles",
        "genre": "Puzzle",
        "price": "$2.00",
        "desc": "A calm tile puzzle game.",
        "created": "Wed, 30 Sep 2026 10:00:00 GMT",
    },
    "frogjam/swamp-golf": {
        "name": "Swamp Golf",
        "genre": "Sports",
        "price": "$0.00",
        "desc": "Golf, but in a swamp.",
        "created": "Tue, 29 Sep 2026 10:00:00 GMT",
    },
}


def itch_item(key: str) -> str:
    game = ITCH_GAMES[key]
    dev, slug = key.split("/")
    url = f"https://{dev}.itch.io/{slug}"
    img = f"https://img.itch.zone/aW1n{slug[:6]}/315x250%23c/{slug[:6]}.png"
    label = "Free" if game["price"] == "$0.00" else game["price"]
    return (
        f"<item><guid>{url}</guid><title>{game['name']} [{label}] [{game['genre']}]</title>"
        f"<plainTitle>{game['name']}</plainTitle><imageurl>{img}</imageurl><price>{game['price']}</price>"
        f"<currency>USD</currency><link>{url}</link><description><![CDATA[{game['desc']}\n"
        f'<img src="{img}" alt="{game["name"]}"/>]]></description><pubDate>{game["created"]}</pubDate>'
        f"<createDate>{game['created']}</createDate><updateDate>{game['created']}</updateDate>"
        f"<platforms><windows>yes</windows></platforms></item>"
    )


def itch_feed(title: str, keys: list[str]) -> str:
    items = "\n".join(itch_item(k) for k in keys)
    return (
        '<?xml version="1.0" encoding="UTF-8" ?><rss version="2.0"><channel>'
        f"<title>{escape(title)}</title><link>https://itch.io/games</link>\n{items}\n</channel></rss>\n"
    )


CLOUDFLARE = (
    "<!DOCTYPE html><html><head><title>Just a moment...</title></head><body>"
    '<script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script></body></html>'
)

# --------------------------------------------------------------------------- Reddit


def reddit_post(
    pid: str,
    sub: str,
    title: str,
    author: str,
    created: datetime,
    score: int,
    comments: int,
    *,
    selftext: str = "",
    url: str | None = None,
    subscribers: int = 250_000,
) -> dict[str, Any]:
    permalink = f"/r/{sub}/comments/{pid}/{title.lower().replace(' ', '_')[:40]}/"
    return {
        "kind": "t3",
        "data": {
            "id": pid,
            "name": f"t3_{pid}",
            "title": title,
            "selftext": selftext,
            "author": author,
            "subreddit": sub,
            "subreddit_name_prefixed": f"r/{sub}",
            "subreddit_subscribers": subscribers,
            "created_utc": created.timestamp(),
            "score": score,
            "ups": score,
            "num_comments": comments,
            "upvote_ratio": 0.95,
            "url": url or f"https://www.reddit.com{permalink}",
            "permalink": permalink,
            "link_flair_text": None,
            "thumbnail": "self" if not url else "default",
            "is_self": url is None,
            "over_18": False,
            "stickied": False,
            "domain": f"self.{sub}" if url is None else "store.steampowered.com",
            "is_video": False,
        },
    }


def listing(posts: list[dict[str, Any]]) -> dict[str, Any]:
    return {"kind": "Listing", "data": {"after": None, "dist": len(posts), "before": None, "children": posts}}


def comments_payload(pid: str, texts: list[tuple[str, str]], at_time: datetime) -> list[dict[str, Any]]:
    children = [
        {
            "kind": "t1",
            "data": {
                "id": f"c{pid}{i}",
                "name": f"t1_c{pid}{i}",
                "author": author,
                "body": body,
                "score": 5,
                "created_utc": at_time.timestamp(),
                "distinguished": None,
                "stickied": False,
                "is_submitter": False,
            },
        }
        for i, (author, body) in enumerate(texts)
    ]
    return [listing([{"kind": "t3", "data": {"id": pid}}]), listing(children)]


def gorilla_post(score: int, comments: int) -> dict[str, Any]:
    return reddit_post(
        "gpp001",
        "IndieDev",
        "My co-op game Gorilla Pizza Panic just got its Steam page - proximity chat chaos for 4 players!",
        "bananabros_dev",
        at(5, 0),
        score,
        comments,
        selftext="Up to 4 gorillas, ragdoll physics, one pizza. Wishlist: "
        "https://store.steampowered.com/app/3141590/Gorilla_Pizza_Panic/",
        subscribers=263_114,
    )


LANTERN = reddit_post(
    "lkp001",
    "CoOpGaming",
    "Lantern Keepers is a 2-4 player co-op horror game about keeping a lighthouse lit",
    "driftwood_games",
    at(1, 0),
    60,
    20,
    selftext="We're a two-person studio, first trailer coming soon. Would love feedback!",
    subscribers=120_000,
)
QUIET = reddit_post(
    "qtl001",
    "gamedev",
    "Made a small puzzle game called Quiet Tiles",
    "pixelpond",
    at(4, 0),
    3,
    1,
    subscribers=1_900_000,
)
HELP = reddit_post(
    "hlp001",
    "godot",
    "how do i stop my CharacterBody3D jittering on slopes?",
    "quietgecko",
    at(5, 30),
    2,
    4,
    subscribers=252_040,
)


def tavern_post(score: int, comments: int) -> dict[str, Any]:
    return reddit_post(
        "ttb001",
        "playmygame",
        "Tiny Tavern Brawl - our free online co-op brawler with proximity chat is on itch!",
        "tavernfolk",
        at(7, 20),
        score,
        comments,
        url="https://tavernfolk.itch.io/tiny-tavern-brawl",
        subscribers=105_000,
    )


GORILLA_COMMENTS_SMALL = [
    ("mossyknight", "Wishlisted! this looks so fun"),
    ("pixel_pal", "me and the boys need this"),
    ("brickfan99", "this is just a roblox game lol"),
    ("dev_dad", "how did you do the ragdolls?"),
    ("nyx", "when does it come out?"),
    ("crumbs", "cool art"),
]
GORILLA_COMMENTS_BIG = [
    *GORILLA_COMMENTS_SMALL,
    ("gamer_greg", "take my money"),
    ("ola", "Wishlisted, my friends would love this"),
    ("blocky", "looks like a roblox game but I'd play it"),
    ("tom_b", "roblox clone? still want it"),
    ("zed", "this is a roblox game and I love it"),
    ("rob", "fortnite creative map energy"),
    ("kiki", "is there a playtest?"),
    ("lu", "day one buy for me"),
    ("vee", "need this with the squad"),
    ("max", "the gorilla walk cycle is perfect"),
    ("jo", "roblox vibes but in a good way"),
    ("ash", "wishlisted!!"),
    ("ivy", "sign me up"),
    ("ken", "nice"),
    ("mia", "lol the pizza physics"),
    ("noor", "can't wait"),
]
LANTERN_COMMENTS = [
    ("salty_sea", "Wishlisted, love the vibe"),
    ("captain_k", "me and my friends would play this"),
    ("moth", "the lighthouse idea is great"),
    ("rain", "is it on steam yet?"),
    ("gull", "asset flip?"),
    ("fern", "can't wait"),
    ("pine", "looks nice"),
    ("brine", "need this"),
]
QUIET_COMMENTS = [("tilefan", "relaxing, nice colors")]
TAVERN_COMMENTS = [
    ("barfly", "played the demo with friends, so chaotic"),
    ("mug", "take my money"),
    ("stool", "the chair throwing is perfect"),
    ("hops", "me and the boys are hooked"),
    ("brew", "wishlisted"),
]

# --------------------------------------------------------------------------- Bluesky


def facet(text: str, needle: str, feature: dict[str, Any]) -> dict[str, Any]:
    start = text.encode().index(needle.encode())
    return {"index": {"byteStart": start, "byteEnd": start + len(needle.encode())}, "features": [feature]}


def bsky_post(
    rkey: str,
    did: str,
    handle: str,
    text: str,
    created: datetime,
    likes: int,
    replies: int,
    reposts: int,
    *,
    link: str | None = None,
    link_text: str | None = None,
    tags: tuple[str, ...] = (),
) -> dict[str, Any]:
    facets = []
    if link and link_text:
        facets.append(facet(text, link_text, {"$type": "app.bsky.richtext.facet#link", "uri": link}))
    for tag in tags:
        facets.append(facet(text, f"#{tag}", {"$type": "app.bsky.richtext.facet#tag", "tag": tag}))
    post: dict[str, Any] = {
        "uri": f"at://{did}/app.bsky.feed.post/{rkey}",
        "cid": f"bafyrei{rkey}",
        "author": {"did": did, "handle": handle, "displayName": handle.split(".")[0], "labels": []},
        "record": {
            "$type": "app.bsky.feed.post",
            "text": text,
            "createdAt": iso(created),
            "langs": ["en"],
            "facets": facets,
        },
        "replyCount": replies,
        "repostCount": reposts,
        "likeCount": likes,
        "quoteCount": 0,
        "indexedAt": iso(created),
        "labels": [],
    }
    if link:
        post["embed"] = {
            "$type": "app.bsky.embed.external#view",
            "external": {
                "uri": link,
                "title": "",
                "description": "",
                "thumb": f"https://cdn.bsky.app/img/feed_thumbnail/plain/{did}/{rkey}@jpeg",
            },
        }
    return post


SOCK_DID = "did:plc:sockworks7q2mvb5h4ejz6qp"
GORILLA_DID = "did:plc:bananabros4xq2m7kz3d5vbn"
SOCK_TEXT = "Sock Puppet Rampage is up on itch: a chaotic physics party game for 4 friends sockworks.itch.io/sock-puppet-rampage #indiedev"
GORILLA_TEXT = (
    "Gorilla Pizza Panic has a Steam page! Proximity chat, ragdolls, 4 gorillas, 1 pizza "
    "store.steampowered.com/app/3141590 #screenshotsaturday"
)


def sock(likes: int) -> dict[str, Any]:
    return bsky_post(
        "3msock1",
        SOCK_DID,
        "sockworks.bsky.social",
        SOCK_TEXT,
        at(5, 30),
        likes,
        4,
        6,
        link="https://sockworks.itch.io/sock-puppet-rampage",
        link_text="sockworks.itch.io/sock-puppet-rampage",
        tags=("indiedev",),
    )


def gorilla_bsky(likes: int, replies: int, reposts: int) -> dict[str, Any]:
    return bsky_post(
        "3mgpp01",
        GORILLA_DID,
        "bananabros.bsky.social",
        GORILLA_TEXT,
        at(6, 10),
        likes,
        replies,
        reposts,
        link="https://store.steampowered.com/app/3141590/Gorilla_Pizza_Panic/",
        link_text="store.steampowered.com/app/3141590",
        tags=("screenshotsaturday",),
    )


def thread(post: dict[str, Any], replies: list[tuple[str, str]]) -> dict[str, Any]:
    return {
        "thread": {
            "$type": "app.bsky.feed.defs#threadViewPost",
            "post": post,
            "replies": [
                {
                    "$type": "app.bsky.feed.defs#threadViewPost",
                    "post": {
                        "uri": f"at://did:plc:fan{i:04d}/app.bsky.feed.post/r{i}",
                        "cid": f"bafyreireply{i}",
                        "author": {
                            "did": f"did:plc:fan{i:04d}",
                            "handle": f"{name}.bsky.social",
                            "labels": [],
                        },
                        "record": {
                            "$type": "app.bsky.feed.post",
                            "text": text,
                            "createdAt": post["indexedAt"],
                        },
                        "likeCount": 1,
                        "replyCount": 0,
                        "repostCount": 0,
                        "indexedAt": post["indexedAt"],
                    },
                    "replies": [],
                }
                for i, (name, text) in enumerate(replies)
            ],
        }
    }


def profile(did: str, handle: str, followers: int) -> dict[str, Any]:
    return {"did": did, "handle": handle, "followersCount": followers, "followsCount": 100, "postsCount": 300}


SESSION = {
    "did": "did:plc:gembotscenario000000000",
    "didDoc": {
        "id": "did:plc:gembotscenario000000000",
        "service": [{"id": "#atproto_pds", "type": "AtprotoPersonalDataServer", "serviceEndpoint": PDS}],
    },
    "handle": "gembot-scenario.bsky.social",
    "accessJwt": "eyJhbGciOiJFUzI1NksifQ.SCENARIO-ACCESS.sig",
    "refreshJwt": "eyJhbGciOiJFUzI1NksifQ.SCENARIO-REFRESH.sig",
    "active": True,
}
REFRESHED = {
    **SESSION,
    "accessJwt": "eyJhbGciOiJFUzI1NksifQ.SCENARIO-ACCESS-2.sig",
    "refreshJwt": "eyJhbGciOiJFUzI1NksifQ.SCENARIO-REFRESH-2.sig",
}

# --------------------------------------------------------------------------- runs


def run_files(
    *,
    popular: list[int],
    comingsoon: list[int],
    itch_popular: list[str],
    itch_newest: list[str],
    reddit_new: list[dict[str, Any]],
    reddit_rising: list[dict[str, Any]],
    reddit_comments: dict[str, list[tuple[str, str]]],
    bsky_posts: list[dict[str, Any]],
    bsky_threads: dict[str, list[tuple[str, str]]],
    when: datetime,
    steam_down: bool = False,
    itch_challenge: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    routes: list[dict[str, Any]] = []
    files: dict[str, str] = {}

    def add(route: dict[str, Any], name: str | None = None, body: Any = None) -> None:
        if name is not None:
            files[name] = body if isinstance(body, str) else json.dumps(body, indent=1, ensure_ascii=False)
            route["file"] = name
        routes.append(route)

    # Steam
    if steam_down:
        routes.append(
            {"regex": r"^https://store\.steampowered\.com/", "status": 503, "body": "Service Unavailable"}
        )
    else:
        add(
            {
                "url": "https://store.steampowered.com/search/results/",
                "params": {"filter": "popularcomingsoon"},
            },
            "steam_popular.json",
            steam_search(popular, 120),
        )
        add(
            {"url": "https://store.steampowered.com/search/results/", "params": {"filter": "comingsoon"}},
            "steam_comingsoon.json",
            steam_search(comingsoon, len(comingsoon)),
        )
        add({"url": "https://store.steampowered.com/api/featuredcategories"}, "steam_featured.json", FEATURED)
        for appid in sorted(STEAM_APPS):
            add(
                {"url": "https://store.steampowered.com/api/appdetails", "params": {"appids": appid}},
                f"steam_app_{appid}.json",
                appdetails(appid),
            )
    # Reddit (OAuth with fake credentials from the manifest)
    add(
        {"url": "https://www.reddit.com/api/v1/access_token", "method": "POST"},
        "reddit_token.json",
        {"access_token": "scenario-token", "token_type": "bearer", "expires_in": 3600, "scope": "*"},
    )
    add({"regex": r"^https://oauth\.reddit\.com/r/[^/]+/new"}, "reddit_new.json", listing(reddit_new))
    add(
        {"regex": r"^https://oauth\.reddit\.com/r/[^/]+/rising"}, "reddit_rising.json", listing(reddit_rising)
    )
    for pid, texts in sorted(reddit_comments.items()):
        add(
            {"url": f"https://oauth.reddit.com/comments/{pid}"},
            f"reddit_comments_{pid}.json",
            comments_payload(pid, texts, when - timedelta(minutes=10)),
        )
    # itch.io
    if itch_challenge:
        routes.append(
            {
                "regex": r"^https://itch\.io/",
                "status": 403,
                "body": CLOUDFLARE,
                "headers": {"content-type": "text/html", "cf-mitigated": "challenge"},
            }
        )
    else:
        add(
            {"url": "https://itch.io/games/new-and-popular.xml"},
            "itch_new_and_popular.xml",
            itch_feed("New & popular games - itch.io", itch_popular),
        )
        add(
            {"url": "https://itch.io/games/newest.xml"},
            "itch_newest.xml",
            itch_feed("Newest games - itch.io", itch_newest),
        )
        add(
            {"regex": r"^https://itch\.io/games/new-and-popular/tag-"},
            "itch_tag.xml",
            itch_feed("Tagged games - itch.io", itch_popular[:2]),
        )
    # Bluesky (logged in with fake credentials from the manifest)
    add(
        {"url": "https://bsky.social/xrpc/com.atproto.server.createSession", "method": "POST"},
        "bsky_session.json",
        SESSION,
    )
    add(
        {"url": "https://bsky.social/xrpc/com.atproto.server.refreshSession", "method": "POST"},
        "bsky_refresh.json",
        REFRESHED,
    )
    add(
        {"url": f"{PDS}/xrpc/app.bsky.feed.searchPosts"},
        "bsky_search.json",
        {"posts": bsky_posts, "hitsTotal": len(bsky_posts)},
    )
    for post in bsky_posts:
        rkey = post["uri"].rsplit("/", 1)[-1]
        add(
            {
                "url": "https://public.api.bsky.app/xrpc/app.bsky.feed.getPostThread",
                "params": {"uri": post["uri"]},
            },
            f"bsky_thread_{rkey}.json",
            thread(post, bsky_threads.get(rkey, [])),
        )
    add(
        {"url": "https://public.api.bsky.app/xrpc/app.bsky.actor.getProfile", "params": {"actor": SOCK_DID}},
        "bsky_profile_sock.json",
        profile(SOCK_DID, "sockworks.bsky.social", 900),
    )
    add(
        {
            "url": "https://public.api.bsky.app/xrpc/app.bsky.actor.getProfile",
            "params": {"actor": GORILLA_DID},
        },
        "bsky_profile_gorilla.json",
        profile(GORILLA_DID, "bananabros.bsky.social", 1800),
    )
    return routes, files


GORILLA_REPLIES = [
    ("ada", "wishlisted!"),
    ("ben", "me and the boys need this"),
    ("cy", "looks like roblox lol"),
    ("dee", "take my money"),
    ("eli", "need this"),
]
SOCK_REPLIES = [("fox", "this looks hilarious"), ("gus", "wishlisted")]

RUNS: list[dict[str, Any]] = [
    dict(
        name="run1",
        at=at(6, 7),
        popular=[2999990, 3456780, 3567800, 3141590, 3333330],
        comingsoon=[3141590, 3333330, 3456780, 3567800, 3600010],
        itch_popular=[
            "frogjam/swamp-golf",
            "sockworks/sock-puppet-rampage",
            "pixelpond/quiet-tiles",
            "tavernfolk/tiny-tavern-brawl",
        ],
        itch_newest=["tavernfolk/tiny-tavern-brawl", "sockworks/sock-puppet-rampage"],
        reddit_new=[gorilla_post(40, 6), LANTERN, QUIET, HELP],
        reddit_rising=[LANTERN],
        reddit_comments={
            "gpp001": GORILLA_COMMENTS_SMALL,
            "lkp001": LANTERN_COMMENTS,
            "qtl001": QUIET_COMMENTS,
        },
        bsky_posts=[sock(25)],
        bsky_threads={"3msock1": SOCK_REPLIES},
    ),
    dict(
        name="run2",
        at=at(6, 37),
        popular=[2999990, 3141590, 3456780, 3567800, 3333330],
        comingsoon=[3141590, 3333330, 3456780, 3567800, 3600010],
        itch_popular=[
            "sockworks/sock-puppet-rampage",
            "frogjam/swamp-golf",
            "tavernfolk/tiny-tavern-brawl",
            "pixelpond/quiet-tiles",
        ],
        itch_newest=["tavernfolk/tiny-tavern-brawl", "sockworks/sock-puppet-rampage"],
        reddit_new=[gorilla_post(310, 64), LANTERN, QUIET, HELP],
        reddit_rising=[gorilla_post(310, 64), LANTERN],
        reddit_comments={
            "gpp001": GORILLA_COMMENTS_BIG,
            "lkp001": LANTERN_COMMENTS,
            "qtl001": QUIET_COMMENTS,
        },
        bsky_posts=[sock(31), gorilla_bsky(80, 10, 12)],
        bsky_threads={"3msock1": SOCK_REPLIES, "3mgpp01": GORILLA_REPLIES},
    ),
    dict(
        name="run3",
        at=at(7, 7),
        popular=[2999990, 3141590, 3456780, 3333330, 3567800],
        comingsoon=[3141590, 3333330, 3456780, 3567800, 3600010],
        itch_popular=[
            "sockworks/sock-puppet-rampage",
            "tavernfolk/tiny-tavern-brawl",
            "frogjam/swamp-golf",
            "pixelpond/quiet-tiles",
        ],
        itch_newest=["tavernfolk/tiny-tavern-brawl", "sockworks/sock-puppet-rampage"],
        reddit_new=[gorilla_post(420, 81), LANTERN, QUIET, HELP],
        reddit_rising=[gorilla_post(420, 81)],
        reddit_comments={
            "gpp001": GORILLA_COMMENTS_BIG,
            "lkp001": LANTERN_COMMENTS,
            "qtl001": QUIET_COMMENTS,
        },
        bsky_posts=[sock(33), gorilla_bsky(120, 14, 20)],
        bsky_threads={"3msock1": SOCK_REPLIES, "3mgpp01": GORILLA_REPLIES},
    ),
    dict(
        name="run4",
        at=at(8, 7),
        reactions=[{"kind": "alarm", "game": "steam:3141590", "emoji": "👍", "users": 2}],
        popular=[2999990, 3141590, 3333330, 3456780, 3567800],
        comingsoon=[3141590, 3333330, 3456780, 3567800, 3600010],
        itch_popular=[
            "tavernfolk/tiny-tavern-brawl",
            "sockworks/sock-puppet-rampage",
            "frogjam/swamp-golf",
            "pixelpond/quiet-tiles",
        ],
        itch_newest=["tavernfolk/tiny-tavern-brawl", "sockworks/sock-puppet-rampage"],
        reddit_new=[tavern_post(48, 11), gorilla_post(510, 95), LANTERN, QUIET, HELP],
        reddit_rising=[tavern_post(48, 11)],
        reddit_comments={
            "gpp001": GORILLA_COMMENTS_BIG,
            "lkp001": LANTERN_COMMENTS,
            "qtl001": QUIET_COMMENTS,
            "ttb001": TAVERN_COMMENTS,
        },
        bsky_posts=[sock(34), gorilla_bsky(150, 16, 24)],
        bsky_threads={"3msock1": SOCK_REPLIES, "3mgpp01": GORILLA_REPLIES},
    ),
    dict(
        name="run5",
        at=at(8, 37),
        steam_down=True,
        itch_challenge=True,
        popular=[],
        comingsoon=[],
        itch_popular=[],
        itch_newest=[],
        reddit_new=[tavern_post(61, 14), gorilla_post(560, 101), LANTERN, QUIET, HELP],
        reddit_rising=[tavern_post(61, 14)],
        reddit_comments={
            "gpp001": GORILLA_COMMENTS_BIG,
            "lkp001": LANTERN_COMMENTS,
            "qtl001": QUIET_COMMENTS,
            "ttb001": TAVERN_COMMENTS,
        },
        bsky_posts=[sock(34), gorilla_bsky(160, 16, 25)],
        bsky_threads={"3msock1": SOCK_REPLIES, "3mgpp01": GORILLA_REPLIES},
    ),
]


def main() -> None:
    manifest: dict[str, Any] = {
        "start": "2026-10-03T06:00:00Z",
        "config_dir": "../config",  # the pinned test config, not the user's config/ folder
        # Fake credentials so every source takes part. Never put real secrets here.
        "env": {
            "REDDIT_CLIENT_ID": "scenario-client",
            "REDDIT_CLIENT_SECRET": "scenario-secret",
            "BLUESKY_HANDLE": "gembot-scenario.bsky.social",
            "BLUESKY_APP_PASSWORD": "scen-ario0-pass-word",
        },
        "runs": [],
    }
    for spec in RUNS:
        spec = dict(spec)
        name, when = spec.pop("name"), spec.pop("at")
        reactions = spec.pop("reactions", None)
        routes, files = run_files(when=when, **spec)
        folder = HERE / name
        folder.mkdir(exist_ok=True)
        for old in folder.iterdir():
            old.unlink()
        for filename, body in files.items():
            (folder / filename).write_text(body + ("" if body.endswith("\n") else "\n"), encoding="utf-8")
        (folder / "routes.yaml").write_text(
            yaml.safe_dump(routes, sort_keys=False, allow_unicode=True), "utf-8"
        )
        entry: dict[str, Any] = {"at": when.strftime("%Y-%m-%dT%H:%M:%SZ"), "responses": name}
        if reactions:
            entry["reactions"] = reactions
        manifest["runs"].append(entry)
    (HERE / "manifest.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True), "utf-8"
    )
    print(f"wrote {len(RUNS)} runs to {HERE}")


if __name__ == "__main__":
    main()
