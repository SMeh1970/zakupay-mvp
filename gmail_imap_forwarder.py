"""Hourly Gmail IMAP forwarder for Zakupay messages.

Required environment variables:
  GMAIL_IMAP_EMAIL
  GMAIL_IMAP_APP_PASSWORD
  ZAKUPAY_EMAIL_WEBHOOK_SECRET
Optional:
  WEBHOOK_URL (defaults to production ingest endpoint)
  START_AFTER (Unix epoch; defaults to 2026-09-14 00:00 Europe/Moscow)
"""

from __future__ import annotations

import email
import imaplib
import os
import sys
import urllib.error
import urllib.request
from email.policy import default
from email.utils import parsedate_to_datetime

GMAIL_HOST = os.getenv("GMAIL_IMAP_HOST", "imap.gmail.com")
GMAIL_EMAIL = os.getenv("GMAIL_IMAP_EMAIL", "").strip()
GMAIL_APP_PASSWORD = os.getenv("GMAIL_IMAP_APP_PASSWORD", "").replace(" ", "")
WEBHOOK_URL = os.getenv(
    "WEBHOOK_URL",
    "https://zakupay-mvp.onrender.com/automation/email/ingest",
).strip()
WEBHOOK_SECRET = os.getenv("ZAKUPAY_EMAIL_WEBHOOK_SECRET", "").strip()
START_AFTER = int(os.getenv("START_AFTER", "1789333200"))
PROCESSED_LABEL = "ZakupayProcessed"


def require_config() -> None:
    missing = []
    if not GMAIL_EMAIL:
        missing.append("GMAIL_IMAP_EMAIL")
    if not GMAIL_APP_PASSWORD:
        missing.append("GMAIL_IMAP_APP_PASSWORD")
    if not WEBHOOK_SECRET:
        missing.append("ZAKUPAY_EMAIL_WEBHOOK_SECRET")
    if missing:
        raise RuntimeError("Missing environment variables: " + ", ".join(missing))


def message_is_new_enough(raw_message: bytes) -> bool:
    parsed = email.message_from_bytes(raw_message, policy=default)
    date_header = parsed.get("Date")
    if not date_header:
        return True
    try:
        return int(parsedate_to_datetime(date_header).timestamp()) >= START_AFTER
    except (TypeError, ValueError, OverflowError):
        return True


def post_message(raw_message: bytes) -> int:
    request = urllib.request.Request(
        WEBHOOK_URL,
        data=raw_message,
        method="POST",
        headers={
            "Content-Type": "message/rfc822",
            "X-Webhook-Secret": WEBHOOK_SECRET,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def main() -> int:
    require_config()
    forwarded = 0
    failed = 0

    with imaplib.IMAP4_SSL(GMAIL_HOST, 993) as mailbox:
        mailbox.login(GMAIL_EMAIL, GMAIL_APP_PASSWORD)
        status, _ = mailbox.select("INBOX")
        if status != "OK":
            raise RuntimeError("Could not select Gmail INBOX")

        query = (
            f'from:zakupay@sel-be.ru after:{START_AFTER} '
            f'-label:{PROCESSED_LABEL}'
        )
        status, data = mailbox.uid("search", None, "X-GM-RAW", f'"{query}"')
        if status != "OK":
            raise RuntimeError("Gmail search failed")

        uids = data[0].split() if data and data[0] else []
        max_per_run = int(os.getenv("GMAIL_MAX_MESSAGES_PER_RUN", "50"))
        uids = uids[:max_per_run]
        for uid in uids:
            status, parts = mailbox.uid("fetch", uid, "(RFC822)")
            if status != "OK" or not parts:
                failed += 1
                continue

            raw_message = next(
                (
                    part[1]
                    for part in parts
                    if isinstance(part, tuple) and isinstance(part[1], bytes)
                ),
                None,
            )
            if not raw_message or not message_is_new_enough(raw_message):
                continue

            response_code = post_message(raw_message)
            if 200 <= response_code < 300:
                mailbox.uid(
                    "store",
                    uid,
                    "+X-GM-LABELS",
                    f'("{PROCESSED_LABEL}")',
                )
                forwarded += 1
            else:
                failed += 1
                print(
                    f"Webhook failed for UID {uid.decode()}: HTTP {response_code}",
                    file=sys.stderr,
                )

    print(f"Forwarded={forwarded}; failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
