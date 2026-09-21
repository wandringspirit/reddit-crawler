import pytest

from reddit_crawler.keywords import KeywordError, KeywordSet, parse_lines, phrase_to_regex, validate_keyword_set


def make(include, require=(), exclude=(), **options):
    return KeywordSet.from_dict({"name": "t", "include_any": list(include), "require_any": list(require),
                                 "exclude": list(exclude), **options}).compile()


def test_phrase_is_case_insensitive_and_tolerates_hyphens_and_spacing():
    matcher = make(["web developer"])
    assert matcher.match(text="Looking for a WEB-DEVELOPER asap").matched
    assert matcher.match(text="web    developer wanted").matched
    assert matcher.match(text="web\ndeveloper wanted").matched
    assert not matcher.match(text="webdeveloper wanted").matched  # no separator at all


def test_whole_word_boundaries():
    matcher = make(["site"])
    assert matcher.match(text="my site is down").matched
    assert not matcher.match(text="website is down").matched
    assert not matcher.match(text="the situation").matched
    loose = make(["site"], whole_word=False)
    assert loose.match(text="website is down").matched


def test_plural_tolerance_only_on_last_word():
    matcher = make(["hire a developer"])
    assert matcher.match(text="I want to hire a developers").matched
    assert matcher.match(text="hire a developer").matched
    strict = make(["hire a developer"], plural_tolerant=False)
    assert not strict.match(text="hire a developers").matched


def test_wildcard_suffix():
    matcher = make(["recommend*"])
    for text in ("recommend", "recommends", "recommendation please", "Recommended"):
        assert matcher.match(text=text).matched, text
    assert not matcher.match(text="commend").matched


def test_regex_terms_and_invalid_regex():
    matcher = make([r"/how much .{0,20} website/"])
    assert matcher.match(text="How much would a website cost?").matched
    assert not matcher.match(text="how much is a car").matched
    with pytest.raises(KeywordError):
        make(["/(unclosed/"])


def test_field_prefixes():
    matcher = make(["title: need a website", "text: budget"])
    assert matcher.match(title="Need a website", text="").matched
    assert not matcher.match(title="", text="need a website").matched
    assert matcher.match(title="", text="my budget is 500").matched
    assert not matcher.match(title="budget", text="").matched


def test_exclude_wins_and_reports_reason():
    matcher = make(["need a website"], exclude=["title: [for hire]", "my portfolio"])
    result = matcher.match(title="[FOR HIRE] I build websites", text="need a website? hire me")
    assert not result.matched
    assert result.exclude_hits and result.exclude_hits[0].term == "title: [for hire]"
    assert matcher.match(title="Need a website", text="check my portfolio").matched is False
    assert matcher.match(title="Need a website", text="for my bakery").matched


def test_require_any_gate():
    matcher = make(["looking for a developer"], require=["website", "landing page"])
    assert not matcher.match(text="looking for a developer for my game").matched
    result = matcher.match(text="looking for a developer for my landing page")
    assert result.matched
    assert "landing page" in result.matched_terms


def test_excluded_authors():
    matcher = make(["need a website"], exclude_authors=["AutoModerator", "u/spambot"])
    assert not matcher.match(text="need a website", author="AutoModerator").matched
    assert not matcher.match(text="need a website", author="SpamBot").matched
    assert matcher.match(text="need a website", author="alice").matched


def test_spans_are_merged_and_usable_for_highlighting():
    matcher = make(["need a website", "website"], require=["website"])
    text = "I need a website for my shop. A website!"
    result = matcher.match(text=text)
    spans = result.spans("text")
    assert spans == [(2, 16), (32, 39)]
    assert text[2:16] == "need a website"
    assert result.spans("title") == []


def test_relevance_prefers_title_and_distinct_terms():
    matcher = make(["need a website", "web developer"], require=["website"])
    title_hit = matcher.match(title="Need a website", text="")
    body_hit = matcher.match(title="", text="need a website")
    both = matcher.match(title="Need a website", text="any web developer here?")
    assert title_hit.relevance > body_hit.relevance
    assert both.relevance > title_hit.relevance


def test_server_terms_prefers_plain_require_terms():
    matcher = make(["need a website", "/regex/"], require=["website", "web-site"])
    assert matcher.server_terms() == ["website", "web site"]
    only_include = make(["need a website", "hire a dev"])
    assert only_include.server_terms() == ["need a website", "hire a dev"]
    unsafe = make(["/regex only/"])
    assert unsafe.server_terms() == []
    wildcard = make(["recommend*"], require=["websit*"])
    assert wildcard.server_terms() == []


def test_parse_lines_strips_comments_blank_and_duplicates():
    assert parse_lines("a\n\n# comment\n a \nb") == ["a", "b"]
    assert parse_lines(["x", "x", " y"]) == ["x", "y"]


def test_validate_requires_include_terms():
    with pytest.raises(KeywordError):
        validate_keyword_set({"name": "empty", "include_any": "", "require_any": "", "exclude": ""})
    with pytest.raises(KeywordError):
        validate_keyword_set({"name": "bad", "include_any": "*"})


def test_curly_apostrophes_match_straight_ones():
    pattern = phrase_to_regex("i'm looking")
    assert pattern.search("I’m looking for help")


def test_regex_label_is_used_in_results():
    matcher = make([r"/how much .{0,20} website/ price question", r"/https?:\/\/[^ ]+/"])
    result = matcher.match(text="How much would a website cost? see https://x.y/z")
    assert result.matched
    assert "price question" in result.matched_terms
    assert any(t.startswith("/https") for t in result.matched_terms)
