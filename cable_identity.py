"""Cable identity is a mark, conductor count and conductor cross-section."""
import re


def identity(name):
    text = str(name or '')
    if not re.search(r'кабел\w*', text, re.I):
        return None
    mark = re.search(r'(?<![\w])(?:КГ(?:ТП)?(?:-ХЛ)?|А?ВВГ(?:НГ)?(?:\([АA]\))?(?:-?(?:FRLS|LS))?)(?![а-яa-z])', text, re.I)
    size = re.search(r'(?<![\d.,])(\d+)\s*[хx×*]\s*(\d+(?:[.,]\d+)?)(?![\d.,])', text, re.I)
    return dict(mark=mark[0].upper() if mark else None,
                cores=int(size[1]) if size else None,
                section=float(size[2].replace(',', '.')) if size else None,
                mark_evidence=mark[0] if mark else '', size_evidence=size[0] if size else '')


def conflicts(request, candidate):
    wanted = identity(request)
    if not wanted:
        return []
    offered = identity(candidate)
    if not offered:
        return ['не совпадает тип товара: требуется кабель']
    errors = []
    for key, label in [('mark','марка кабеля'),('cores','число жил'),('section','сечение жилы')]:
        if wanted[key] is not None and wanted[key] != offered[key]:
            errors.append(f'не совпадает {label}: {offered[key] or "не указано"} вместо {wanted[key]}')
    return errors


def reel_length(name, supplier_unit, requested_unit):
    def unit(value):
        return re.sub(r'[.\s]', '', str(value or '').lower())
    if not identity(name) or unit(requested_unit) not in {'м','мп','погм','метр'} or unit(supplier_unit) not in {'шт','штука','штуки','упак','упаковка','бухта'}:
        return None
    length = re.search(r'(?<![\d.,хx×*])(\d+(?:[.,]\d+)?)\s*(?:метров|метр(?:а)?|м)(?![\w²])', str(name), re.I)
    return float(length[1].replace(',', '.')) if length and float(length[1].replace(',', '.')) > 0 else None


def queries(name):
    cable = identity(name)
    if not cable or not cable['mark'] or cable['cores'] is None or cable['section'] is None:
        return []
    mark, cores, section = cable['mark'], cable['cores'], f"{cable['section']:g}"
    result = [f'кабель {mark} {cores}х{section}', f'{mark} {cores}x{section}', f'кабель {mark} {cores} х {section}']
    # Official product SKUs verified on the user-provided VI KG 3x4 tag page.
    # Only retrieval hints: prices/units/stock still come from the current API.
    if (mark, cores, section) == ('КГ', 3, '4'):
        result += ['43843283', '43844279', '43843499']
    return result
