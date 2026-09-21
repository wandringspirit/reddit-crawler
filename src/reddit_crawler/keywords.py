"""Keyword sets and client-side matching.

A keyword set has three lists of *terms*:

* ``include_any`` – the item matches if at least one of these hits (the search phrases).
* ``require_any`` – optional extra gate: at least one of these must also hit
  (e.g. "website" so that "looking for a developer" alone is not enough).
* ``exclude``     – the item is rejected if any of these hit.

Each term is one line of text. Syntax:

* plain text            → case-insensitive whole-word/phrase match, hyphens and
                          extra whitespace between words are tolerated
* ``recommend*``        → trailing ``*`` on a word = prefix wildcard
* ``/regex/``           → a Python regular expression (case-insensitive unless
                          the set is case sensitive); ``/regex/ some label`` gives
                          it a readable name for the results
* ``title: ...``        → only match in the post title
* ``text: ...``         → only match in the post body / comment body

Matching happens on the *original* text with ``re.IGNORECASE`` so the returned
spans can be used directly for highlighting.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

FIELDS = ("title", "text")
MAX_REGEX_LEN = 400
# Characters that may separate words inside a phrase: whitespace, hyphens, slashes.
_SEP = r"[\s\-‐-―/]+"
_APOSTROPHES = "['’‘`]"


class KeywordError(ValueError):
    """Raised when a keyword set cannot be compiled (bad regex, empty set, ...)."""


@dataclass(frozen=True)
class Term:
    raw: str                      # the original line as the user typed it
    pattern: re.Pattern           # compiled matcher
    fields: tuple[str, ...]       # subset of FIELDS this term applies to
    is_regex: bool = False
    name: str = ""                # optional label given after a regex

    @property
    def label(self) -> str:
        """Short name shown in the results (long regexes are abbreviated)."""
        if self.name:
            return self.name
        if self.is_regex and len(self.raw) > 48:
            return self.raw[:45] + "…/"
        return self.raw


@dataclass
class TermHit:
    term: str
    field: str
    start: int
    end: int


@dataclass
class MatchResult:
    matched: bool
    include_hits: list[TermHit] = field(default_factory=list)
    require_hits: list[TermHit] = field(default_factory=list)
    exclude_hits: list[TermHit] = field(default_factory=list)
    relevance: float = 0.0

    @property
    def matched_terms(self) -> list[str]:
        seen: list[str] = []
        for hit in self.include_hits + self.require_hits:
            if hit.term not in seen:
                seen.append(hit.term)
        return seen

    def spans(self, field_name: str) -> list[tuple[int, int]]:
        """Merged, sorted highlight spans for one field."""
        raw = sorted(
            (h.start, h.end) for h in self.include_hits + self.require_hits if h.field == field_name
        )
        merged: list[tuple[int, int]] = []
        for start, end in raw:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        return merged


def _split_field_prefix(line: str) -> tuple[tuple[str, ...], str]:
    lowered = line.lower()
    for prefix, fields in (("title:", ("title",)), ("text:", ("text",)), ("body:", ("text",))):
        if lowered.startswith(prefix):
            return fields, line[len(prefix):].strip()
    return FIELDS, line


def phrase_to_regex(text: str, *, whole_word: bool = True, plural_tolerant: bool = True,
                    case_sensitive: bool = False) -> re.Pattern:
    """Compile a plain phrase into a tolerant regex.

    ``web-developer`` and ``web   developer`` both match ``web developer``; a
    trailing ``*`` on a word matches any continuation; the last word optionally
    takes an ``s``/``es`` suffix when *plural_tolerant*.
    """
    tokens = [t for t in re.split(r"[\s\-/]+", text.strip()) if t]
    if not tokens:
        raise KeywordError(f"Empty keyword: {text!r}")
    parts: list[str] = []
    for index, token in enumerate(tokens):
        wildcard = token.endswith("*")
        token = token.rstrip("*")
        if not token:
            raise KeywordError(f"A lone '*' is not a valid keyword: {text!r}")
        escaped = re.escape(token).replace("'", _APOSTROPHES)
        if wildcard:
            escaped += r"\w*"
        elif plural_tolerant and index == len(tokens) - 1 and token.isalpha() and len(token) > 3:
            escaped += r"(?:e?s)?"
        parts.append(escaped)
    body = _SEP.join(parts)
    if whole_word:
        body = rf"(?<!\w){body}(?!\w)"
    return re.compile(body, 0 if case_sensitive else re.IGNORECASE)


def compile_term(line: str, *, whole_word: bool, plural_tolerant: bool, case_sensitive: bool) -> Term:
    fields, body = _split_field_prefix(line.strip())
    if not body:
        raise KeywordError(f"Empty keyword line: {line!r}")
    regex = _split_regex(body)
    if regex is not None:
        source, name = regex
        if len(source) > MAX_REGEX_LEN:
            raise KeywordError(f"Regex too long (max {MAX_REGEX_LEN} chars): {line!r}")
        try:
            pattern = re.compile(source, 0 if case_sensitive else re.IGNORECASE)
        except re.error as exc:
            raise KeywordError(f"Invalid regex {line!r}: {exc}") from exc
        return Term(raw=line.strip(), pattern=pattern, fields=fields, is_regex=True, name=name)
    pattern = phrase_to_regex(body, whole_word=whole_word, plural_tolerant=plural_tolerant,
                              case_sensitive=case_sensitive)
    return Term(raw=line.strip(), pattern=pattern, fields=fields)


def _split_regex(body: str) -> tuple[str, str] | None:
    """``/pattern/`` → (pattern, ""); ``/pattern/ label`` → (pattern, label); otherwise None."""
    if len(body) < 2 or not body.startswith("/"):
        return None
    if body.endswith("/"):
        return body[1:-1], ""
    cut = body.rfind("/ ")
    if cut <= 0:
        return None
    return body[1:cut], body[cut + 2:].strip()


def parse_lines(value: str | Iterable[str] | None) -> list[str]:
    """Split a textarea value (or list) into clean, de-duplicated lines."""
    if value is None:
        return []
    lines = value.splitlines() if isinstance(value, str) else list(value)
    out: list[str] = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or line in out:
            continue
        out.append(line)
    return out


@dataclass
class KeywordSet:
    name: str = "Untitled"
    include_any: list[str] = field(default_factory=list)
    require_any: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    exclude_authors: list[str] = field(default_factory=lambda: ["AutoModerator"])
    whole_word: bool = True
    plural_tolerant: bool = True
    case_sensitive: bool = False
    id: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "KeywordSet":
        return cls(
            id=str(data.get("id") or ""),
            name=str(data.get("name") or "Untitled").strip() or "Untitled",
            include_any=parse_lines(data.get("include_any")),
            require_any=parse_lines(data.get("require_any")),
            exclude=parse_lines(data.get("exclude")),
            exclude_authors=parse_lines(data.get("exclude_authors", ["AutoModerator"])),
            whole_word=bool(data.get("whole_word", True)),
            plural_tolerant=bool(data.get("plural_tolerant", True)),
            case_sensitive=bool(data.get("case_sensitive", False)),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "include_any": list(self.include_any),
            "require_any": list(self.require_any),
            "exclude": list(self.exclude),
            "exclude_authors": list(self.exclude_authors),
            "whole_word": self.whole_word,
            "plural_tolerant": self.plural_tolerant,
            "case_sensitive": self.case_sensitive,
        }

    def compile(self) -> "Matcher":
        return Matcher(self)


class Matcher:
    """Compiled form of a :class:`KeywordSet`."""

    def __init__(self, keyword_set: KeywordSet):
        self.keyword_set = keyword_set
        options = dict(whole_word=keyword_set.whole_word, plural_tolerant=keyword_set.plural_tolerant,
                       case_sensitive=keyword_set.case_sensitive)
        self.include = [compile_term(line, **options) for line in keyword_set.include_any]
        self.require = [compile_term(line, **options) for line in keyword_set.require_any]
        self.exclude = [compile_term(line, **options) for line in keyword_set.exclude]
        if not self.include:
            raise KeywordError("Add at least one search phrase (include list).")
        self._excluded_authors = {a.lower().lstrip("u/") for a in keyword_set.exclude_authors}

    @staticmethod
    def _hits(terms: list[Term], fields: dict[str, str], *, first_only: bool) -> list[TermHit]:
        """Collect hits of *terms* in *fields*; with *first_only* stop at the first hit per term."""
        hits: list[TermHit] = []
        for term in terms:
            found = False
            for field_name in term.fields:
                haystack = fields.get(field_name) or ""
                if not haystack:
                    continue
                for match in term.pattern.finditer(haystack):
                    if match.end() == match.start():
                        continue  # ignore empty regex matches
                    hits.append(TermHit(term.label, field_name, match.start(), match.end()))
                    found = True
                    if first_only:
                        break
                if found and first_only:
                    break
        return hits

    def match(self, *, title: str = "", text: str = "", author: str | None = None) -> MatchResult:
        if author and author.lower().lstrip("u/") in self._excluded_authors:
            return MatchResult(False)
        fields = {"title": title or "", "text": text or ""}
        exclude_hits = self._hits(self.exclude, fields, first_only=True)
        if exclude_hits:
            return MatchResult(False, exclude_hits=exclude_hits)
        include_hits = self._hits(self.include, fields, first_only=False)
        if not include_hits:
            return MatchResult(False)
        require_hits: list[TermHit] = []
        if self.require:
            require_hits = self._hits(self.require, fields, first_only=False)
            if not require_hits:
                return MatchResult(False, include_hits=include_hits)
        result = MatchResult(True, include_hits=include_hits, require_hits=require_hits)
        result.relevance = self._relevance(result)
        return result

    @staticmethod
    def _relevance(result: MatchResult) -> float:
        """Distinct include terms count most; a title hit is worth extra."""
        score = 0.0
        seen: set[str] = set()
        for hit in result.include_hits:
            if hit.term in seen:
                continue
            seen.add(hit.term)
            score += 1.5 if hit.field == "title" else 1.0
        if result.require_hits:
            score += 0.5
        return round(score, 2)

    def server_terms(self) -> list[str]:
        """Plain (non-regex, non-wildcard) phrases usable as a server-side recall prefilter.

        Returns the ``require_any`` phrases when they are all plain (they are a
        superset gate), otherwise the ``include_any`` phrases when all plain,
        otherwise an empty list meaning "no safe prefilter exists".
        """
        for group in (self.require, self.include):
            if not group:
                continue
            plain = []
            for term in group:
                if term.is_regex or "*" in term.raw:
                    plain = []
                    break
                _fields, body = _split_field_prefix(term.raw)
                cleaned = re.sub(r"[^\w\s']", " ", body).strip()
                if cleaned:
                    plain.append(cleaned)
            if plain:
                return plain
        return []


def validate_keyword_set(data: dict) -> KeywordSet:
    """Parse and compile a keyword set from the API, raising KeywordError on problems."""
    keyword_set = KeywordSet.from_dict(data)
    keyword_set.compile()
    return keyword_set
