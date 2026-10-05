# -*- coding: utf-8 -*-
"""
Корректная подстановка {{плейсхолдеров}} в .docx.

Зачем отдельный модуль: Word почти всегда режет текст абзаца на несколько «runs»
(из-за проверки орфографии, правок, смены языка). Плейсхолдер {{full_name}}
физически лежит в файле как, например, «{{full_» + «name}}». Простой поиск по
run.text его не находит, а запись paragraph.text затирает всё форматирование
абзаца. Здесь замена идёт по склеенному тексту абзаца с обратной раскладкой по
runs — форматирование первого run сохраняется.

Покрываются: тело документа, таблицы (включая вложенные), колонтитулы.

    from docx import Document
    from docx_zapolnenie import fill_document, find_unfilled

    doc = Document("template11.docx")
    fill_document(doc, client_data)
    missing = find_unfilled(doc)       # ['{{bank_bik}}'] — незаполненные
    doc.save("out.docx")
"""

import re

PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")


def _iter_paragraphs(container):
    """Все абзацы контейнера, включая таблицы и вложенные таблицы."""
    for p in getattr(container, "paragraphs", []):
        yield p
    for table in getattr(container, "tables", []):
        for row in table.rows:
            for cell in row.cells:
                for p in _iter_paragraphs(cell):
                    yield p


def iter_all_paragraphs(doc):
    """Тело + все колонтитулы всех секций."""
    for p in _iter_paragraphs(doc):
        yield p
    for section in doc.sections:
        for part in (section.header, section.footer,
                     section.first_page_header, section.first_page_footer,
                     section.even_page_header, section.even_page_footer):
            if part is None:
                continue
            for p in _iter_paragraphs(part):
                yield p


def replace_in_paragraph(paragraph, mapping):
    """
    Заменяет все {{ключи}} в абзаце, сохраняя форматирование.
    Возвращает True, если что-то заменили.
    """
    runs = paragraph.runs
    if not runs:
        return False
    full = "".join(r.text for r in runs)
    if "{{" not in full:
        return False

    def sub(m):
        key = m.group(1)
        if key not in mapping:
            return m.group(0)          # чужой плейсхолдер не трогаем
        val = mapping[key]
        return "" if val is None else str(val).strip()

    new_full = PLACEHOLDER.sub(sub, full)
    if new_full == full:
        return False

    # Раскладываем новый текст по runs: весь текст — в первый run,
    # остальные очищаем. Так сохраняется стиль начала абзаца.
    runs[0].text = new_full
    for r in runs[1:]:
        r.text = ""
    return True


def fill_document(doc, mapping):
    """Подставляет значения во всём документе. Возвращает число изменённых абзацев."""
    changed = 0
    for p in iter_all_paragraphs(doc):
        if replace_in_paragraph(p, mapping):
            changed += 1
    return changed


def find_unfilled(doc):
    """Плейсхолдеры, оставшиеся в документе после заполнения."""
    left = []
    for p in iter_all_paragraphs(doc):
        for m in PLACEHOLDER.finditer("".join(r.text for r in p.runs)):
            if m.group(0) not in left:
                left.append(m.group(0))
    return left


def squeeze_empty_paragraphs_in_cells(doc):
    """
    Убирает пустые абзацы в ячейках таблиц, но ВСЕГДА оставляет минимум один:
    ячейка без абзаца делает файл невалидным, и Word ругается при открытии.
    """
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                paragraphs = list(cell.paragraphs)
                empties = [p for p in paragraphs if not p.text.strip()]
                if len(empties) == len(paragraphs):
                    empties = empties[1:]          # оставляем один
                for p in empties:
                    el = p._element
                    if el.getparent() is not None:
                        el.getparent().remove(el)
