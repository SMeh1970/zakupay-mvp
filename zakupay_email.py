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
    order_items: tuple[dict, ...] = ()
    delivery_date: str = ""
    delivery_address: str = ""
    payment_terms: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[str] = []
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)
        elif tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(" ".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        value = " ".join(data.split())
        if value:
            self.parts.append(value)
            if self._cell is not None:
                self._cell.append(value)

    @property
    def text(self) -> str:
        return " ".join(self.parts)


def _message_text(message: Message) -> tuple[str, list[str], list[list[str]]]:
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
    return " ".join(plain_parts + [extractor.text]), extractor.links, extractor.rows


def _extract_order_items(rows: list[list[str]]) -> tuple[dict, ...]:
    items: list[dict] = []
    in_items = False
    for row in rows:
        normalized = [re.sub(r"\s+", " ", cell).strip() for cell in row]
        lowered = [cell.lower().replace("ё", "е") for cell in normalized]
        if len(lowered) >= 3 and lowered[0] == "наименование" and "кол-во" in lowered[1]:
            in_items = True
            continue
        if in_items and lowered and "требуемая дата поставки" in lowered[0]:
            break
        if not in_items or len(normalized) < 3:
            continue
        quantity_match = re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*", normalized[1])
        if not quantity_match:
            continue
        name = re.sub(r"\s*Комментарий:.*$", "", normalized[0], flags=re.IGNORECASE).strip()
        quantity = float(quantity_match.group(1).replace(",", "."))
        if quantity.is_integer():
            quantity = int(quantity)
        items.append({"goodName": name, "count": quantity, "unit": normalized[2]})
    return tuple(items)


def _extract_info(body: str, label: str, next_labels: tuple[str, ...]) -> str:
    stop = "|".join(re.escape(value) for value in next_labels)
    pattern = rf"{re.escape(label)}\s*(.*?)(?=\s*(?:{stop})\b|$)"
    match = re.search(pattern, body, re.IGNORECASE)
    return " ".join(match.group(1).split()) if match else ""


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
    body, links, rows = _message_text(message)
    order_id = _extract_order_id(subject, body, " ".join(links))
    order_items = _extract_order_items(rows)
    return ZakupayEmailEvent(
        event_type=_event_type(subject, body),
        order_id=order_id,
        subject=" ".join(subject.split()),
        sender=sender,
        order_items=order_items,
        delivery_date=_extract_info(
            body, "Требуемая дата поставки",
            ("Требуется доставка по адресу", "Требуется отсрочка платежа", "Действия по заявке"),
        ),
        delivery_address=_extract_info(
            body, "Требуется доставка по адресу",
            ("Требуется отсрочка платежа", "Действия по заявке"),
        ),
        payment_terms=_extract_info(
            body, "Требуется отсрочка платежа", ("Действия по заявке",),
        ),
    )
