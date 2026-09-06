"""The three emails a club organiser gets while being onboarded, and the
templates behind them.

Until 2026-09-06 they got none: a request went into silence until someone
remembered to write by hand. Now they get an acknowledgement, a "you're live"
and, if it comes to it, a decline — and a platform admin can edit all of it from
the admin console without a deploy, because onboarding copy is the sort of thing
you want to tune after the third club rather than the third release.

Two decisions worth keeping:

**Templates are plain text, not HTML.** An editor typing into a box should not
be able to send half-open markup out over our sending domain, and shouldn't have
to think about tags to add a paragraph. Bodies are written as plain text with
blank lines between paragraphs; render() escapes the lot and builds the HTML.
The only markup that survives is the linkifier below, which runs on
already-escaped text and so cannot be used to inject anything.

**Nothing here may break its caller.** A club request must still be recorded,
and a provisioned club must still exist, if Resend is down or the address was
mistyped. Every send swallows its own failure and reports what happened for the
caller to surface — a mistyped address on a public form is ordinary input, not
an incident, which is the distinction emailer.UndeliverableRecipient draws.
"""
import re
from typing import Optional

from sqlmodel import Session

from emailer import UndeliverableRecipient, esc, send_email
from models import AppSetting

# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------
# `tokens` is what the editor is shown. Rendering uses plain replacement rather
# than str.format, matching call_to_arms_content.render — a stray brace in
# hand-edited copy must not raise mid-send, and an unknown {token} is left
# visible rather than swallowed, so a typo is obvious in the preview.

REQUEST_RECEIVED = "request_received"
CLUB_LIVE = "club_live"
REQUEST_DECLINED = "request_declined"

# NOTE ON REPLIES: none of these invite one, and that is deliberate.
# calltoarms.app has no MX records — the domain accepts no mail — and Resend is
# send-only, so a reply to notifications@calltoarms.app bounces. Copy that says
# "just reply" is a promise the infrastructure cannot keep, and a bounce is a
# poor first impression from someone we are trying to onboard. If a real inbox
# or a forwarding address ever exists, set Reply-To and this can change.
EMAIL_KINDS: dict[str, dict] = {
    REQUEST_RECEIVED: {
        "label": "Request received",
        "when": "Sent the moment someone submits the add-my-club form.",
        "tokens": ["requester_name", "club_name"],
        "subject": "We've got your request for {club_name}",
        "body": (
            "Hi {requester_name},\n"
            "\n"
            "Thanks for asking us to add {club_name} to Call to Arms. Your request is in "
            "and we'll look at it shortly — usually the same day.\n"
            "\n"
            "When it's approved you'll get a second email with your club's own web "
            "address. You won't need to set up an account: the Discord you signed in "
            "with is already linked, and you'll land straight in your club's admin.\n"
            "\n"
            "If you didn't make this request, you can ignore this — nothing has been "
            "created."
        ),
    },
    CLUB_LIVE: {
        "label": "Club is live",
        "when": "Sent when you provision a request into a real club.",
        # systems_line and admin_line are whole sentences, computed because they
        # depend on what was actually switched on. They come through empty when
        # they don't apply, and an empty paragraph is dropped — so a template
        # using them reads correctly either way.
        "tokens": [
            "requester_name", "club_name", "club_url",
            "systems", "club_night", "systems_line", "admin_line",
        ],
        "subject": "{club_name} is live on Call to Arms",
        # The one email with somewhere to go, so it gets a button. The URL comes
        # from the context rather than the body, which means an edited template
        # cannot accidentally remove the way in.
        "cta": {"url": "{club_url}", "label": "Open your club"},
        "body": (
            "Hi {requester_name},\n"
            "\n"
            "{club_name} is live on Call to Arms — it's ready for your players.\n"
            "\n"
            "{systems_line}\n"
            "\n"
            "{admin_line}"
        ),
    },
    REQUEST_DECLINED: {
        "label": "Request declined",
        "when": "Sent when you decline a request, unless you decline silently.",
        "tokens": ["requester_name", "club_name", "reason"],
        "subject": "About your Call to Arms request for {club_name}",
        "body": (
            "Hi {requester_name},\n"
            "\n"
            "Thanks for your interest in Call to Arms. We're not able to set up "
            "{club_name} at the moment.\n"
            "\n"
            "{reason}\n"
            "\n"
            "If something changes, you're welcome to ask again at "
            "https://www.calltoarms.app/request-club — we're happy to take another look."
        ),
    },
}

# Sample values for the preview, so an admin can see the shape of a real email
# before any club does.
SAMPLE_CONTEXT = {
    "requester_name": "Nick",
    "club_name": "Badmoon Bunker",
    "club_url": "https://badmoon.calltoarms.app",
    "systems": "The Old World, Kill Team",
    "club_night": "Thursday",
    "systems_line": (
        "We've switched on The Old World, Kill Team on Thursdays to save you a job. "
        "Change any of it from your admin — it's a starting point, not a decision."
    ),
    "admin_line": (
        "Sign in with the same Discord account and you'll arrive in your club's admin "
        "as its owner. There's a short checklist on the first screen — the one step "
        "worth doing before your next club night is connecting a Discord channel, so "
        "sign-ups and pairings post themselves."
    ),
    "reason": "We couldn't tell from the link that you run this club.",
}


def _key(kind: str, part: str) -> str:
    return f"club_email_{kind}_{part}"


def get_template(db: Session, kind: str) -> dict:
    """The subject and body in force for one email — the stored override if a
    platform admin has edited it, otherwise the default above."""
    spec = EMAIL_KINDS[kind]
    subject_row = db.get(AppSetting, _key(kind, "subject"))
    body_row = db.get(AppSetting, _key(kind, "body"))
    return {
        "subject": (subject_row.value if subject_row and subject_row.value else spec["subject"]),
        "body": (body_row.value if body_row and body_row.value else spec["body"]),
        "customised": bool((subject_row and subject_row.value) or (body_row and body_row.value)),
    }


def fill(text: str, context: dict) -> str:
    """Plain token replacement. Never raises on stray braces, and leaves an
    unknown {token} visible so a typo shows up in the preview rather than as a
    silent gap in a real email."""
    out = text
    for key, value in context.items():
        out = out.replace("{" + key + "}", "" if value is None else str(value))
    return out


# Matches a URL in text we have ALREADY escaped, which is why this is safe: the
# haystack cannot contain a raw '<', so the match can't run past the URL into
# markup, and the href is re-escaped for the attribute anyway.
_URL_RE = re.compile(r"https?://[^\s<]+")


def _linkify(escaped: str) -> str:
    return _URL_RE.sub(
        lambda m: f'<a href="{m.group(0)}" style="color:{_ACCENT}">{m.group(0)}</a>',
        escaped,
    )


# ---------------------------------------------------------------------------
# The wrapper
# ---------------------------------------------------------------------------
# Tables and inline styles, not divs and a stylesheet. That is not nostalgia:
# Outlook renders with Word's engine, Gmail strips most of a <style> block, and
# neither has ever supported flexbox or grid. Everything below is the shape that
# survives — a centred table, explicit bgcolor attributes alongside the CSS, and
# every rule written on the element it applies to.

LOGO_URL_KEY = "club_email_logo_url"
DEFAULT_LOGO_URL = "https://www.calltoarms.app/email-logo.png"

# The app's own palette, so an email looks like the thing it is about.
_PAGE = "#0a0b0e"        # outside the card
_CARD = "#12141c"        # the card itself
_HEADER = "#101219"      # the band the logo sits on
_BORDER = "#332c1c"      # a gold-tinted hairline, not a grey one
_TEXT = "#e9e0cd"
_MUTED = "#9b937f"
_ACCENT = "#c9a14a"

_FONT = (
    "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
)

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
        The Call to Arms Team<br />
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


def logo_url(db: Session) -> str:
    row = db.get(AppSetting, LOGO_URL_KEY)
    return (row.value if row and row.value else DEFAULT_LOGO_URL)


def _wrap(body_html: str, *, logo: str, preheader: str = "",
          cta_url: Optional[str] = None, cta_label: Optional[str] = None) -> str:
    cta = ""
    if cta_url and cta_label:
        cta = _CTA.format(card=_CARD, accent=_ACCENT, font=_FONT,
                          url=esc(cta_url), label=esc(cta_label))
    return _SHELL.format(
        page=_PAGE, card=_CARD, header=_HEADER, border=_BORDER, text=_TEXT,
        muted=_MUTED, accent=_ACCENT, font=_FONT, logo_url=esc(logo),
        # The line an inbox shows next to the subject. Left empty it shows the
        # first thing in the HTML, which for a branded email is usually nothing
        # useful at all.
        preheader=esc(preheader), body=body_html, cta=cta,
    )


def render_body(body_text: str, context: dict) -> str:
    """Plain-text template + context -> the HTML we actually send.

    Order matters: fill first, then escape the whole thing, then build markup.
    Escaping after substitution means neither the admin's copy nor a club name
    off a public form can introduce markup — the only tags in the output are the
    ones added below.

    Empty paragraphs are dropped, which is what makes the conditional tokens
    work: a club with no systems enabled gets no stray blank paragraph where
    {systems_line} was.
    """
    filled = fill(body_text, context)
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", filled)]
    # Styles are inline on every paragraph because a <style> block is stripped
    # or ignored by most of the clients these land in.
    style = f"margin:0 0 16px;font-family:{_FONT};font-size:15px;line-height:1.6;color:{_TEXT}"
    return "".join(
        f'<p style="{style}">{_linkify(esc(p)).replace(chr(10), "<br />")}</p>'
        for p in paragraphs if p
    )


def render_text(body_text: str, context: dict) -> str:
    """The plain-text alternative — the filled template, unchanged.

    Which is the quiet advantage of writing templates as text in the first
    place: the text part is not a lossy conversion of the HTML, it IS the
    source, and the HTML is the derived thing.
    """
    filled = fill(body_text, context)
    return "\n\n".join(p.strip() for p in re.split(r"\n\s*\n", filled) if p.strip())


def preview_html(db: Session, kind: str, subject: str, body_text: str, context: dict) -> str:
    """The full email for an UNSAVED draft — shell, logo, button and all.

    Separate from render() because that reads the stored template, and the whole
    point of a preview is to see the version that isn't stored yet.
    """
    spec = EMAIL_KINDS[kind]
    cta = spec.get("cta")
    return _wrap(
        render_body(body_text, context),
        logo=logo_url(db),
        preheader=render_text(body_text, context).split("\n")[0][:120],
        cta_url=fill(cta["url"], context) if cta else None,
        cta_label=cta["label"] if cta else None,
    )


def render(db: Session, kind: str, context: dict) -> tuple[str, str, str]:
    """(subject, html, text) for one email, using whatever template is in force."""
    tpl = get_template(db, kind)
    spec = EMAIL_KINDS[kind]
    subject = _subject_safe(fill(tpl["subject"], context))
    cta = spec.get("cta")
    html = _wrap(
        render_body(tpl["body"], context),
        logo=logo_url(db),
        # The first line of the message, which is what an inbox shows beside the
        # subject if we don't say otherwise.
        preheader=render_text(tpl["body"], context).split("\n")[0][:120],
        cta_url=fill(cta["url"], context) if cta else None,
        cta_label=cta["label"] if cta else None,
    )
    return subject, html, render_text(tpl["body"], context)


def _subject_safe(value: str, limit: int = 120) -> str:
    """A subject line, fit to be a header.

    Deliberately NOT html-escaped — entities in a subject line are their own bug
    (see CLAUDE.md). Flattened and capped instead: a club name off a public form
    has no business putting newlines in a header or 400 characters in a subject.
    """
    flat = " ".join(str(value).split())
    return flat[: limit - 1] + "…" if len(flat) > limit else flat


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def _send(to: str, subject: str, html: str, text: Optional[str] = None) -> str:
    """Send, and say what happened instead of raising.

    Returns "sent", "bad_address", "not_configured:…" or "failed:…". Callers log
    or surface it; none of them should care enough to fail.
    """
    try:
        send_email(to=to, subject=subject, html=html, text=text)
        return "sent"
    except UndeliverableRecipient:
        return "bad_address"
    except RuntimeError as exc:
        return f"not_configured:{exc}" if "not configured" in str(exc) else f"failed:{exc}"
    except Exception as exc:
        return f"failed:{type(exc).__name__}"


def send_kind(db: Session, kind: str, to: str, context: dict) -> str:
    subject, html, text = render(db, kind, context)
    return _send(to, subject, html, text)


def send_request_received(db: Session, *, to: str, requester_name: str, club_name: str) -> str:
    return send_kind(db, REQUEST_RECEIVED, to, {
        "requester_name": requester_name,
        "club_name": club_name,
    })


def send_club_live(
    db: Session, *, to: str, requester_name: str, club_name: str, club_url: str,
    is_admin: bool, systems: Optional[list] = None, club_night: Optional[str] = None,
) -> str:
    systems_text = ", ".join(systems or [])
    night = f" on {club_night}s" if club_night else ""
    return send_kind(db, CLUB_LIVE, to, {
        "requester_name": requester_name,
        "club_name": club_name,
        "club_url": club_url,
        "systems": systems_text,
        "club_night": club_night or "",
        "systems_line": (
            f"We've switched on {systems_text}{night} to save you a job. Change any of "
            "it from your admin — it's a starting point, not a decision."
            if systems_text else ""
        ),
        "admin_line": (
            "Sign in with the same Discord account and you'll arrive in your club's "
            "admin as its owner. There's a short checklist on the first screen — the "
            "one step worth doing before your next club night is connecting a Discord "
            "channel, so sign-ups and pairings post themselves."
            if is_admin else
            "Sign in with Discord to get started."
        ),
    })


def send_request_declined(
    db: Session, *, to: str, requester_name: str, club_name: str, reason: Optional[str] = None,
) -> str:
    return send_kind(db, REQUEST_DECLINED, to, {
        "requester_name": requester_name,
        "club_name": club_name,
        "reason": reason or "",
    })
