"""Keep abusive words out of names players type (2026-09-15).

Checked wherever a player types a name other people will see: the account's
display name, the roster name chosen when creating a profile, a guest (+1)
name, and a tournament entry name. Club admin renames are NOT checked: an
admin renaming a player is the fix when this blocks a real name.

A word list, not a judgement. It catches the obvious and the usual dodges
("f.u.c.k", "sh1t", "fuuuck", "b1tch"), and a determined person will still get
something past it. That is accepted: names are seen by a club's own members,
admins can rename a player and reset an account name, and changes are in the
audit log. The list lives in name_blocklist.txt.

Matching is on WHOLE words so real names containing a listed word pass
(Hancock, Dickens, Cockburn); a handful of words that are never part of a name
match anywhere (the [substring] section).
"""
import pathlib
import re
import unicodedata

_LIST = pathlib.Path(__file__).with_name("name_blocklist.txt")

# Look-alike characters people swap in. "1" is tried as both i and l.
_LEET = str.maketrans({"@": "a", "4": "a", "3": "e", "!": "i", "0": "o", "5": "s",
                       "$": "s", "7": "t", "+": "t", "8": "b", "9": "g"})


def _load() -> tuple[frozenset[str], tuple[str, ...]]:
    words, subs, section = set(), [], "words"
    for raw in _LIST.read_text(encoding="utf-8").splitlines():
        line = raw.strip().lower()
        if not line or line.startswith("#"):
            continue
        if line == "[substring]":
            section = "substring"
            continue
        (subs.append(line) if section == "substring" else words.add(line))
    return frozenset(words), tuple(subs)


_WORDS, _SUBSTRINGS = _load()


def _normalised_forms(name: str) -> set[str]:
    """The name lowercased with accents and look-alikes undone, once reading
    "1" as i and once as l."""
    base = unicodedata.normalize("NFKD", name)
    base = "".join(ch for ch in base if not unicodedata.combining(ch)).lower()
    base = base.translate(_LEET)
    return {base.replace("1", "i"), base.replace("1", "l")}


def _tokens(form: str) -> set[str]:
    parts = [p for p in re.split(r"[^a-z]+", form) if p]
    tokens = set(parts)
    # Letters spaced out to dodge a word match: "f u c k", "s.h.i.t".
    run: list[str] = []
    for p in parts + [""]:
        if len(p) == 1:
            run.append(p)
        else:
            if len(run) > 1:
                tokens.add("".join(run))
            run = []
    # Stretched letters: "fuuuuck" and "shiiit" read as the word.
    for t in list(tokens):
        tokens.add(re.sub(r"(.)\1{2,}", r"\1\1", t))
        tokens.add(re.sub(r"(.)\1+", r"\1", t))
    return tokens


def is_blocked(name: str) -> bool:
    """True if the name contains a blocked word."""
    for form in _normalised_forms(name or ""):
        tokens = _tokens(form)
        if tokens & _WORDS:
            return True
        if any(s in t for t in tokens for s in _SUBSTRINGS):
            return True
    return False


BLOCKED_MESSAGE = "Please choose a different name."
