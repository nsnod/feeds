"""Build the synthetic itch.io RSS fixtures in this directory.

Run from the repo root:  python tests/fixtures/itch/make_fixtures.py

The item layout copies the 2026 itch.io feed schema (12 children per item, in this order:
guid, title, plainTitle, imageurl, price, currency, link, description, pubDate,
createDate, updateDate, platforms). A few items break the schema on purpose (missing
plainTitle / imageurl / createDate, bad dates, a non-game link) so the parser's
fallbacks are exercised. Replace these files with real captures after the first smoke run
(see README.md); the tests only rely on the values written below.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
MISSING = object()  # leave the element out entirely


def rfc1123(value: datetime) -> str:
    return format_datetime(value.astimezone(UTC), usegmt=True)


def image(seed: str, ext: str = "png") -> str:
    return f"https://img.itch.zone/aW1nLz{seed}LnBuZw==/315x250%23c/{seed}.{ext}"


def item(
    *,
    link: str,
    title: str,
    plain: object,
    imageurl: object,
    price: str = "$0.00",
    currency: str = "USD",
    text: str,
    img: str | None,
    pub: str,
    create: object = None,
    update: str,
    platforms: str = "",
) -> str:
    """One <item>. ``text`` is already HTML-escaped (itch escapes inside the CDATA)."""
    parts = [f"<guid>{escape(link)}</guid>", f"<title>{escape(title)}</title>"]
    if plain is not MISSING:
        parts.append(f"<plainTitle>{escape(str(plain))}</plainTitle>")
    if imageurl is not MISSING:
        parts.append(f"<imageurl>{escape(str(imageurl))}</imageurl>")
    parts += [
        f"<price>{escape(price)}</price>",
        f"<currency>{currency}</currency>",
        f"<link>{escape(link)}</link>",
    ]
    alt = plain if isinstance(plain, str) else title.split(" [")[0]
    body = text if img is None else f'{text}\n<img src="{img}" alt="{escape(alt)}"/>'
    parts.append(f"<description><![CDATA[{body}]]></description>")
    parts.append(f"<pubDate>{pub}</pubDate>")
    if create is not MISSING:
        parts.append(f"<createDate>{pub if create is None else create}</createDate>")
    parts.append(f"<updateDate>{update}</updateDate>")
    parts.append(f"<platforms>{platforms}</platforms>" if platforms else "<platforms />")
    return "<item>" + "".join(parts) + "</item>"


def rss(title: str, link: str, items: list[str]) -> str:
    head = '<?xml version="1.0" encoding="UTF-8" ?><rss version="2.0"><channel>'
    head += f"<title>{escape(title)}</title><link>{link}</link>"
    return head + "\n" + "\n".join(items) + ("\n" if items else "") + "</channel></rss>\n"


GORILLA = item(
    link="https://bananabros.itch.io/gorilla-pizza-panic",
    title="Gorilla Pizza Panic [$4.99] [Action]",
    plain="Gorilla Pizza Panic",
    imageurl=image("GpZa01"),
    price="$4.99",
    text="Deliver pizzas as a gorilla with up to 4 friends &amp; proximity chat",
    img=image("GpZa01"),
    pub="Thu, 01 Oct 2026 18:12:00 GMT",
    update="Sat, 03 Oct 2026 09:01:44 GMT",
    platforms="<windows>yes</windows><linux>yes</linux>",
)
LUNCH = item(
    link="https://moonmilk.itch.io/lethal-lunch-shift",
    title="Lethal Lunch Shift [Free] [Simulation]",
    plain="Lethal Lunch Shift",
    imageurl=image("LuNch2"),
    text="Co-op cafeteria horror for 1&ndash;4 players. Don&#39;t let the soup win.",
    img=image("LuNch2"),
    pub="Fri, 02 Oct 2026 07:30:00 GMT",
    update="Fri, 02 Oct 2026 21:15:09 GMT",
    platforms="<windows>yes</windows><html>yes</html>",
)
SPREAD = item(
    link="https://spreadteam.itch.io/spread",
    title="[Spread] [Free] [Platformer]",
    plain="[Spread]",
    imageurl="https://img.itch.zone/aW1nLzEwMDAwMDAzLmdpZg==/original/Spr3ad.gif",
    text="A tiny platformer about spreading out",
    img="https://img.itch.zone/aW1nLzEwMDAwMDAzLmdpZg==/original/Spr3ad.gif",
    pub="Wed, 30 Sep 2026 12:00:00 GMT",
    update="Wed, 30 Sep 2026 12:00:00 GMT",
    platforms="<html>yes</html>",
)
RED_DOT = item(
    link="https://redcircle.itch.io/red-dot",
    title="🔴 [Free]",
    plain="🔴",
    imageurl=image("RdDot4"),
    text="it is a dot",
    img=image("RdDot4"),
    pub="Tue, 29 Sep 2026 23:59:59 GMT",
    update="Tue, 29 Sep 2026 23:59:59 GMT",
)
JAM = item(
    link="https://itch.io/jam/friendslop-jam-2026",
    title="Friendslop Jam 2026 [Free] [Other]",
    plain="Friendslop Jam 2026",
    imageurl=image("JaMm05"),
    text="Make a chaotic co-op game in 72 hours",
    img=None,
    pub="Mon, 28 Sep 2026 10:00:00 GMT",
    update="Mon, 28 Sep 2026 10:00:00 GMT",
)
CAVERN = item(
    link="https://Some_Dev.itch.io/Co-Op-Cavern",
    title="Co-op Cavern [3.39€] [Action]",
    plain=MISSING,
    imageurl=image("CaVe06"),
    price="3.39€",
    currency="EUR",
    text="Couch co-op cave crawler for 1&ndash;4 players",
    img=image("CaVe06"),
    pub="Sat, 03 Oct 2026 08:12:00 GMT",
    update="Sat, 03 Oct 2026 10:01:44 GMT",
    platforms="<windows>yes</windows><osx>yes</osx><linux>yes</linux>",
)
ROPE = item(
    link="https://ropeworks.itch.io/ragdoll-rope-bridge",
    title="Ragdoll Rope Bridge [$2.00] [Puzzle]",
    plain="Ragdoll Rope Bridge",
    imageurl=MISSING,
    price="$2.00",
    text="Physics-based <b>co-op</b> bridge building &amp; ragdoll chaos",
    img="https://img.itch.zone/aW1nLzEwMDAwMDA3LnBuZw==/315x250%23c/R0pe07.png",
    pub="Thu, 01 Oct 2026 03:04:05 GMT",
    create=MISSING,
    update="Thu, 01 Oct 2026 03:04:05 GMT",
    platforms="<windows>yes</windows>",
)
MOTEL = item(
    link="https://hauntco.itch.io/mimic-motel",
    title="Mimic Motel [$1.99] [Survival]",
    plain="Mimic Motel",
    imageurl=image("MoTel8"),
    price="$1.99",
    text="Check in with friends. Not everyone checks out.",
    img=image("MoTel8"),
    pub="sometime last week",
    create="",
    update="not a date",
    platforms="<windows>yes</windows><android></android>",
)
KART = item(
    link="https://tinycrew.itch.io/couch-kart-chaos",
    title="Couch Kart Chaos [$0.99] [Racing]",
    plain="Couch Kart Chaos",
    imageurl=image("KaRt09"),
    price="$0.99",
    text="Split-screen kart brawler",
    img=image("KaRt09"),
    pub="Fri, 02 Oct 2026 16:00:00 GMT",
    update="Fri, 02 Oct 2026 16:00:00 GMT",
    platforms="<windows>yes</windows>",
)

GENRES = ["Action", "Adventure", "Puzzle", "Platformer", "Simulation", "Other"]
NEWEST_START = datetime(2026, 10, 3, 11, 59, tzinfo=UTC)


def newest_item(i: int) -> str:
    when = rfc1123(NEWEST_START - timedelta(minutes=7 * (i - 1)))
    free = i % 3 == 0
    return item(
        link=f"https://newdev{i:02d}.itch.io/game-{i:02d}",
        title=f"Game {i:02d} [{'Free' if free else '$1.00'}] [{GENRES[i % len(GENRES)]}]",
        plain=f"Game {i:02d}",
        imageurl=image(f"NeW{i:03d}"),
        price="$0.00" if free else "$1.00",
        text=f"Fresh upload number {i}",
        img=image(f"NeW{i:03d}"),
        pub=when,
        update=when,
        platforms="<html>yes</html>",
    )


def build() -> dict[str, str]:
    newest = "https://itch.io/games/newest"
    return {
        "new_and_popular.xml": rss(
            "New & popular games - itch.io",
            "https://itch.io/games/new-and-popular",
            [GORILLA, LUNCH, SPREAD, RED_DOT, JAM, CAVERN, ROPE, MOTEL],
        ),
        "tag_coop.xml": rss(
            "New & popular games tagged Co-op - itch.io",
            "https://itch.io/games/new-and-popular/tag-co-op",
            [LUNCH, GORILLA, CAVERN, KART, newest_item(3)],
        ),
        "newest_page1.xml": rss("Latest games - itch.io", newest, [newest_item(i) for i in range(1, 37)]),
        "newest_page2.xml": rss("Latest games - itch.io", newest, [newest_item(i) for i in range(37, 73)]),
        "newest_page3.xml": rss("Latest games - itch.io", newest, [newest_item(i) for i in range(73, 83)]),
        "empty_channel.xml": '<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>\n',
        "malformed.xml": rss(
            "New & popular games - itch.io", "https://itch.io/games/new-and-popular", [GORILLA]
        )[:420],
    }


def main() -> None:
    for name, content in build().items():
        (HERE / name).write_text(content, encoding="utf-8")
        print(f"wrote {name} ({len(content.encode())} bytes)")


if __name__ == "__main__":
    main()
