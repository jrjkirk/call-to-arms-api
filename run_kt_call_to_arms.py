"""Entry point for GitHub Actions: post the weekly Kill Team Call to Arms.

Mirrors the original Streamlit app's post_kt_call_to_arms(). Static
flavour-text announcement with a sign-up link, posted every Tuesday.
No database access needed.
"""
import os
import httpx

DISCORD_KT_CALL_TO_ARMS_WEBHOOK_URL = os.environ.get("DISCORD_KT_CALL_TO_ARMS_WEBHOOK_URL", "")
APP_PUBLIC_URL = os.environ.get("APP_PUBLIC_URL", "")


def post_kt_call_to_arms(webhook_url: str | None = None, app_url: str | None = None) -> None:
    """webhook_url/app_url default to this module's env vars for the
    __main__ manual-run path; the scheduler passes a resolved per-club
    webhook instead."""
    webhook = webhook_url or DISCORD_KT_CALL_TO_ARMS_WEBHOOK_URL
    if not webhook:
        print("No KT call-to-arms webhook, skipping.")
        return

    signup_url = app_url or APP_PUBLIC_URL or "https://your-app-url"
    content = (
        "🔪 **Kill Team — Call to Arms** 🔪\n\n"
        "*\"In the cramped corridors and shattered ruins, elite operatives wage their secret wars. "
        "Quick, deadly, decisive — the perfect skirmish awaits.\"*\n\n"
        f"Friday's session is approaching. Sign up here: {signup_url}"
    )

    try:
        httpx.post(webhook, json={"content": content}, timeout=10)
        print("Posted KT Call to Arms.")
    except Exception as e:
        print(f"Failed to post KT Call to Arms: {e}")


if __name__ == "__main__":
    post_kt_call_to_arms()