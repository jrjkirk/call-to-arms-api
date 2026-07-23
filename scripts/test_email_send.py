"""One-off manual check that Resend is wired up correctly end-to-end.
Run from repo root (needs PYTHONPATH to resolve the root-level `email` module):

    PYTHONPATH=. python scripts/test_email_send.py you@example.com
"""
import sys

from dotenv import load_dotenv

load_dotenv()

from emailer import send_email  # repo-root module, see docstring above


def main():
    if len(sys.argv) != 2:
        print("Usage: PYTHONPATH=. python scripts/test_email_send.py <to-address>")
        sys.exit(1)

    to = sys.argv[1]
    message_id = send_email(
        to=to,
        subject="Call to Arms — Resend test",
        html="<p>This is a test email from the Call to Arms API. "
             "If you're reading this, Resend is wired up correctly.</p>",
    )
    print(f"Sent. Resend message id: {message_id}")


if __name__ == "__main__":
    main()
