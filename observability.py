"""Optional Sentry error reporting.

A complete no-op unless the SENTRY_DSN env var is set, so local dev and any
environment without the secret behave exactly as before (nothing is sent,
nothing is imported eagerly). Once a DSN is configured (Fly secret for the API,
GitHub secret for the crons), unhandled exceptions in the API and the scheduled
scripts are reported automatically, plus any error we explicitly capture() —
notably the Discord-webhook posts that are otherwise silently swallowed.

Set up: create a Sentry project, then
    fly secrets set SENTRY_DSN=<dsn> -a call-to-arms-api      # API
and add SENTRY_DSN to the GitHub repo secrets                 # crons
"""
import os

_initialised = False


def init_sentry(component: str) -> None:
    """Initialise Sentry if SENTRY_DSN is set; otherwise do nothing. Safe to
    call more than once. `component` tags events as "api" or "cron"."""
    global _initialised
    if _initialised:
        return
    dsn = os.environ.get("SENTRY_DSN")
    if not dsn:
        return

    import sentry_sdk

    sentry_sdk.init(
        dsn=dsn,
        environment=os.environ.get("SENTRY_ENVIRONMENT", "production"),
        # Errors only — no performance tracing, to keep quota and noise down.
        traces_sample_rate=0.0,
        # Never attach cookies / headers / client IP (would include the
        # cta_session auth cookie); we also drop request body/cookies below.
        send_default_pii=False,
        before_send=_scrub,
    )
    sentry_sdk.set_tag("component", component)
    _initialised = True


def _scrub(event, hint):
    """Defensively strip request body + cookies from events — they can carry
    player names / the session cookie, which we never want to ship."""
    req = event.get("request")
    if isinstance(req, dict):
        req.pop("data", None)
        req.pop("cookies", None)
    return event


def capture(exc: BaseException, **tags) -> None:
    """Report an otherwise-swallowed exception (e.g. a failed Discord webhook),
    with optional tags. No-op when Sentry isn't configured."""
    if not _initialised:
        return
    import sentry_sdk

    with sentry_sdk.new_scope() as scope:
        for k, v in tags.items():
            scope.set_tag(k, v)
        sentry_sdk.capture_exception(exc)
