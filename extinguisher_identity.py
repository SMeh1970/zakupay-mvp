"""Explicit extinguisher designation; never infer missing safety classes."""
import re


def identity(text):
    text = str(text or '').upper()
    if not re.search(r'\bОГНЕТУШИТЕЛ\w*', text):
        return None
    model = re.search(r'(?<!\w)ОП\s*[-–]?\s*(\d+)(?!\d)', text)
    charging = re.search(r'\(\s*З\s*\)|ОП\s*[-–]?\s*\d+\s*/\s*З\b', text)
    classes = re.search(r'(?<![А-ЯA-Z])([АA][ВB][СC][ЕE])(?![А-ЯA-Z])', text)
    return dict(model=('ОП-' + str(int(model[1]))) if model else None,
                charging='Закачной' if charging else None,
                classes='АВСЕ' if classes else None)


def queries(text):
    item = identity(text)
    if not item or not item['model']:
        return []
    return list(dict.fromkeys([str(text).strip(),
        'Огнетушитель ' + item['model'] + (' АВСЕ' if item['classes'] else ''),
        'Огнетушитель ' + item['model']]))


def conflicts(requested, offered):
    wanted = identity(requested)
    if not wanted:
        return []
    found = identity(offered)
    if not found:
        return ['Не огнетушитель']
    return [label for key, label in [('model', 'Модель огнетушителя'),
        ('charging', 'Тип исполнения'), ('classes', 'Классы пожара')]
        if wanted[key] and found[key] and wanted[key] != found[key]]
