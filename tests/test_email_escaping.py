"""Every HTML email must escape the text people typed into it.

This exists because none of them did. The table-booking email built
`<li>{name}</li>` straight from `Signup.player_name`, and
`admin/table-booking/preview` hands that same HTML back to the admin page,
which renders it with `{@html}` — so a player could put markup in their name
and have it run in a club admin's browser. The venue booking emails had the
same shape with a booker's name, phone and notes, which come off a public form
that needs no account at all.

Each block below feeds a payload through one renderer and asserts the markup
came out inert.

Run: PYTHONPATH=. python tests/test_email_escaping.py
"""
import sys
from html.parser import HTMLParser
from types import SimpleNamespace

from emailer import esc
from table_booking import render_table_booking_email
from venue import _booker_email_html, _staff_email_html

FAILURES = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else f"  {detail}"))
    if not cond:
        FAILURES.append(label)


# The classic: closes nothing, needs no script tag, fires on render.
PAYLOAD = '<img src=x onerror="alert(1)">'
ESCAPED = "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;"


class _Tags(HTMLParser):
    """Collects the tags and attribute names a browser would actually see."""

    def __init__(self):
        super().__init__()
        self.tags: list[str] = []
        self.attrs: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs += [a for a, _ in attrs]


def inert(html: str) -> bool:
    """True if nothing executable survived into the parsed document.

    Parsed rather than substring-matched: escaped output still *contains* the
    text "onerror=", it just isn't an attribute any more, and a naive `in`
    check can't tell the two apart. What matters is what a parser sees.
    """
    t = _Tags()
    t.feed(html)
    injected_tags = {"img", "script", "iframe", "object", "embed", "svg"}
    return not (set(t.tags) & injected_tags) and not any(
        a.startswith("on") for a in t.attrs
    )


print("\n1. esc() itself")
check("markup is neutralised", esc(PAYLOAD) == ESCAPED, esc(PAYLOAD))
check("quotes are escaped too, so attributes can't be broken out of",
      "&quot;" in esc('a"b') and "&#x27;" in esc("a'b"))
check("None renders empty, not the word None", esc(None) == "")
check("non-strings are coerced", esc(7) == "7")


print("\n2. Table-booking email — the one that reaches an admin's browser")
cfg = SimpleNamespace(
    venue_name=PAYLOAD, subject_template=None, players_per_table=2,
    include_player_names=True, notes=PAYLOAD,
)
subject, html = render_table_booking_email(
    cfg, system=PAYLOAD, week="02/09/2026", tables=4, headcount=8,
    player_names=["Ann", PAYLOAD],
)
check("a player's name cannot inject markup", ESCAPED in html)
check("the venue name cannot inject markup", html.count(ESCAPED) >= 2)
check("admin notes cannot inject markup", html.count(ESCAPED) >= 3)
check("nothing live survived anywhere in the body", inert(html), html[:120])
check("the real content is still there", "<li>Ann</li>" in html and "<strong>8 player" in html)
# The subject is a plain-text header: escaping it would show &amp; to the venue.
check("the subject is left unescaped on purpose", "&lt;" not in subject, subject)


print("\n3. Venue staff email — booker name, phone and notes from a public form")
booking = {
    "date": "Wednesday 02 September 2026", "time": "18:00–21:00",
    "table": PAYLOAD, "table_size": None, "party_size": 2, "game": PAYLOAD,
    "name": PAYLOAD, "email": "a@b.com", "phone": PAYLOAD, "notes": PAYLOAD,
    "status": "confirmed",
}
club = SimpleNamespace(name=PAYLOAD)
staff = _staff_email_html(club, booking, "confirmed")
check("booker name, phone, notes and table are all escaped", staff.count(ESCAPED) >= 5,
      f"found {staff.count(ESCAPED)}")
check("nothing live survived", inert(staff), staff[:120])
check("the club name in the footer is escaped", ESCAPED in staff)
check("the table still renders", "<table" in staff and "Booked by" in staff)


print("\n4. Booker confirmation email")
booker = _booker_email_html(club, booking, "confirmed", "https://x.test/manage?t=abc")
check("table and game are escaped", booker.count(ESCAPED) >= 2)
check("nothing live survived", inert(booker), booker[:120])
check("the club name inside the lead sentence is escaped", ESCAPED in booker)
check("the manage link still works", 'href="https://x.test/manage?t=abc"' in booker)

# A link is built by us, but the attribute must still be closed safely.
evil_link = 'https://x.test/" onmouseover="alert(1)'
booker2 = _booker_email_html(club, booking, "confirmed", evil_link)
check("a link cannot break out of its href attribute",
      'onmouseover="alert(1)"' not in booker2 and "&quot;" in booker2)

print(f"\n{'ALL PASS' if not FAILURES else str(len(FAILURES)) + ' FAILURE(S): ' + ', '.join(FAILURES)}")
sys.exit(1 if FAILURES else 0)
