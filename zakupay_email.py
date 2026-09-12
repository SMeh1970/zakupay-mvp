"""Parse inbound Zakupay notification emails without external dependencies."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from email import policy
from email.message import Message
from email.parser import BytesParser
from html.parser import HTMLParser


ORDER_ID_PATTERNS = (
    re.compile(r"\[\s*zakupay\s+id\s+(\d+)\s*\]", re.IGNORECASE),
    re.compile(r"заявк[аи]\s+(?:ID\s*)?(\d+)", re.IGNORECASE),
    re.compile(r"(?:[?&]|^)id=(\d+)(?:&|$)", re.IGNORECASE),
)


@dataclass(frozen=True)
class ZakupayEmailEvent:
    event_type: str
    order_id: int
    subject: str
    sender: str

    def to_dict(self) -> dict:
        return asdict(self)


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)

    def handle_data(self, data: str) -> None:
        value = " ".join(data.split())
        if value:
            self.parts.append(value)

    @property
    def text(self) -> str:
        return " ".join(self.parts)


def _message_text(message: Message) -> tuple[str, list[str]]:
    plain_parts: list[str] = []
    html_parts: list[str] = []
    for part in message.walk():
        if part.get_content_disposition() == "attachment":
            continue
        content_type = part.get_content_type()
        if content_type not in {"text/plain", "text/html"}:
            continue
        try:
            content = part.get_content()
        except (LookupError, UnicodeError):
            payload = part.get_payload(decode=True) or b""
            content = payload.decode("utf-8", errors="replace")
        if content_type == "text/html":
            html_parts.append(str(content))
        else:
            plain_parts.append(str(content))

    extractor = _HTMLTextExtractor()
    for fragment in html_parts:
        extractor.feed(fragment)
    return " ".join(plain_parts + [extractor.text]), extractor.links


def _extract_order_id(*values: str) -> int:
    for value in values:
        for pattern in ORDER_ID_PATTERNS:
            match = pattern.search(value or "")
            if match:
                return int(match.group(1))
    raise ValueError("Zakupay order ID was not found")


def _event_type(subject: str, body: str) -> str:
    normalized = f"{subject} {body}".lower().replace("ё", "е")
    if any(marker in normalized for marker in ("потеряло лидерство", "потеря лидерства", "предложение перебито")):
        return "leadership_lost"
    if any(marker in normalized for marker in ("автозапрос по заявке", "запрос предложения")):
        return "new_order"
    return "unknown"


def parse_zakupay_email(raw_email: bytes) -> ZakupayEmailEvent:
    """Return the stable routing fields from an RFC 5322 Zakupay email."""
    message = BytesParser(policy=policy.default).parsebytes(raw_email)
    subject = str(message.get("Subject", ""))
    sender = str(message.get("From", ""))
    body, links = _message_text(message)
    order_id = _extract_order_id(subject, body, " ".join(links))
    return ZakupayEmailEvent(
        event_type=_event_type(subject, body),
        order_id=order_id,
        subject=" ".join(subject.split()),
        sender=sender,
    )
