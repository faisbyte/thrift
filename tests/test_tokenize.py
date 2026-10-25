import sqlite3

import pytest

from thriftctx.tokenize import (
    fts_match_expr,
    identifier_terms,
    parse_query,
    path_terms,
    split_identifier,
    text_terms,
)


@pytest.mark.parametrize(
    "ident, parts",
    [
        ("getUserById", ["get", "user", "by", "id"]),
        ("HTTPServer", ["http", "server"]),
        ("parseHTTPResponse", ["parse", "http", "response"]),
        ("snake_case2", ["snake", "case", "2"]),
        ("__init__", ["init"]),
        ("SCREAMING_SNAKE", ["screaming", "snake"]),
        ("x", ["x"]),
    ],
)
def test_split_identifier(ident, parts):
    assert split_identifier(ident) == parts


def test_identifier_terms_keep_whole_and_parts():
    assert identifier_terms("getUserById") == ["getuserbyid", "get", "user", "by", "id"]
    assert identifier_terms("split_cells") == ["split_cells", "split", "cells"]
    assert identifier_terms("plain") == ["plain"]


def test_text_and_path_terms():
    assert text_terms("def fooBar(x): return x") == "def foobar foo bar x return x"
    assert path_terms("src/http_client/RetryPolicy.ts") == (
        "src http_client http client retrypolicy retry policy ts"
    )


def test_parse_query_prioritises_identifiers_and_drops_stopwords():
    q = parse_query("please fix the bug where getUserById returns the wrong user")
    # whole identifier first, then its parts ("get"/"by" are stopwords)
    assert q.terms[:3] == ["getuserbyid", "user", "id"]
    assert "the" not in q.terms and "please" not in q.terms and "fix" not in q.terms
    assert "getuserbyid" in q.identifiers
    assert "bug" in q.words


def test_parse_query_dotted_and_backticked_identifiers():
    q = parse_query("look at Console.export_html and `render`")
    assert "console.export_html" in q.identifiers
    assert "export_html" in q.identifiers
    assert "render" in q.identifiers


def test_test_words_are_modifiers_not_terms():
    q = parse_query("run the tests again")
    assert q.terms == []
    assert q.mentions_tests
    q = parse_query("add a test for split_cells")
    assert q.mentions_tests and "split_cells" in q.terms and "test" not in q.terms


def test_parse_query_caps_terms():
    q = parse_query(" ".join(f"word{i}x" for i in range(40)))
    assert len(q.terms) == 32  # the ranker later keeps the 12 most selective


@pytest.mark.parametrize(
    "hostile",
    ['foo" OR "bar', "a - b", "col:value", "NEAR(a b)", "x*", "^start", "AND OR NOT", "(((", "'quote'"],
)
def test_fts_expression_is_always_valid(hostile):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE VIRTUAL TABLE fts USING fts5(symbol_terms, body_terms, tokenize=\"unicode61 tokenchars '_'\", prefix='3')")
    conn.execute("INSERT INTO fts VALUES ('near foo', 'bar and or not start value col')")
    q = parse_query(hostile + " something_real")
    expr = fts_match_expr(q)
    conn.execute("SELECT rowid FROM fts WHERE fts MATCH ?", (expr,)).fetchall()  # must not raise


def test_fts_prefix_only_on_symbol_column_for_long_terms():
    expr = fts_match_expr(parse_query("ab parse"))
    assert expr == '"ab" OR "parse" OR symbol_terms : "parse" *'
