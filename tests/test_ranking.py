"""Search ranking on a small synthetic corpus with known answers."""

from pathlib import Path

import pytest

from conftest import write
from thriftctx import store
from thriftctx.indexer import index_project
from thriftctx.rank import BM25Ranker, Hit, diversify, is_test_path

CORPUS = {
    "src/auth/session.py": '''
class SessionStore:
    """Keeps user sessions."""

    def refresh_token(self, token):
        """Refresh an expired session token."""
        if token.expired:
            return self._renew(token)
        return token

    def _renew(self, token):
        return Token(token.user)
''',
    "src/auth/login.py": '''
def login_handler(request):
    """Handle login form posts and start a session."""
    user = authenticate(request.form)
    return start_session(user)
''',
    "src/http/retry.py": '''
def exponential_backoff(attempt, base=0.5):
    """Delay before the next retry attempt."""
    return base * (2 ** attempt)


class RetryPolicy:
    def should_retry(self, response):
        return response.status in (502, 503)
''',
    "src/notes.py": '''
# This module mentions session session session session session a lot,
# session session session, but is not about refreshing anything.
NOTES = "session " * 20
''',
    "tests/test_session.py": '''
def test_refresh_token():
    store = SessionStore()
    assert store.refresh_token(tok) is tok
''',
    "src/big.py": "\n".join(
        [f"def helper_{i}(session):\n    return session.refresh_token({i})\n" for i in range(6)]
    ),
}


@pytest.fixture
def ranker(tmp_path: Path):
    for rel, text in CORPUS.items():
        write(tmp_path, rel, text.lstrip("\n"))
    index_project(tmp_path)
    conn = store.open_readonly(tmp_path)
    yield BM25Ranker(conn)
    conn.close()


def labels(hits):
    return [h.qualname or h.path for h in hits]


def test_exact_symbol_beats_body_mentions(ranker):
    hits = ranker.rank("refresh_token", 5)
    assert hits[0].qualname == "SessionStore.refresh_token"
    assert hits[0].exact


def test_qualified_name_query(ranker):
    assert ranker.rank("SessionStore.refresh_token", 3)[0].qualname == "SessionStore.refresh_token"


def test_natural_language_query(ranker):
    assert ranker.rank("how is the delay between retry attempts computed", 3)[0].qualname == "exponential_backoff"
    assert ranker.rank("where do we handle the login form", 3)[0].qualname == "login_handler"


def test_coverage_beats_repeating_one_term(ranker):
    hits = ranker.rank("refresh expired session token", 5)
    assert hits[0].qualname == "SessionStore.refresh_token"
    assert "src/notes.py" not in [h.path for h in hits[:2]]


def test_test_files_demoted_unless_asked(ranker):
    hits = ranker.rank("refresh_token", 5)
    assert hits[0].path == "src/auth/session.py"
    test_rank = [h.path for h in hits].index("tests/test_session.py")
    assert test_rank > 0
    hits = ranker.rank("test refresh_token", 5)
    assert "tests/test_session.py" in [h.path for h in hits[:2]]


def test_max_two_chunks_per_file(ranker):
    hits = ranker.rank("session refresh_token helper", 10)
    from collections import Counter

    assert max(Counter(h.path for h in hits).values()) <= 2


def test_path_terms_help(ranker):
    hits = ranker.rank("http retry policy", 3)
    assert hits[0].path == "src/http/retry.py"


def test_long_prompts_keep_rare_terms(ranker):
    # >12 terms: common/unknown words are dropped before the ones that discriminate
    prompt = ("basically the customer dashboard widget sidebar shows another weird spinner later after "
              "refreshing the browser window tab, and the exponential backoff delay seems off")
    from thriftctx.tokenize import parse_query

    q = parse_query(prompt)
    assert len(q.terms) > 12
    sel = ranker.select_terms(q, ranker.doc_freqs(q.terms))
    assert len(sel.terms) == 12 and {"exponential", "backoff", "delay"} <= set(sel.terms)
    assert ranker.rank(prompt, 3)[0].qualname == "exponential_backoff"


def test_no_results_for_unknown_terms(ranker):
    assert ranker.rank("kubernetes helm chart", 5) == []


def test_empty_query(ranker):
    assert ranker.rank("the and of", 5) == []


def _hit(path, start, end, nlines=None, qual=None, kind="function", score=1.0):
    return Hit(0, path, start, end, kind, qual, qual, None, nlines or end - start + 1, score)


def test_diversify_drops_overlaps_and_split_duplicates():
    hits = [
        _hit("a.py", 1, 50, qual="A"),
        _hit("a.py", 10, 40, qual="A.m"),  # inside A's carried lines -> dropped
        _hit("b.py", 1, 100, qual="big", kind="method"),
        _hit("b.py", 91, 150, qual="big", kind="method"),  # second window of same symbol
        _hit("c.py", 1, 5, qual="c"),
    ]
    assert [h.qualname for h in diversify(hits, 10, 2)] == ["A", "big", "c"]


def test_class_header_does_not_hide_methods():
    # header carries 5 lines although the class spans 1-100
    hits = [_hit("a.py", 1, 100, nlines=5, qual="A", kind="class"), _hit("a.py", 20, 30, qual="A.m")]
    assert len(diversify(hits, 10, 2)) == 2


@pytest.mark.parametrize(
    "path, is_test",
    [("tests/test_x.py", True), ("pkg/test_x.py", True), ("a/b_test.go", True), ("src/x.spec.ts", True),
     ("src/FooTest.java", True), ("src/__tests__/a.js", True), ("src/testing_utils.py", False),
     ("src/contest.py", False)],
)
def test_is_test_path(path, is_test):
    assert is_test_path(path) is is_test
