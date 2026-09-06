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
        "body": (
            "Hi {requester_name},\n"
            "\n"
            "{club_name} is live on Call to Arms.\n"
            "\n"
            "{club_url}\n"
            "\n"
            "{systems_line}\n"
            "\n"
            "{admin_line}\n"
            "\n"
            "Anything at all, just reply — a person reads these."
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
            "If you think we've got this wrong, or something's changed, just reply to "
            "this email — we're happy to take another look."
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
    return _URL_RE.sub(lambda m: f'<a href="{m.group(0)}">{m.group(0)}</a>', escaped)


_SHELL = (
    '<div style="font-family:system-ui,sans-serif;font-size:15px;color:#111;'
    'line-height:1.5;max-width:520px">'
    "{body}"
    '<p style="color:#666;font-size:13px;margin-top:1.5rem">'
    "Call to Arms — club night, organised."
    "</p>"
    "</div>"
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
    return "".join(
        f"<p>{_linkify(esc(p)).replace(chr(10), '<br>')}</p>" for p in paragraphs if p
    )


def render(db: Session, kind: str, context: dict) -> tuple[str, str]:
    """(subject, html) for one email, using whatever template is in force."""
    tpl = get_template(db, kind)
    return _subject_safe(fill(tpl["subject"], context)), _SHELL.format(
        body=render_body(tpl["body"], context)
    )


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

def _send(to: str, subject: str, html: str) -> str:
    """Send, and say what happened instead of raising.

    Returns "sent", "bad_address", "not_configured:…" or "failed:…". Callers log
    or surface it; none of them should care enough to fail.
    """
    try:
        send_email(to=to, subject=subject, html=html)
        return "sent"
    except UndeliverableRecipient:
        return "bad_address"
    except RuntimeError as exc:
        return f"not_configured:{exc}" if "not configured" in str(exc) else f"failed:{exc}"
    except Exception as exc:
        return f"failed:{type(exc).__name__}"


def send_kind(db: Session, kind: str, to: str, context: dict) -> str:
    subject, html = render(db, kind, context)
    return _send(to, subject, html)


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
            "Sign in with Discord to get started. Reply to this email if you need admin "
            "access and we'll sort it."
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
