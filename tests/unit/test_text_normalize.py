"""Unicode-aware text normalization shared by Fact-Checker + Author validator."""
from __future__ import annotations

from account_research.utils.text_normalize import normalize_for_match, strip_html


def test_nfkd_strips_diacritics():
    assert normalize_for_match("República Dominicana") == "republica dominicana"
    assert normalize_for_match("José Pérez") == "jose perez"
    assert normalize_for_match("naïve") == "naive"


def test_smart_quotes_map_to_ascii():
    assert normalize_for_match("“Hello”") == '"hello"'
    assert normalize_for_match("don’t") == "don't"
    assert normalize_for_match("«ciao»") == '"ciao"'


def test_dash_map_to_hyphen():
    # em-dash, en-dash, hyphen, minus
    src = "a—b–c‐d−e"
    assert normalize_for_match(src) == "a-b-c-d-e"


def test_ellipsis_unicode():
    assert normalize_for_match("loading… done") == "loading... done"


def test_whitespace_collapse_includes_nbsp():
    src = "hello   \tworld"
    assert normalize_for_match(src) == "hello world"


def test_lowercase():
    assert normalize_for_match("MERCADO LIBRE") == "mercado libre"


def test_empty_input():
    assert normalize_for_match("") == ""
    assert normalize_for_match(None) == ""  # type: ignore[arg-type]


def test_strip_html_removes_tags_and_unescapes_entities():
    src = "<p>Founded in <b>2020</b>&nbsp;in DR &amp; ROW.</p>"
    out = strip_html(src)
    assert "<" not in out and ">" not in out
    assert "&amp;" not in out
    assert "&" in out  # entity decoded


def test_combined_pipeline_matches_typical_drift():
    quote = '"the strategy was clear - expand into Republica Dominicana"'
    page = "<p>The strategy was clear — expand into República Dominicana</p>"
    nquote = normalize_for_match(quote)
    npage = normalize_for_match(strip_html(page))
    # The normalized quote (minus the wrapping quotes) should be substring of the page
    assert "the strategy was clear - expand into republica dominicana" in npage
    # And nquote includes the same text (with surrounding ASCII quotes)
    assert nquote.strip('"') in npage
