"""Interpret the offer identifier returned by Zakupay, including XML wrappers."""

UNCONFIRMED_OFFER_ERROR = "Ответ Закупай не подтверждает создание предложения"


def confirmed_offer_id(data, *, require_explicit_success=False):
    containers = []

    def visit(value):
        if not isinstance(value, dict):
            return
        containers.append(value)
        for key in ('response', 'data', 'offer', 'result'):
            visit(value.get(key))

    visit(data)
    flags = [str(value['result']).strip().lower() for value in containers
             if 'result' in value and not isinstance(value['result'], (dict, list))]
    if any(flag in {'false', '0'} for flag in flags):
        return None
    if require_explicit_success and not any(flag in {'true', '1'} for flag in flags):
        return None
    explicit_id = any('offerId' in value for value in containers)
    keys = ('offerId',) if require_explicit_success or explicit_id else ('id',)
    for key in keys:
        for value in containers:
            identifier = value.get(key)
            if isinstance(identifier, bool) or not isinstance(identifier, (int, str)):
                continue
            identifier = str(identifier).strip()
            if not identifier or identifier.lower() in {'0', 'false', 'none', 'null'}:
                continue
            if identifier.startswith('-') and identifier[1:].isdigit():
                continue
            return identifier
    return None
