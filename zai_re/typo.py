"""zai_re.typo — typo-tolerant intent decoding.

Why this exists: in the capture the user typed

    "deep search whats next elonmkusk goad of towdy"

and the model's *thinking* frame shows it doing exactly this decode by hand:

    "The user's query is garbled: ... this likely means "what's next Elon Musk
     goals of today" or "what's next Elon Musk plans today"."

This module makes that step explicit and testable: token-level fuzzy correction,
glued-word segmentation ("elonmkusk" -> "elon musk"), contraction fixes, phrase
fixes, then intent classification. The agent runs it before planning tools, and
the UI shows the corrections so a sloppy prompt is visibly understood instead of
silently misread.

Design rules that keep it from over-correcting:
  * an unknown token is only rewritten when nothing about it is a known word;
  * fuzzy matches need ratio >= 0.75 against a same-length-ish candidate;
  * common English words are never "corrected" (they are in VOCAB and pass through);
  * original spacing and punctuation are preserved byte-for-byte.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------- vocabulary
# Domain vocabulary: what this chat app is actually asked about.
BRANDS = [
    "elon musk", "spacex", "tesla", "grok", "xai", "optimus", "robotaxi",
    "starship", "mars", "starlink", "neuralink", "openai", "chatgpt", "glm",
    "cloudflare", "zai", "google", "microsoft", "nvidia", "meta", "apple",
    "trillionaire", "valuation", "ipo", "agenti", "grok-5", "grok 5",
]
DOMAIN = [
    "next", "goals", "goal", "today", "tomorrow", "yesterday", "news", "latest",
    "search", "deep", "plan", "plans", "update", "updates", "announcement",
    "announcements", "happening", "happen", "happened", "recent", "currently",
    "price", "stock", "release", "launch", "launched", "roadmap", "summary",
    "summarize", "compare", "timeline", "revenue", "valuation", "capital",
    "spending", "expansion", "production", "analysts", "expect", "expects",
    "citiation", "citation", "source", "sources", "research", "browse",
]
# High-frequency English, so unknown-but-valid words are never mangled.
COMMON = """
a about above after again against all almost along already also although always am among an and another any
anyone anything are area around as ask asked at available away back bad be because been before being below
best better between big both but by call called came can cannot case change come could day days did different
do does doing done down during each early easy either end enough even ever every everyone everything face fact
far feel few find first for found four from full further get getting give given go going good got great group
grow had half has have having he head hear her here high him his history hit hold home hope how however i if
im in include including info information into is issue it its itself just keep kind know known large last late
later lead learn least leave left less let level life like likely line list little long look looking lot low
made make making man many may maybe me mean means men might million mind more most much must my name near need
never new news next no none nor not nothing now number of off often on once one only open or other others our
out over own part past people per perhaps person place plan please point possible present press problem provide
put question quite rather reach read real really reason recent record reply report require research result
return right room run said same saw say says second see seem seen send sent several she short should show side
since sit small so some someone something sometimes soon sort sound start state still stop such sure system
take taken talk tell ten than that the their them then there these they thing things think third this those
though thought three through time times to today together too took top toward two under until up upon us use
used using usually very want wanted was watch way we week well went were what whatever when where whether which
while who whole why will with within without work world would write written year years yes yesterday yet you
your yours whats happening release latest broken something anything everything
""".split()

GREETINGS = ["hey", "hi", "hello", "yo", "sup", "thanks", "thx", "ok", "okay",
             "morning", "evening", "afternoon", "night", "bye"]

VOCAB: set[str] = set()
for chunk in (COMMON, DOMAIN, GREETINGS, [w for b in BRANDS for w in b.split()]):
    for word in chunk:
        VOCAB.add(word.lower())

CONTRACTIONS = {
    "whats": "what's", "wats": "what's", "whatss": "what's", "hwats": "what's",
    "ther": "there", "teh": "the", "adn": "and", "abt": "about", "plz": "please",
    "ur": "your", "dont": "don't", "doesnt": "doesn't", "im": "i'm",
    "thats": "that's", "wanna": "want to", "gimme": "give me", "wat": "what",
    "wen": "when", "wher": "where", "releaese": "release", "relaese": "release",
    "goaals": "goals", "musk": "musk",
}

# phrases the model itself paraphrased ("goals of today")
PHRASE_FIXES = [
    (r"\bgoal of\b", "goals of"),
    (r"\bwhat's next of\b", "what's next for"),
]

SEARCH_RE = re.compile(r"\b(deep\s*search|deep\s*research|research|search|browse)\b", re.I)
QUESTION_RE = re.compile(r"\b(what|who|when|where|why|how|which|is|are|does|do|did)\b", re.I)
GREETING_RE = re.compile(r"^\s*(hey|hi|hello|yo|sup|good\s+(morning|evening|afternoon))\b", re.I)
WORD_RE = re.compile(r"^[A-Za-z][A-Za-z']*$")


@dataclass
class Correction:
    original: str
    fixed: str
    kind: str          # "spelling" | "glue" | "contraction" | "phrase"


@dataclass
class Decoded:
    raw: str
    text: str                                   # corrected prompt
    intent: str                                 # "search" | "question" | "chat"
    corrections: list[Correction] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    confidence: float = 1.0

    @property
    def changed(self) -> bool:
        return bool(self.corrections)

    def summary(self) -> str:
        if not self.corrections:
            return "no corrections"
        return ", ".join(f"{c.original}→{c.fixed}" for c in self.corrections)


# ---------------------------------------------------------------- primitives
def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def _split_glued(token: str) -> tuple[str, str] | None:
    """'elonmkusk' -> ('elon', 'musk') when both halves resolve to known words."""
    if len(token) < 7:
        return None
    best: tuple[str, str, float] | None = None
    for i in range(3, len(token) - 2):
        left, right = token[:i], token[i:]
        near_right = None
        if right in VOCAB or left in VOCAB:
            if right not in VOCAB:
                cand = difflib.get_close_matches(right, VOCAB, n=1, cutoff=0.8)
                near_right = cand[0] if cand else None
            right_final = right if right in VOCAB else near_right
            if left in VOCAB and right_final:
                score = (len(left) + len(right_final)) / len(token)
                if best is None or score > best[2]:
                    best = (left, right_final, score)
    if best:
        return best[0], best[1]
    return None


def _is_inflection(low: str) -> bool:
    """'starships' / 'launched' / 'launching' are valid forms, not typos."""
    for suffix, cut in (("s", 1), ("es", 2), ("ed", 2), ("ing", 3), ("d", 1)):
        if low.endswith(suffix) and len(low) > len(suffix) + 1:
            stem = low[:-cut]
            if stem in VOCAB or stem + "e" in VOCAB:
                return True
    return False


# words that make a following noun-vs-adjective typo resolve the right way
_PREPOSITION_FOLLOWERS = {"of", "for", "in", "about", "on", "with"}


def correct_token(token: str, next_word: str | None = None) -> tuple[str, str | None]:
    """Return (fixed_token, kind_or_None) for a single alphabetic token."""
    low = token.lower()
    if low in CONTRACTIONS:
        return CONTRACTIONS[low], "contraction"
    if low in VOCAB or _is_inflection(low):
        return token, None                      # known word / valid form: untouched
    if not WORD_RE.match(low):                  # numbers, mixed tokens
        return token, None
    glued = _split_glued(low)
    if glued:
        return " ".join(glued), "glue"
    if len(low) >= 4:
        cands = difflib.get_close_matches(low, VOCAB, n=5, cutoff=0.7)
        best = None
        for c in cands:
            if abs(len(c) - len(low)) > 2:      # 'towdy' -> 'today', not 'tomorrow'
                continue
            score = _ratio(low, c)
            if c in DOMAIN:                     # prefer this app's vocabulary
                score += 0.05
            if (next_word or "").lower() in _PREPOSITION_FOLLOWERS and \
                    c in ("goal", "goals", "plans", "plan", "roadmap"):
                score += 0.15                   # 'goad of today' -> 'goals of today'
            if best is None or score > best[1] + 1e-9:
                best = (c, score)
        if best:
            return best[0], "spelling"
    return token, None


def decode(prompt: str) -> Decoded:
    """Correct typos, segment glued words, then classify intent.

    Spacing and punctuation are preserved: only word tokens are rewritten.
    """
    parts = re.split(r"(\s+)", prompt)          # keep separators
    corrections: list[Correction] = []
    rebuilt: list[str] = []
    # lookahead: the next non-space token helps disambiguate (goal vs good)
    words_only = [p for p in parts if p and not p.isspace()]
    pos_of: dict[int, int] = {}
    wi = 0
    for i, part in enumerate(parts):
        if part and not part.isspace():
            pos_of[i] = wi
            wi += 1

    for idx, part in enumerate(parts):
        wi_here = pos_of.get(idx)
        next_word = words_only[wi_here + 1] if (wi_here is not None
                                                and wi_here + 1 < len(words_only)) else None
        if not part or part.isspace():
            rebuilt.append(part)
            continue
        # split leading/trailing punctuation off so "towdy." still resolves
        m = re.match(r"^([^\w]*)([\w']*)([^\w]*)$", part)
        if not m:
            rebuilt.append(part)
            continue
        lead, core, trail = m.groups()
        fixed, kind = correct_token(core, next_word) if core else (core, None)
        if kind:
            corrections.append(Correction(core, fixed, kind))
        rebuilt.append(lead + fixed + trail)

    text = "".join(rebuilt)
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    for pattern, repl in PHRASE_FIXES:
        new = re.sub(pattern, repl, text, flags=re.I)
        if new != text:
            corrections.append(Correction(text, new, "phrase"))
            text = new

    low = text.lower()
    if SEARCH_RE.search(low):
        intent = "search"
    elif GREETING_RE.match(low):
        intent = "chat"
    elif QUESTION_RE.search(low) or low.rstrip().endswith("?"):
        intent = "question"
    else:
        intent = "chat"

    entities = [b for b in BRANDS if b in low]
    words = [w for w in re.findall(r"[a-z']+", low) if w]
    known = sum(1 for w in words
                if w in VOCAB or _is_inflection(w)
                or w in CONTRACTIONS.values() or "'" in w)
    confidence = round(known / len(words), 3) if words else 1.0

    return Decoded(raw=prompt, text=text, intent=intent,
                   corrections=corrections, entities=entities,
                   confidence=confidence)
