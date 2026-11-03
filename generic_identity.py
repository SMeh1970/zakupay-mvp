"""Identity for requests whose product kind is not in product_identity.KINDS.

The kind list is closed. For everything else the request itself says what is needed: the main noun
(first noun of the name) and every token that carries a digit (size, grade, designation, model).
A candidate is comparable only if it has the same main noun and every one of those tokens; any token
it lacks becomes a pending difference for the operator (✓ accepts it, ✗ excludes the offer).

A request with the main noun but no digit token (for example "батарейки АА") says too little to be
verified from the name, so it is not covered here and keeps the blanket category confirmation.
"""
import re

_UNITS = r"мм|см|дм|м|км|мл|л|г|кг|т|вт|квт|в|а|ма|гц|бар|атм|мкм|шт|мпа|кпа"
_ADJECTIVE_END = re.compile(r"(?:ый|ий|ой|ая|яя|ое|ее|ые|ие|ого|его|ому|ему|ым|им|ых|их|ую|юю|ом|ем|ыми|ими)$")
# Nouns in -ние/-тие/-ция/-ствие (ограждение, покрытие, соединение) end like plural adjectives.
_NOUN_IE = re.compile(r"(?:ени|ани|стви|ти|ци)е$")
_SKIP = {"набор", "комплект", "изделие", "товар", "тип", "вид", "модель", "марка", "серия", "аналог", "эквивалент",
         "требуется", "упаковка", "штука", "размер", "цвет", "материал"}


def _clean(text):
    text = str(text or "").lower().replace("ё", "е")
    text = re.sub(r"[«»\"'“”()\[\],;:]", " ", text)
    text = text.replace("×", "x").replace("х", "x").replace("*", "x")
    text = re.sub(r"(?<=\d),(?=\d)", ".", text)
    # 8 мм -> 8мм, so that a value and its unit are one token.
    text = re.sub(r"(?<=\d)\s+(?=(?:" + _UNITS + r")(?![а-я]))", "", text)
    text = re.sub(r"(?<=\d)\s*x\s*(?=\d)", "x", text)
    return text


def head_noun(requested):
    for word in re.findall(r"(?<![a-z0-9а-я])[а-я]{4,}(?![a-z0-9а-я])", str(requested or "").lower().replace("ё", "е")):
        if word in _SKIP or (_ADJECTIVE_END.search(word) and not _NOUN_IE.search(word)):
            continue
        return word
    return None


def stem(word):
    return word[:max(4, len(word) - 2)]


def digit_tokens(requested):
    seen = []
    for token in _clean(requested).split():
        token = token.strip(".,-/")
        if len(token) < 1 or not re.search(r"\d", token) or token in seen:
            continue
        seen.append(token)
    return seen


def other_words(requested, noun):
    """The remaining descriptive words of the request, added one by one after the main noun."""
    words = []
    for word in re.findall(r"(?<![a-z0-9а-я])[а-я]{5,}(?![a-z0-9а-я])", str(requested or "").lower().replace("ё", "е")):
        if word != noun and word not in _SKIP and word not in words:
            words.append(word)
    return words


def identity(requested):
    """(main noun, digit tokens) or None when the request is not specific enough to verify by name."""
    noun, tokens = head_noun(requested), digit_tokens(requested)
    return (noun, tokens) if noun and tokens else None


def has_token(candidate_text, token):
    text = _clean(candidate_text)
    return bool(re.search(r"(?<![0-9a-zа-я.])" + re.escape(token) + r"(?![0-9a-zа-я]|\.\d)", text))


def has_noun(candidate_text, noun):
    return bool(re.search(r"(?<![а-я])" + re.escape(stem(noun)), str(candidate_text or "").lower().replace("ё", "е")))


def _shape(token):
    """(number of x-parts, unit) of a size token such as 8мм or 2050x3050; None for plain codes."""
    found = re.fullmatch(r"(\d+(?:\.\d+)?(?:x\d+(?:\.\d+)?)*)([a-zа-я]*)", token)
    if not found:
        return None
    numbers, unit = found.groups()
    parts = numbers.count("x") + 1
    return None if parts == 1 and not unit else (parts, unit)


def alternative(candidate_text, token):
    """The value an offer has instead of a requested size token: same kind of size and unit, other number.

    The offer's own value is a value found in its name, so it is offered for choice with that name as its source.
    """
    shape = _shape(token)
    if shape is None:
        return None
    for other in _clean(candidate_text).split():
        other = other.strip(".,-/")
        if other != token and _shape(other) == shape:
            return other
    return None

