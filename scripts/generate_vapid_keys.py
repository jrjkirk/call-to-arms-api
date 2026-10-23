"""Make the key pair push notifications are signed with (push.py).

Run once, by hand. It prints the two values and the command to store them as
Fly secrets. The private key is a secret: put it in Fly (and your local .env if
you want push in dev), never in chat, a commit or a ticket.

    PYTHONPATH=. python scripts/generate_vapid_keys.py

Changing the keys later unsubscribes every device (browsers tie a subscription
to the public key), so players would have to switch notifications on again.
"""
import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def main() -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    private = b64url(key.private_numbers().private_value.to_bytes(32, "big"))
    public = b64url(key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
    print("Run this to store them on Fly (it restarts the app once):\n")
    print(f"fly secrets set VAPID_PUBLIC_KEY={public} VAPID_PRIVATE_KEY={private} -a call-to-arms-api\n")
    print("For push in local dev too, add the same two lines to .env.")


if __name__ == "__main__":
    main()
