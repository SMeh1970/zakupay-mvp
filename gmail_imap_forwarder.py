"""Hourly Gmail IMAP forwarder for Zakupay messages.

Required environment variables:
  GMAIL_IMAP_EMAIL
  GMAIL_IMAP_APP_PASSWORD
  WEBHOOK_BEARER_TOKEN or ZAKUPAY_EMAIL_WEBHOOK_SECRET
Optional:
  WEBHOOK_URL (defaults to production ingest endpoint)
  START_AFTER (Unix epoch; defaults to 2026-09-14 00:00 Europe/Moscow)
"""

from __future__ import annotations

import email
import imaplib
import json
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
WEBHOOK_BEARER_TOKEN = os.getenv("WEBHOOK_BEARER_TOKEN", "").strip()
START_AFTER = int(os.getenv("START_AFTER", "1789333200"))
PROCESSED_LABEL = os.getenv("GMAIL_PROCESSED_LABEL", "ZakupayProcessedV2").strip()


def require_config() -> None:
    missing = []
    if not GMAIL_EMAIL:
        missing.append("GMAIL_IMAP_EMAIL")
    if not GMAIL_APP_PASSWORD:
        missing.append("GMAIL_IMAP_APP_PASSWORD")
    if not WEBHOOK_SECRET and not WEBHOOK_BEARER_TOKEN:
        missing.append("WEBHOOK_BEARER_TOKEN or ZAKUPAY_EMAIL_WEBHOOK_SECRET")
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


def post_message(raw_message: bytes) -> tuple[int, dict | None]:
    headers = {"Content-Type": "message/rfc822"}
    if WEBHOOK_BEARER_TOKEN:
        headers["Authorization"] = f"Bearer {WEBHOOK_BEARER_TOKEN}"
    else:
        headers["X-Webhook-Secret"] = WEBHOOK_SECRET
    request = urllib.request.Request(
        WEBHOOK_URL,
        data=raw_message,
        method="POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            status = response.status
            body = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        body = exc.read()
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        payload = None
    return status, payload


def accepted_result(status_code: int, payload: dict | None) -> bool:
    if not 200 <= status_code < 300 or not isinstance(payload, dict):
        return False
    return payload.get("status") in {
        "ready_for_review", "needs_review", "skipped_not_prepayment",
    }


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
        max_per_run = int(os.getenv("GMAIL_MAX_MESSAGES_PER_RUN", "5"))
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

            response_code, response_payload = post_message(raw_message)
            if accepted_result(response_code, response_payload):
                mailbox.uid(
                    "store",
                    uid,
                    "+X-GM-LABELS",
                    f'("{PROCESSED_LABEL}")',
                )
                forwarded += 1
                print(
                    "Accepted "
                    f"UID={uid.decode()} "
                    f"order={response_payload.get('result', {}).get('order_id')} "
                    f"job={response_payload.get('job_id')} "
                    f"status={response_payload.get('status')}"
                )
            else:
                failed += 1
                detail = (
                    response_payload.get("detail")
                    if isinstance(response_payload, dict)
                    else None
                )
                print(
                    f"Webhook failed for UID {uid.decode()}: "
                    f"HTTP {response_code}; detail={detail or 'unknown'}",
                    file=sys.stderr,
                )

    print(f"Forwarded={forwarded}; failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
