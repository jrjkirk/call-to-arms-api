"""Lightweight error alerting to a Discord channel.

Free and self-contained — no third-party service. A complete no-op unless
ALERTS_WEBHOOK_URL is set, so local dev and any environment without the secret
behave exactly as before. When set, unhandled API errors and otherwise-swallowed
failures (notably broken club Discord webhooks) post to that channel.

Set up: make a private #alerts channel, add a Discord webhook to it, then
    fly secrets set ALERTS_WEBHOOK_URL=<webhook-url> -a call-to-arms-api

Identical alerts are throttled (once per window) so a repeating error can't
spam the channel. Throttle state is in-process — fine here since prod runs a
single always-on machine; at worst a duplicate alert after a redeploy/restart.
"""
import os
import time
import traceback

_WINDOW_SECONDS = 600  # don't repeat the same alert within 10 minutes
_last_sent: dict[str, float] = {}


def _post(content: str) -> None:
    url = os.environ.get("ALERTS_WEBHOOK_URL")
    if not url:
        return
    try:
        import httpx

        httpx.post(url, json={"content": content[:1900]}, timeout=httpx.Timeout(10.0, connect=5.0))
    except Exception:
        # Alerting must never itself break anything.
        pass


def report(title: str, detail: str = "", **context) -> None:
    """Post an alert, throttling identical ones (same title + context)."""
    if not os.environ.get("ALERTS_WEBHOOK_URL"):
        return
    sig = title + "|" + "|".join(f"{k}={v}" for k, v in sorted(context.items()))
    now = time.time()
    last = _last_sent.get(sig)
    if last is not None and now - last < _WINDOW_SECONDS:
        return
    _last_sent[sig] = now

    content = f"🚨 **{title}**"
    if context:
        content += "\n" + " ".join(f"`{k}={v}`" for k, v in context.items())
    if detail:
        content += f"\n```\n{detail[:1500]}\n```"
    _post(content)


def capture(exc: BaseException, **context) -> None:
    """Report a caught, otherwise-swallowed exception (e.g. a failed webhook)."""
    report(f"{type(exc).__name__}: {exc}", **context)


def report_exception(exc: BaseException, **context) -> None:
    """Report an unhandled exception, with a short traceback for debugging."""
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    report(f"Unhandled {type(exc).__name__}: {exc}", detail=tb, **context)
