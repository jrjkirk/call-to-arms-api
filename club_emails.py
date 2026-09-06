"""The three emails a club organiser gets while being onboarded.

Until now they got none. A request went into silence and stayed there until
someone remembered to write by hand: no acknowledgement, no "you're live, here's
where to start", not even a note when a request was turned down. That silence
was the single biggest source of the back-and-forth onboarding used to need.

Design notes that matter more than the copy:

**Never break the thing that triggered them.** A club request must still be
recorded, and a provisioned club must still exist, if Resend is down or the
address was mistyped. Every function here swallows its own failures and reports
what happened for the caller to log, exactly like signups.py's webhook post.

**A mistyped address is not an incident.** emailer raises
UndeliverableRecipient specifically so ordinary bad user input can be told apart
from "our email is broken"; only the latter is worth alerting on.

**Everything interpolated is escaped** — see emailer.esc and CLAUDE.md. A club
name is whatever someone typed into a public form.
"""
from typing import Optional

from emailer import UndeliverableRecipient, esc, send_email

# Wrapped in one shell so the three read as coming from the same product, and so
# the styling lives in one place rather than three near-copies.
_SHELL = (
    '<div style="font-family:system-ui,sans-serif;font-size:15px;color:#111;'
    'line-height:1.5;max-width:520px">'
    "{body}"
    '<p style="color:#666;font-size:13px;margin-top:1.5rem">'
    "Call to Arms — club night, organised."
    "</p>"
    "</div>"
)


def _subject_safe(value: str, limit: int = 80) -> str:
    """A club name, fit for a subject line.

    Subjects are plain text and deliberately NOT html-escaped — entities in a
    subject line are their own bug (see CLAUDE.md). But a club name comes off a
    public form, so collapse anything that would break the line and cap the
    length: a subject is not the place for someone's 400-character joke, and
    newlines have no business in a header even though Resend takes JSON and so
    can't be header-injected.
    """
    flat = " ".join(str(value).split())
    return flat[: limit - 1] + "…" if len(flat) > limit else flat


def _send(to: str, subject: str, body: str) -> str:
    """Send, and say what happened instead of raising.

    Returns one of "sent", "bad_address", "not_configured", or "failed:...".
    Callers log it; none of them should care enough to fail.
    """
    try:
        send_email(to=to, subject=subject, html=_SHELL.format(body=body))
        return "sent"
    except UndeliverableRecipient:
        # Someone typed their address wrong on a public form. Ordinary, and not
        # worth waking anyone: the request is recorded either way.
        return "bad_address"
    except RuntimeError as exc:
        # Includes "email is not configured", which is a legitimate state for a
        # deployment that has never set RESEND_API_KEY.
        return f"not_configured:{exc}" if "not configured" in str(exc) else f"failed:{exc}"
    except Exception as exc:  # network, timeout, anything else
        return f"failed:{type(exc).__name__}"


def send_request_received(*, to: str, requester_name: str, club_name: str) -> str:
    """Acknowledge a club request the moment it lands.

    Says what happens next and roughly when, because the alternative — which is
    what we had — is a form that appears to do nothing and a person who emails
    two days later asking whether it worked.
    """
    body = (
        f"<p>Hi {esc(requester_name)},</p>"
        f"<p>Thanks for asking us to add <strong>{esc(club_name)}</strong> to Call to Arms. "
        "Your request is in and we'll look at it shortly — usually the same day.</p>"
        "<p>When it's approved you'll get a second email with your club's own web "
        "address. You won't need to set up an account: the Discord you signed in "
        "with is already linked, and you'll land straight in your club's admin.</p>"
        "<p>If you didn't make this request, you can ignore this — nothing has been "
        "created.</p>"
    )
    return _send(to, f"We've got your request for {_subject_safe(club_name)}", body)


def send_club_live(
    *,
    to: str,
    requester_name: str,
    club_name: str,
    club_url: str,
    is_admin: bool,
    systems: Optional[list] = None,
    club_night: Optional[str] = None,
) -> str:
    """Tell them the club exists, and give them the two things to do next.

    Deliberately short. The first-run checklist in the app is the real guide;
    this email only has to get them through the door and set the expectation
    that the checklist is where they finish the job.
    """
    enabled = ", ".join(esc(s) for s in (systems or []))
    lines = [
        f"<p>Hi {esc(requester_name)},</p>",
        f"<p><strong>{esc(club_name)}</strong> is live on Call to Arms.</p>",
        f'<p style="font-size:17px"><a href="{esc(club_url)}">{esc(club_url)}</a></p>',
    ]
    if enabled:
        night = f" on {esc(club_night)}s" if club_night else ""
        lines.append(
            f"<p>We've switched on {enabled}{night} to save you a job. "
            "Change any of it from your admin — it's a starting point, not a decision.</p>"
        )
    if is_admin:
        lines.append(
            "<p><strong>Sign in with the same Discord account</strong> and you'll arrive "
            "in your club's admin as its owner. There's a short checklist on the first "
            "screen — the one step worth doing before your next club night is connecting "
            "a Discord channel, so sign-ups and pairings post themselves.</p>"
        )
    else:
        lines.append(
            "<p>Sign in with Discord to get started. Reply to this email if you need "
            "admin access and we'll sort it.</p>"
        )
    lines.append("<p>Anything at all, just reply — a person reads these.</p>")
    return _send(to, f"{_subject_safe(club_name)} is live on Call to Arms", "".join(lines))


def send_request_declined(*, to: str, requester_name: str, club_name: str,
                          reason: Optional[str] = None) -> str:
    """Close the loop on a request we aren't taking forward.

    Sent because the alternative is someone waiting indefinitely on an answer
    that already exists. Kept warm and reversible: most declines are "not yet"
    or "we couldn't tell you run this club", both of which are fixable.
    """
    body = [
        f"<p>Hi {esc(requester_name)},</p>",
        f"<p>Thanks for your interest in Call to Arms. We're not able to set up "
        f"<strong>{esc(club_name)}</strong> at the moment.</p>",
    ]
    if reason:
        body.append(f"<p>{esc(reason)}</p>")
    body.append(
        "<p>If you think we've got this wrong, or something's changed, just reply to "
        "this email — we're happy to take another look.</p>"
    )
    return _send(to, f"About your Call to Arms request for {_subject_safe(club_name)}", "".join(body))
