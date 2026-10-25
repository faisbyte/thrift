"""Identifier-aware tokenization shared by indexing and querying.

STDLIB ONLY: the UserPromptSubmit hook imports this with no third-party deps.

Text is turned into a space-separated stream of lowercase terms. Every
identifier contributes its whole lowercased form plus its parts, so
``getUserById`` yields ``getuserbyid get user by id`` and ``HTTP_SERVER``
yields ``http_server http server``. The FTS5 table is created with
``tokenchars '_'`` so whole snake_case identifiers survive as single tokens.
"""

from __future__ import annotations

import re

TOKENIZER_VERSION = "1"

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# camelCase / PascalCase / ACRONYMWord / digits boundaries
_PART_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")

STOPWORDS = frozenset(
    """
    a an and are as at be been but by can could did do does doing done for from
    had has have having he her here hers him his how i if in into is it its just
    me more most my no nor not of off on once only or other our out over own same
    she should so some such than that the their them then there these they this
    those through to too under until up very was we were what when where which
    while who whom why will with would you your yours also any all each few both
    get got let make made use used using via per like want need please thanks
    find show tell explain look see check work works working change changes
    fix fixes fixed add adds added update updates implement implemented support
    code file files function functions method methods class classes thing things
    way new old call calls called return returns value values data
    thanks thank ok okay yes yeah sure great good nice cool perfect awesome
    looks seems sounds right wrong now again still continue proceed go ahead
    done try maybe actually really something anything everything run rerun
    whether decide decides decided decision decisions makes many much name named
    involved involve point responsible logic implemented implementation defined
    located location place part happen happens kind sort mean means understand
    self cls this def fn func var let const none null nil true false
    import imports from package module int str string bool void
    """.split()
)

# Modifiers, not search terms: they only lift the test-file demotion.
TEST_WORDS = frozenset({"test", "tests", "testing", "spec", "specs", "pytest", "unittest"})

MAX_QUERY_TERMS = 12  # terms actually searched (most selective first)
MAX_PARSED_TERMS = 32  # candidates kept from the prompt before selection
PREFIX_MIN_LEN = 4


def split_identifier(ident: str) -> list[str]:
    """Split an identifier into lowercase parts (no empty strings)."""
    parts: list[str] = []
    for piece in ident.split("_"):
        if piece:
            parts.extend(p.lower() for p in _PART_RE.findall(piece))
    return parts


def identifier_terms(ident: str) -> list[str]:
    """Whole lowercased identifier (if compound) followed by its parts."""
    whole = ident.lower()
    parts = split_identifier(ident)
    if len(parts) == 1 and parts[0] == whole:
        return [whole]
    out = [whole] if whole.strip("_") else []
    out.extend(p for p in parts if len(p) > 1 or p.isdigit())
    return out


def text_terms(text: str) -> str:
    """Convert arbitrary text/code into the indexed term stream."""
    out: list[str] = []
    for m in _IDENT_RE.finditer(text):
        out.extend(identifier_terms(m.group(0)))
    return " ".join(out)


def path_terms(path: str) -> str:
    """Terms for a repo-relative path: each component and its parts."""
    out: list[str] = []
    for comp in re.split(r"[/\\.\-]+", path):
        if comp:
            out.extend(identifier_terms(comp))
    return " ".join(out)


def is_identifier_like(token: str) -> bool:
    """True for tokens that look like code identifiers rather than English."""
    if "_" in token.strip("_") or any(c.isdigit() for c in token):
        return True
    # camelCase / PascalCase with an inner capital
    return bool(re.search(r"[a-z][A-Z]", token)) or (
        token[:1].isupper() and len(split_identifier(token)) > 1
    )


class Query:
    """A parsed search query.

    ``terms``: ordered, de-duplicated search terms (lowercase).
    ``identifiers``: lowercased tokens that look like code (camelCase,
    snake_case, dotted ``cls.method``, `backticked`) -> strong exact-symbol
    signal. ``words``: every other lowercased token -> weak signal.
    """

    __slots__ = ("terms", "identifiers", "words", "ident_like_count", "mentions_tests")

    def __init__(self, terms, identifiers, words, ident_like_count, mentions_tests):
        self.terms: list[str] = terms
        self.identifiers: set[str] = identifiers
        self.words: set[str] = words
        self.ident_like_count: int = ident_like_count
        self.mentions_tests: bool = mentions_tests

    def __bool__(self) -> bool:
        return bool(self.terms)


def parse_query(text: str, max_terms: int = MAX_PARSED_TERMS) -> Query:
    identifiers: set[str] = set()
    words: set[str] = set()
    for m in re.finditer(r"[A-Za-z_][A-Za-z0-9_]*(?:(?:\.|::)[A-Za-z_][A-Za-z0-9_]*)+", text):
        dotted = m.group(0).lower().replace("::", ".")
        identifiers.add(dotted)
        identifiers.add(dotted.rsplit(".", 1)[1])
    for m in re.finditer(r"`([^`\s]{2,80})`", text):
        identifiers.update(t.lower() for t in _IDENT_RE.findall(m.group(1)))

    ident_first: list[str] = []
    plain: list[str] = []
    ident_like = 0
    for m in _IDENT_RE.finditer(text):
        tok = m.group(0)
        low = tok.lower()
        if is_identifier_like(tok) or low in identifiers:
            ident_like += 1
            identifiers.add(low)
            ident_first.extend(identifier_terms(tok))
        else:
            if low not in STOPWORDS:
                words.add(low)
            plain.extend(identifier_terms(tok))

    seen: set[str] = set()
    terms: list[str] = []
    for t in ident_first + plain:
        seen_before = t in seen
        seen.add(t)
        if seen_before or t in STOPWORDS or t in TEST_WORDS or len(t) < 2:
            continue
        terms.append(t)
        if len(terms) >= max_terms:
            break
    mentions_tests = any(t in TEST_WORDS for t in seen)
    return Query(terms, identifiers, words, ident_like, mentions_tests)


def variants(term: str) -> tuple[str, ...]:
    """Cheap English inflection variants for query-side matching (no index
    stemming, so identifiers stay exact): attempts->attempt, retries->retry,
    caching->cach/cache, computed->compute/comput."""
    out: list[str] = []
    if len(term) > 4 and not term.isdigit():
        if term.endswith("ies"):
            out.append(term[:-3] + "y")
        elif term.endswith(("sses", "shes", "ches", "xes")):
            out.append(term[:-2])
        elif term.endswith("s") and not term.endswith("ss"):
            out.append(term[:-1])
        elif term.endswith("ing") and len(term) > 5:
            out.extend((term[:-3], term[:-3] + "e"))
        elif term.endswith("ed") and len(term) > 5:
            out.extend((term[:-1], term[:-2]))
        elif term.endswith("tion") and len(term) > 6:
            out.extend((term[:-3] + "e", term[:-3]))  # distribution -> distribute
        elif term.endswith("er") and len(term) > 5:
            out.extend((term[:-2], term[:-1]))  # parser -> pars/parse
    return tuple(v for v in out if len(v) >= 3)


def _quote(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def fts_match_expr(query: Query) -> str:
    """Build a safe FTS5 MATCH expression (OR of quoted terms).

    Every term is quoted, so FTS operators in user text (AND/OR/NOT/NEAR,
    ``-``, ``:``, ``^``, ``*``) are inert. Longer terms also get a prefix
    match restricted to the symbol column.
    """
    parts: list[str] = []
    for t in query.terms:
        parts.append(_quote(t))
        parts.extend(_quote(v) for v in variants(t))
        if len(t) >= PREFIX_MIN_LEN:
            parts.append("symbol_terms : " + _quote(t) + " *")
    return " OR ".join(parts)
