#!/usr/bin/env python3
"""Shared helpers for the reddit-research scripts. Imported by sibling scripts
(`from _common import ...` works because Python puts the script's folder first on
sys.path, under both `python3 scripts/x.py` and `uv run scripts/x.py`).

Rules that live here so every script applies the same ones:
- DELETED: author values that mean "no person" ("" / [deleted] / [removed]).
- is_bot(author): AutoModerator, names ending in "modteam", and names whose tokens
  (split on _, -, digits and camelCase) include exactly "bot" (RemindMeBot, auto_bot_3),
  so "Bottle" and "Both-Noun-1234" stay human. Single-token bots (sneakpeekbot) need the
  manual list (`--bots-file`, one name per line).
- norm_text(): the normalisation used for duplicate detection (lowercase, no
  punctuation, single spaces).
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from pathlib import Path

if sys.version_info < (3, 9):
    sys.exit("reddit-research scripts need Python 3.9 or newer")

DELETED = {"", "[deleted]", "[removed]"}
BOT_NAMES = {"automoderator"}
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_PUNCT = re.compile(r"[^\w\s]+", re.UNICODE)
_WS = re.compile(r"\s+")


def is_deleted(author: str | None) -> bool:
    return (author or "") in DELETED


def author_tokens(author: str) -> list[str]:
    return [t for t in re.split(r"[_\-\s0-9]+", _CAMEL.sub("_", author).lower()) if t]


def is_bot(author: str | None, extra: set[str] | None = None) -> bool:
    a = (author or "").lower()
    if not a:
        return False
    if a in BOT_NAMES or (extra and a in extra) or a.endswith("modteam"):
        return True
    return "bot" in author_tokens(author or "")


def read_bots_file(path: Path | None) -> set[str]:
    if not path:
        return set()
    return {ln.strip().lower() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")}


def norm_text(s: str | None) -> str:
    s = unicodedata.normalize("NFKC", s or "").lower()
    return _WS.sub(" ", _PUNCT.sub(" ", s)).strip()


def read_ndjson(path: Path):
    with Path(path).open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def read_regex_files(paths) -> str:
    """Join one or more regex files into one alternation: each file's text, stripped, becomes
    a (?:...) group. The same expression serves DuckDB's regexp_matches (RE2: no lookbehind)
    and Python's re, so keep the phrases simple."""
    parts = [Path(p).read_text(encoding="utf-8").strip() for p in paths]
    parts = [x for x in parts if x]
    if not parts:
        raise SystemExit(f"empty regex file(s): {paths}")
    return "|".join(f"(?:{x})" for x in parts) if len(parts) > 1 else parts[0]


def read_id_list(path: Path | None) -> set[str]:
    """One id per line (a tab-separated remainder is ignored); # starts a comment."""
    if not path:
        return set()
    return {ln.strip().split("\t")[0] for ln in Path(path).read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")}


if __name__ == "__main__":
    assert is_bot("AutoModerator") and is_bot("RemindMeBot") and is_bot("auto_bot_3") and is_bot("xModTeam")
    assert not is_bot("Bottle") and not is_bot("Both-Noun-1234") and not is_bot("sneakpeekbot") and not is_bot("")
    assert is_bot("sneakpeekbot", {"sneakpeekbot"})
    assert norm_text("  Hello,  WORLD!! ") == "hello world"
    print("ok")
