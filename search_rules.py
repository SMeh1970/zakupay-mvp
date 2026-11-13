"""Deterministic rules for price-list search: sizes, product type, match levels.

No dependencies and no network. Decisions are recorded in the project plan
(section 9, 13.11.2026):

* sizes are compared as number pairs, whatever the separator (60*27, 60x27,
  60×27, 60х27, 60/27 are the same size);
* the product type is the first word of the name that is not an adjective, a
  number or a qualifier from QUALIFIERS (a profile that is not first does not
  make the first level);
* levels: 1 - type and every requested size confirmed; 2 - nothing contradicts
  but something is unspecified or the type is doubtful; 3 - one difference;
  4 - only the type matches (more than one difference).
"""
from __future__ import annotations

import re

# Qualifiers that look like nouns but never name the product by themselves.
# Collected from the Saturn price list; extend as the "not recognised" report
# shows new ones.
QUALIFIERS = {"алюм", "стандарт", "серебро", "сталь", "золото", "бронза"}
PREPOSITIONS = {"для", "под", "на", "с", "со", "по", "от", "к", "в", "из", "при", "без", "над"}

_ADJECTIVE_END = re.compile(r"(?:ый|ий|ой|ая|яя|ое|ые|ого|ему|ому|ым|им|ом|ую|юю|ых|их)$")
_OBLIQUE_ENDINGS = ("я", "ю", "ем", "ей", "ям", "ями", "ях", "ов", "ам", "ами", "ах", "ом", "у")
_SIZE = re.compile(
    r"(?<![0-9.,])(\d+(?:[.,]\d+)?)\s*[*×xXхХ/]\s*(\d+(?:[.,]\d+)?)(?:\s*[*×xXхХ/]\s*(\d+(?:[.,]\d+)?))?(?![0-9])")


def _words(text) -> list[str]:
    return re.findall(r"[a-zа-я0-9]+", str(text or "").lower().replace("ё", "е"))


def _num(value: str) -> str:
    value = value.replace(",", ".")
    if "." in value:
        value = value.rstrip("0").rstrip(".")
    return value.lstrip("0") or "0"


def sizes(text) -> list[tuple[str, ...]]:
    """All sizes written as 2-3 numbers joined by * × x х /, in canonical form."""
    return [tuple(_num(n) for n in match.groups() if n) for match in _SIZE.finditer(str(text or ""))]


def strip_sizes(text) -> str:
    """The text without sizes, so a size is never mistaken for a model or article."""
    return re.sub(r"\s+", " ", _SIZE.sub(" ", str(text or ""))).strip()


def size_text(size: tuple[str, ...]) -> str:
    return "х".join(size)


def size_tokens(text) -> set[str]:
    """Index spellings of the sizes in a text: Cyrillic and Latin 'x'."""
    result = set()
    for size in sizes(text):
        result.add("х".join(size))
        result.add("x".join(size))
    return result


def normalize_sizes(text) -> str:
    """Rewrite every size in a text as numbers joined by Cyrillic 'х'."""
    return _SIZE.sub(lambda m: size_text(tuple(_num(n) for n in m.groups() if n)), str(text or ""))


def _stem(word: str) -> str:
    if not re.fullmatch(r"[а-я]{5,}", word):
        return word
    for _ in range(2):
        if len(word) > 4 and word[-1] in "аяыиеоуюьй":
            word = word[:-1]
    return word


def _is_head_candidate(word: str) -> bool:
    return (len(word) >= 3 and re.fullmatch(r"[а-я]+", word) is not None
            and word not in QUALIFIERS and not _ADJECTIVE_END.search(word))


def product_type(text, brand=None) -> str | None:
    """The type word of a name: first word that is not a number, adjective,
    qualifier or brand (a brand is the given brand or a word in capitals,
    like «КНАУФ Профиль потолочный»)."""
    brand_words = set(_words(brand))
    for raw in re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", normalize_sizes(text)):
        word = raw.lower().replace("ё", "е")
        if len(raw) >= 3 and raw.isalpha() and raw.isupper():
            continue
        if word in brand_words:
            continue
        if _is_head_candidate(word):
            return word
    return None


def type_status(requested_type: str | None, name, brand=None) -> str:
    """'match' | 'doubtful' | 'other' for an offered name against the requested type.

    match    - the type is the head word of the name;
    doubtful - the type word stands elsewhere in the nominative (for example
               «Грильято 100х100 профиль»): second level, "type in question";
    other    - the type word is absent or only governs a different product
               («Соединитель профилей», «Сверло с профилем»): not shown.
    """
    if not requested_type:
        return "match"
    stem = _stem(requested_type)
    words = _words(normalize_sizes(name))
    head = product_type(name, brand)
    if head and _stem(head) == stem:
        return "match"
    own_tail = requested_type[len(stem):] if requested_type.startswith(stem) else ""
    for index, word in enumerate(words):
        if _stem(word) != stem or not word.startswith(stem):
            continue
        tail = word[len(stem):]
        if tail in _OBLIQUE_ENDINGS and tail != own_tail:
            continue
        if index and words[index - 1] in PREPOSITIONS:
            continue
        return "doubtful"
    return "other"


def compare_sizes(requested: list[tuple[str, ...]], offered_text) -> dict:
    """confirmed / missing / conflicts of requested sizes against an offered name."""
    offered = sizes(offered_text)
    confirmed, missing, conflicts = [], [], []
    for size in requested:
        if size in offered:
            confirmed.append(size)
        elif any(len(other) == len(size) for other in offered):
            conflicts.append(size)
        else:
            missing.append(size)
    return {"confirmed": confirmed, "missing": missing, "conflicts": conflicts}


def match_level(*, type_state: str, differences: int, missing: bool) -> int | None:
    """Level 1-4, or None when the item must not be shown at all."""
    if type_state == "other":
        return None
    if differences == 1:
        return 3
    if differences > 1:
        return 4
    if missing or type_state == "doubtful":
        return 2
    return 1


def shares_word(requested, name) -> bool:
    """False only when the request has real words and the name shares none of
    them (junk from a supplier API: «ремень», «сверло» for «профиль 60*27»).
    Requests without words (an article, numbers) are never gated."""
    stop = {"для", "под", "над", "при", "без", "шт", "мм", "см", "руб", "набор", "комплект", "упаковка"}
    wanted = {_stem(w) for w in _words(strip_sizes(requested))
              if re.fullmatch(r"[а-яa-z]{4,}", w) and w not in stop}
    if not wanted:
        return True
    offered = {_stem(w) for w in _words(name)}
    return bool(wanted & offered)
