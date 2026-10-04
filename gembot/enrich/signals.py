"""Comment signals: hype (intent), negativity and "Roblox clone" discourse.

Input is a sample of comments/replies for one post (see ``enrich/comments.py``); output is
a :class:`~gembot.models.CommentSignals` with raw counts. The scoring layer turns those
into the ``hype`` and ``meme`` features and the negativity penalty (BUILD_SPEC 4.2).

Rules:

* The post author's own replies, bots (``is_bot``, AutoModerator, names ending in "bot")
  and empty / ``[deleted]`` / ``[removed]`` comments are ignored.
* Counts the spec asks for per *person* are per distinct commenter
  (``intent_commenters``, ``negative_commenters``, ``roblox_commenters``); raw comment
  counts are kept too. A comment without an author counts as its own anonymous commenter.
* A phrase preceded by a negation in the same clause ("not a scam", "doesn't look like
  roblox at all", "I would never play this") does not count; "but" / "though" / "yet" /
  "however" start a new clause ("No offense but this is an asset flip" counts). "Not gonna
  lie" is not a negation. Intent phrases asked about someone else ("Who would buy this",
  "get people wishlisting") and "day one refund" are not intent. Any other comment that mentions Roblox / Fortnite Creative / UEFN counts as
  Roblox discourse, including "I make roblox games for a living": the meme feature
  measures attention, so being generous here is harmless.
* ``merge_signals`` adds counts from several posts. Commenters are only distinct per
  post (different platforms have different user bases), so the merged
  ``distinct_commenters`` is a sum.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

from gembot.models import Comment, CommentSignals

MAX_EXAMPLES = 3
MAX_TERMS = 8
EXAMPLE_CHARS = 100

_I = re.IGNORECASE

INTENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("wishlisted", re.compile(r"\bwish[\s-]?list(?:ed|ing)\b", _I)),
    (
        "wishlisted",
        re.compile(
            r"\b(?:will|gonna|going\s+to|must|instantly|immediately|definitely|def)\s+wish[\s-]?list\b", _I
        ),
    ),
    (
        "wishlisted",
        re.compile(r"\badded\s+(?:it\s+|this\s+|that\s+)?to\s+(?:my\s+|the\s+)?wish[\s-]?list", _I),
    ),
    ("wishlisted", re.compile(r"\bon\s+my\s+wish[\s-]?list\b", _I)),
    ("take my money", re.compile(r"\btake\s+(?:all\s+)?my\s+(?:money|cash|wallet)\b", _I)),
    ("need this", re.compile(r"\bneed\s+(?:this|it|that)\b", _I)),
    ("want this", re.compile(r"\bwant\s+(?:this|it|to\s+play\s+(?:this|it))\b", _I)),
    (
        "day one",
        re.compile(
            r"\bday\s*(?:one|1)\b(?!\s+(?:refund\w*|patch\w*|bugs?|crash\w*|dlc|update|player\s+count))"
            r"(?:\s+(?:buy|purchase|pickup|pick\s+up))?",
            _I,
        ),
    ),
    (
        "when does it come out",
        re.compile(
            r"\bwhen\s+(?:does|will|is|can\s+(?:i|we))\s+(?:it|this|the\s+game|this\s+game)?\s*"
            r"(?:come\s+out|coming\s+out|out\b|release|launch|be\s+(?:out|released|available)|"
            r"(?:buy|play|get)\s+(?:it|this))",
            _I,
        ),
    ),
    ("when does it come out", re.compile(r"\bwhen(?:'|’)?s\s+(?:it|this)\s+(?:coming\s+)?out\b", _I)),
    (
        "release date?",
        re.compile(
            r"\b(?:any|what(?:'|’)?s\s+the|what\s+is\s+the)\s+release\s+date\b|\brelease\s+date\s*\?", _I
        ),
    ),
    (
        "is there a demo",
        re.compile(
            r"\b(?:is\s+there|any)\s+(?:a\s+)?(?:demo|playtest|beta)\b|\bwhere(?:'|’)?s\s+the\s+demo\b|\bdemo\s+when\b",
            _I,
        ),
    ),
    (
        "playtest?",
        re.compile(
            r"\b(?:can\s+i|could\s+i|how\s+(?:do|can)\s+i|i(?:'|’)?d\s+(?:love|like)\s+to|i\s+would\s+(?:love|like)\s+to|"
            r"let\s+me|sign\s+me\s+up\s+for(?:\s+the)?|want\s+to|wanna)\s+(?:join\s+(?:the\s+)?|be\s+(?:a\s+|in\s+the\s+)?)?"
            r"playtest(?:ing|er)?\b|\bplaytest\s*\?",
            _I,
        ),
    ),
    (
        "me and the boys",
        re.compile(r"\bme\s+(?:and|&|n)\s+the\s+(?:boys|bois|lads|squad|crew|gang|homies)\b", _I),
    ),
    (
        "my friends would love this",
        re.compile(
            r"\b(?:my\s+(?:friends?|buddies|mates|squad|group|crew|boys|homies|gf|girlfriend|bf|boyfriend|wife|"
            r"husband|partner)|the\s+(?:boys|bois|lads|squad|crew|gang|homies))\s+(?:(?:is|are)\s+)?"
            r"(?:would|will|going\s+to|gonna)\s+(?:love|enjoy|like)\b",
            _I,
        ),
    ),
    ("can't wait", re.compile(r"\bcan(?:'|’)?t\s+wait\b|\bcannot\s+wait\b", _I)),
    (
        "instant buy",
        re.compile(r"\binsta(?:nt)?[\s-]?(?:buy|purchase|wishlist)\b|\binstabuy\b|\bauto[\s-]?buy\b", _I),
    ),
    ("sign me up", re.compile(r"\bsign\s+me\s+up\b", _I)),
    (
        "where can i get it",
        re.compile(r"\bwhere\s+(?:can|do)\s+(?:i|we)\s+(?:buy|get|play|download|find|wishlist)\b", _I),
    ),
    (
        "steam link?",
        re.compile(
            r"\b(?:steam|store)\s+(?:link|page)\s*\?|\blink\s+to\s+(?:the\s+)?(?:steam|store)\b|"
            r"\bis\s+(?:it|this)\s+on\s+steam\b",
            _I,
        ),
    ),
    (
        "would play",
        re.compile(
            r"\bwould\s+(?:definitely\s+|totally\s+|100%\s+|absolutely\s+|so\s+)?(?:play|buy)\b|"
            r"\bi(?:'|’)?d\s+(?:definitely\s+|totally\s+|absolutely\s+|so\s+)?(?:play|buy)\b",
            _I,
        ),
    ),
    (
        "gonna buy",
        re.compile(
            r"\b(?:gonna|going\s+to|will\s+(?:definitely\s+)?|def(?:initely)?)\s+(?:buy|grab|cop)\b", _I
        ),
    ),
    (
        "play with friends",
        re.compile(
            r"\bneed\s+to\s+play\s+(?:this|it)\b|\b(?:play|playing)\s+(?:this|it)\s+with\s+(?:my|the)\s+"
            r"(?:friends|boys|buddies|mates|squad|crew|gf|girlfriend|bf|boyfriend|wife|husband|partner)\b",
            _I,
        ),
    ),
)

NEGATIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("asset flip", re.compile(r"\basset[\s-]?flip(?:s|ped|per|pers)?\b", _I)),
    ("scam", re.compile(r"\bscam(?:s|my|mer|mers)?\b", _I)),
    (
        "ai slop",
        re.compile(
            r"\bai[\s-]?(?:slop|generated|gen|made|art|assets|garbage)\b|\bmade\s+(?:with|by|using)\s+(?:an?\s+)?ai\b|"
            r"\bgenerative\s+ai\b",
            _I,
        ),
    ),
    ("stolen", re.compile(r"\bstolen\b|\bripped[\s-]?off\b|\brip[\s-]?off\b|\bplagiar\w*", _I)),
    ("abandoned", re.compile(r"\babandon(?:ed|ware)\b|\bdead\s+game\b|\bgame\s+is\s+dead\b", _I)),
    ("cash grab", re.compile(r"\bcash[\s-]?grab\b", _I)),
    (
        "unity asset store",
        re.compile(r"\bunity\s+asset\s+store\b|\basset\s+store\s+(?:assets|game|models|stuff|junk)\b", _I),
    ),
    ("low effort", re.compile(r"\blow[\s-]effort\b", _I)),
)

ROBLOX_PATTERN = re.compile(r"\broblox|\bfortnite\s+(?:creative|map)\b|\buefn\b", _I)

# A negation earlier in the same clause cancels a match ("not a scam", "doesn't look like roblox").
# A contrast word starts a new clause: "No offense but this is an asset flip" still counts.
_NEGATION = re.compile(
    r"(?:\b(?:not|never|no|nobody|nothing|hardly|dont|doesnt|isnt|wont|wouldnt|didnt|aint|cant)\b|\w+n['’]t\b)"
    r"[^.!?,;]{0,30}$",
    _I,
)
_CLAUSE_BREAK = re.compile(r"[.!?,;]|\b(?:but|though|tho|although|yet|however)\b", _I)
# Intent that is really sarcasm ("Who would buy this") or about other people ("get people wishlisting").
_NOT_THE_COMMENTER = re.compile(
    r"(?:\bwho(?:\s+(?:tf|the\s+hell|even|actually|honestly|tf\s+even))?|"
    r"\b(?:people|players|users|folks|them|others|everyone|anyone|viewers|followers))\s+$",
    _I,
)
_NOT_GONNA_LIE = re.compile(r"\bnot\s+(?:gonna|going\s+to)\s+lie\b", _I)
_BOT_NAMES = frozenset(
    {
        "automoderator",
        "[bot]",
        "bot",
        "remindmebot",
        "sneakpeekbot",
        "repostsleuthbot",
        "savevideobot",
        "savevideo",
        "vredditdownloader",
        "stabbot",
        "gifreversingbot",
        "haikusbot",
        "wikisummarizerbot",
        "auddbot",
        "uwutranslator",
    }
)
_CAMEL_BOT = re.compile(r"[a-z0-9]Bot$")  # RemindMeBot, SaveVideoBot (but not WorkingRobot / TheAbbot)
_DELETED = frozenset({"", "[deleted]", "[removed]", "[deleted by user]"})


def _prepare(text: str) -> str:
    return _NOT_GONNA_LIE.sub("ngl", text or "")


def _negated(text: str, start: int) -> bool:
    clause = _CLAUSE_BREAK.split(text[max(0, start - 40) : start])[-1]
    return bool(_NEGATION.search(clause))


def _not_the_commenter(text: str, start: int) -> bool:
    return bool(_NOT_THE_COMMENTER.search(text[max(0, start - 30) : start]))


def _hits(text: str, patterns: Iterable[tuple[str, re.Pattern[str]]], *, intent: bool = False) -> list[str]:
    text = _prepare(text)
    found: list[str] = []
    for label, pattern in patterns:
        if label in found:
            continue
        for match in pattern.finditer(text):
            if _negated(text, match.start()) or (intent and _not_the_commenter(text, match.start())):
                continue
            found.append(label)
            break
    return found


def intent_hits(text: str) -> list[str]:
    """Labels of "I want this game" phrases in one comment (wishlisted, take my money, ...)."""
    return _hits(text, INTENT_PATTERNS, intent=True)


def negative_hits(text: str) -> list[str]:
    """Labels of negative phrases in one comment (asset flip, scam, ai slop, ...)."""
    return _hits(text, NEGATIVE_PATTERNS)


def is_roblox_joke(text: str) -> bool:
    """True when a comment brings up Roblox / Fortnite Creative / UEFN (and does not deny it)."""
    text = _prepare(text)
    return any(not _negated(text, m.start()) for m in ROBLOX_PATTERN.finditer(text))


def _author_key(author: str | None) -> str:
    name = (author or "").strip().lower()
    for prefix in ("/u/", "u/", "@"):
        name = name.removeprefix(prefix)
    return name


def is_bot_name(author: str | None) -> bool:
    """Known bot accounts, ``*[bot]`` / ``*_bot`` / ``*-bot`` names and CamelCase ``...Bot`` names."""
    name = _author_key(author)
    if not name:
        return False
    if name in _BOT_NAMES or name.endswith(("[bot]", "_bot", "-bot", ".bot")):
        return True
    original = (author or "").strip().removeprefix("/u/").removeprefix("u/").removeprefix("@")
    return bool(_CAMEL_BOT.search(original))


def _snippet(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= EXAMPLE_CHARS else text[: EXAMPLE_CHARS - 1].rstrip() + "…"


def analyze_comments(
    comments: list[Comment],
    *,
    post_author: str | None = None,
    post_comment_count: int = 0,
    platform: str | None = None,
) -> CommentSignals:
    """Count intent / negativity / Roblox discourse in a sample of comments for one post.

    ``platform`` (the mention's source) keeps "alex" on Reddit and "alex" elsewhere apart
    when a game's posts are merged.
    """
    owner = _author_key(post_author)
    sampled = 0
    anonymous = 0
    commenters: set[str] = set()
    intent_people: set[str] = set()
    negative_people: set[str] = set()
    roblox_people: set[str] = set()
    intent_comments = 0
    roblox_comments = 0
    intent_examples: list[tuple[str, str]] = []
    roblox_examples: list[tuple[str, str]] = []
    terms: list[str] = []
    for comment in comments:
        text = (comment.text or "").strip()
        if text.lower() in _DELETED or comment.is_bot or is_bot_name(comment.author):
            continue
        who = _author_key(comment.author)
        if owner and who == owner:
            continue
        if who in _DELETED:
            anonymous += 1
            who = f"#anonymous-{anonymous}"
        sampled += 1
        commenters.add(who)
        if intent_hits(text):
            intent_comments += 1
            intent_people.add(who)
            _add_example(intent_examples, who, text)
        negatives = negative_hits(text)
        if negatives:
            negative_people.add(who)
            terms.extend(t for t in negatives if t not in terms)
        if is_roblox_joke(text):
            roblox_comments += 1
            roblox_people.add(who)
            _add_example(roblox_examples, who, text)
    return CommentSignals(
        sampled=sampled,
        distinct_commenters=len(commenters),
        intent_comments=intent_comments,
        intent_commenters=len(intent_people),
        negative_commenters=len(negative_people),
        roblox_comments=roblox_comments,
        roblox_commenters=len(roblox_people),
        post_comment_count=max(int(post_comment_count or 0), 0),
        intent_examples=[text for _, text in intent_examples],
        negative_terms=terms[:MAX_TERMS],
        roblox_examples=[text for _, text in roblox_examples],
        commenter_ids=_person_ids(commenters, platform),
        intent_ids=_person_ids(intent_people, platform),
        negative_ids=_person_ids(negative_people, platform),
        roblox_ids=_person_ids(roblox_people, platform),
    )


MAX_PERSON_IDS = 200  # per post and category; keeps the stored signals small


def _person_ids(people: set[str], platform: str | None) -> list[str]:
    """Short, stable hashes of commenter names (anonymous ones can't be matched across posts)."""
    ids = {
        hashlib.sha1(f"{platform or ''}:{who}".encode()).hexdigest()[:10]
        for who in people
        if not who.startswith("#anonymous-")
    }
    return sorted(ids)[:MAX_PERSON_IDS]


def _distinct(items: list[CommentSignals], count: str, ids: str) -> tuple[int, list[str]]:
    """Sum of the per-post counts minus the people seen under more than one post."""
    total = sum(getattr(i, count) for i in items)
    listed = [x for i in items for x in getattr(i, ids)]
    union = sorted(set(listed))
    return max(total - (len(listed) - len(union)), 0), union


def _add_example(examples: list[tuple[str, str]], who: str, text: str) -> None:
    """Keep up to three short examples, one per commenter."""
    if len(examples) < MAX_EXAMPLES and all(author != who for author, _ in examples):
        examples.append((who, _snippet(text)))


def _merge_lists(lists: Iterable[list[str]], limit: int) -> list[str]:
    out: list[str] = []
    for items in lists:
        for item in items:
            if item not in out and len(out) < limit:
                out.append(item)
    return out


def merge_signals(items: list[CommentSignals]) -> CommentSignals:
    """Add up the signals of several posts about one game.

    Comment counts are summed; people are counted once even when they commented under
    several of the game's posts (matched by ``*_ids``).
    """
    items = [item for item in items if item is not None]
    if not items:
        return CommentSignals()
    commenters, commenter_ids = _distinct(items, "distinct_commenters", "commenter_ids")
    intent, intent_ids = _distinct(items, "intent_commenters", "intent_ids")
    negative, negative_ids = _distinct(items, "negative_commenters", "negative_ids")
    roblox, roblox_ids = _distinct(items, "roblox_commenters", "roblox_ids")
    return CommentSignals(
        sampled=sum(i.sampled for i in items),
        distinct_commenters=commenters,
        intent_comments=sum(i.intent_comments for i in items),
        intent_commenters=intent,
        negative_commenters=negative,
        roblox_comments=sum(i.roblox_comments for i in items),
        roblox_commenters=roblox,
        post_comment_count=sum(i.post_comment_count for i in items),
        intent_examples=_merge_lists((i.intent_examples for i in items), MAX_EXAMPLES),
        negative_terms=_merge_lists((i.negative_terms for i in items), MAX_TERMS),
        roblox_examples=_merge_lists((i.roblox_examples for i in items), MAX_EXAMPLES),
        commenter_ids=commenter_ids,
        intent_ids=intent_ids,
        negative_ids=negative_ids,
        roblox_ids=roblox_ids,
    )
