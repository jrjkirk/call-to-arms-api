"""Fire a GitHub Actions `workflow_dispatch` from the running app.

Why the app pokes GitHub at all
-------------------------------
Two jobs need a machine that has matplotlib, which the 256 MB API image
deliberately does not carry (see scheduler.py). Rather than put a renderer in
the container, the app asks a GitHub runner to do it.

The important property: **GitHub throttles *scheduled* runs, not dispatched
ones.** A `cron:` workflow that asks for hourly actually lands about five times
a day at unpredictable times — that is what cost The Old World at EGNWGC its
02/09/2026 pairings, when the job's 21:00 fire time was missed by a run at
20:57 and nothing came back before midnight. A dispatch queues immediately, so
anything triggered this way happens in about a minute rather than "sometime".

Callers:
- `admin.pairings_post_discord` — the manual "Post to Discord" button.
- `scripts/run_auto_pairings_check` — when it has published pairings but is
  running somewhere it cannot render the image itself.
"""
import os
from typing import Optional

import httpx

GH_DISPATCH_TOKEN = os.environ.get("GH_DISPATCH_TOKEN", "")
GH_REPO = os.environ.get("GH_REPO", "jrjkirk/call-to-arms-api")
# Scheduled workflows only ever run on the default branch, so a dispatch that
# targeted anything else would behave differently from the cron backstop.
GH_DISPATCH_REF = os.environ.get("GH_DISPATCH_REF", "main")

PAIRINGS_IMAGE_WORKFLOW = "post-pairings-image.yml"
AUTO_PAIRINGS_WORKFLOW = "auto-pairings-check.yml"
LEAGUE_RANKINGS_WORKFLOW = "league-rankings-check.yml"


def dispatch_enabled() -> bool:
    """Whether a token is configured. Callers use this to tell "switched off"
    apart from "tried and failed" — the first is a deployment that never set
    GH_DISPATCH_TOKEN, and should not be reported as a fault."""
    return bool(GH_DISPATCH_TOKEN)


def dispatch_workflow(
    workflow_file: str, inputs: Optional[dict] = None
) -> tuple[bool, str]:
    """Queue one run of `workflow_file`. Returns (ok, reason-if-not).

    Never raises: every caller is either a user-facing button that wants a
    message to show, or a scheduled job where a failed dispatch must not take
    down the rest of the run. The cron backstop picks up anything a failed
    dispatch missed, so returning False here degrades timeliness, not
    correctness.
    """
    if not GH_DISPATCH_TOKEN:
        return False, "no dispatch token configured"

    url = f"https://api.github.com/repos/{GH_REPO}/actions/workflows/{workflow_file}/dispatches"
    payload: dict = {"ref": GH_DISPATCH_REF}
    if inputs:
        payload["inputs"] = inputs

    try:
        resp = httpx.post(
            url,
            headers={
                "Authorization": f"Bearer {GH_DISPATCH_TOKEN}",
                "Accept": "application/vnd.github+json",
            },
            json=payload,
            timeout=10.0,
        )
    except Exception:
        return False, "GitHub API request failed"

    if resp.status_code != 204:
        # GitHub puts the actual reason in the body — "Provided value ... is not
        # a valid option", "Required input not provided", "No ref found". The
        # bare status code sent an admin to the logs to find out that a system
        # name wasn't in a dropdown, so pass the message through.
        detail = ""
        try:
            detail = (resp.json() or {}).get("message", "")
        except Exception:
            pass
        return False, f"GitHub API returned {resp.status_code}" + (
            f": {detail}" if detail else ""
        )

    return True, ""
