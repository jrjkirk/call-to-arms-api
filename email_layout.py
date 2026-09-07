"""The look of every email this app sends.

Extracted from club_emails.py once the onboarding emails had been given the
app's own gunmetal-and-gold and the venue ones were still unstyled
black-on-white. A booking alert and a welcome email arriving from the same
product should not look like they came from two different ones.

Why it is built like 2004
-------------------------
Tables and inline styles, not divs and a stylesheet. Outlook renders with
Word's engine, Gmail strips most of a `<style>` block, and neither has ever
supported flexbox or grid. Everything here is the shape that survives: a
centred table, explicit `bgcolor` attributes alongside the CSS, and every rule
written on the element it applies to.

The card is `width="100%"` with `max-width:600px`, never a fixed 600, which
overflowed at a 380px viewport.

Escaping
--------
`paragraphs()` and `rows_table()` escape everything they are given and then add
markup, so no caller can introduce a tag by accident and no value off a public
form can either. `linkify()` deliberately runs on already-escaped text: the
haystack cannot contain a raw `<`, so a match cannot run past the URL.
"""
import re
from typing import Optional

from sqlmodel import Session

from emailer import esc
from models import AppSetting

LOGO_URL_KEY = "club_email_logo_url"
DEFAULT_LOGO_URL = "https://www.calltoarms.app/email-logo.png"

# The app's own palette, so an email looks like the thing it is about.
PAGE = "#0a0b0e"        # outside the card
CARD = "#12141c"        # the card itself
HEADER = "#101219"      # the band the logo sits on
BORDER = "#332c1c"      # a gold-tinted hairline, not a grey one
TEXT = "#e9e0cd"
MUTED = "#9b937f"
ACCENT = "#c9a14a"

FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"

_SHELL = """\
<!--[if mso]><style>body,table,td{{font-family:Arial,Helvetica,sans-serif!important}}</style><![endif]-->
<div style="display:none;max-height:0;overflow:hidden;opacity:0">{preheader}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="{page}" \
style="background:{page};margin:0;padding:0;width:100%">
  <tr><td align="center" style="padding:28px 12px">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" \
style="width:100%;max-width:600px;border-collapse:separate;border:1px solid {border};border-radius:10px;overflow:hidden">
      <tr><td align="center" bgcolor="{header}" style="background:{header};padding:26px 24px 20px">
        <img src="{logo_url}" width="240" alt="Call to Arms" \
style="display:block;width:240px;max-width:70%;height:auto;border:0;outline:none;text-decoration:none" />
      </td></tr>
      <tr><td bgcolor="{card}" style="background:{card};padding:26px 28px 8px;font-family:{font};\
font-size:15px;line-height:1.6;color:{text}">
        {body}
      </td></tr>
      {cta}
      <tr><td bgcolor="{card}" style="background:{card};padding:18px 28px 26px;border-top:1px solid {border};\
font-family:{font};font-size:12px;line-height:1.5;color:{muted}">
        {footer}<br />
        <a href="https://www.calltoarms.app" style="color:{muted};text-decoration:underline">calltoarms.app</a>
      </td></tr>
    </table>
  </td></tr>
</table>"""

# A padded link inside its own table cell. Bulletproof enough without dropping
# to VML: the cell carries the colour, so a client that ignores the anchor's
# styling still shows a gold block with readable text on it.
_CTA = """\
<tr><td bgcolor="{card}" style="background:{card};padding:8px 28px 26px">
  <table role="presentation" cellpadding="0" cellspacing="0" border="0">
    <tr><td align="center" bgcolor="{accent}" style="background:{accent};border-radius:6px">
      <a href="{url}" style="display:inline-block;padding:12px 26px;font-family:{font};font-size:15px;\
font-weight:bold;color:#1b1206;text-decoration:none">{label}</a>
    </td></tr>
  </table>
</td></tr>"""

DEFAULT_FOOTER = "The Call to Arms Team"

_URL_RE = re.compile(r"https?://[^\s<]+")


def linkify(escaped: str) -> str:
    """Turn URLs into links inside text that has ALREADY been escaped.

    That ordering is the safety property: escaped text contains no raw `<`, so
    a match cannot run past the end of the URL into markup.
    """
    return _URL_RE.sub(
        lambda m: f'<a href="{m.group(0)}" style="color:{ACCENT}">{m.group(0)}</a>',
        escaped,
    )


def paragraphs(text: str) -> str:
    """Plain text to styled HTML paragraphs. A blank line starts a new one.

    Empty paragraphs are dropped, which is what lets a caller interpolate a
    value that is sometimes absent without leaving a gap where it would be.
    """
    style = f"margin:0 0 16px;font-family:{FONT};font-size:15px;line-height:1.6;color:{TEXT}"
    return "".join(
        f'<p style="{style}">{linkify(esc(p.strip())).replace(chr(10), "<br />")}</p>'
        for p in re.split(r"\n\s*\n", text)
        if p.strip()
    )


def rows_table(rows) -> str:
    """A label/value table, for the emails that are mostly booking details.

    Takes (label, value) pairs and escapes both. Values that are None or empty
    are dropped rather than printed as a blank row.
    """
    cells = "".join(
        f'<tr><td style="padding:4px 14px 4px 0;color:{MUTED};white-space:nowrap;'
        f'font-family:{FONT};font-size:14px">{esc(label)}</td>'
        f'<td style="padding:4px 0;color:{TEXT};font-family:{FONT};font-size:14px">'
        f"<strong>{esc(value)}</strong></td></tr>"
        for label, value in rows
        if value not in (None, "")
    )
    return f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;margin:0 0 16px">{cells}</table>'


def logo_url(db: Optional[Session]) -> str:
    """The logo an email points at. A setting, so it can change without a deploy.

    Takes an optional session because some senders (the venue ones) are called
    from paths that have already closed theirs, and a missing logo is not worth
    reopening a connection over.
    """
    if db is None:
        return DEFAULT_LOGO_URL
    row = db.get(AppSetting, LOGO_URL_KEY)
    return row.value if row and row.value else DEFAULT_LOGO_URL


def wrap(
    body_html: str,
    *,
    logo: str = DEFAULT_LOGO_URL,
    preheader: str = "",
    cta_url: Optional[str] = None,
    cta_label: Optional[str] = None,
    footer: str = DEFAULT_FOOTER,
) -> str:
    """Put the branded shell around already-rendered body HTML."""
    cta = ""
    if cta_url and cta_label:
        cta = _CTA.format(card=CARD, accent=ACCENT, font=FONT,
                          url=esc(cta_url), label=esc(cta_label))
    return _SHELL.format(
        page=PAGE, card=CARD, header=HEADER, border=BORDER, text=TEXT,
        muted=MUTED, accent=ACCENT, font=FONT, logo_url=esc(logo),
        # The line an inbox shows beside the subject. Left empty it picks up
        # whatever markup comes first, which is never anything useful.
        preheader=esc(preheader), body=body_html, cta=cta, footer=esc(footer),
    )


def subject_safe(value: str, limit: int = 120) -> str:
    """A subject line, fit to be a header.

    Deliberately NOT html-escaped: entities in a subject line are their own bug.
    Flattened and capped instead, because a club name off a public form has no
    business putting newlines in a header or 400 characters in a subject.
    """
    flat = " ".join(str(value).split())
    return flat[: limit - 1] + "…" if len(flat) > limit else flat
