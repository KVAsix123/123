# -*- coding: utf-8 -*-
"""
Ядро генератора договоров — без UI.

Отсюда логику берут и окно на компьютере (generator_v2.py), и телеграм-бот
(bot_telegram.py). Ничего не печатает и не показывает диалогов: возвращает
данные либо бросает исключение с понятным текстом.
"""

import csv
import datetime
import json
import os
import re
import shutil
import subprocess

from docx import Document

from docx_zapolnenie import fill_document, find_unfilled, squeeze_empty_paragraphs_in_cells

try:
    from docx2pdf import convert as docx2pdf_convert
except ImportError:
    docx2pdf_convert = None

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(SCRIPT_DIR, "template11.docx")
WORD_DIR = os.path.join(SCRIPT_DIR, "11 Договоры Word")
PDF_DIR = os.path.join(SCRIPT_DIR, "11 Договоры PDF")
COUNTER_FILE = os.path.join(SCRIPT_DIR, "counter.json")
JOURNAL = os.path.join(SCRIPT_DIR, "журнал_договоров.csv")

MONTHS_RU = {1: "января", 2: "февраля", 3: "марта", 4: "апреля", 5: "мая", 6: "июня",
             7: "июля", 8: "августа", 9: "сентября", 10: "октября", 11: "ноября", 12: "декабря"}


def short_name(full_name):
    parts = str(full_name or "").strip().split()
    if len(parts) >= 3:
        return f"{parts[0]} {parts[1][0]}. {parts[2][0]}."
    if len(parts) == 2:
        return f"{parts[0]} {parts[1][0]}."
    return full_name or ""


def next_contract_number(today=None, counter_file=None):
    """
    Сквозная нумерация внутри дня. Счётчик дописывается, а не затирается,
    и сохраняется атомарно — не бьётся, если программа упадёт на записи.
    """
    counter_file = counter_file or COUNTER_FILE
    today = today or datetime.date.today()
    key = today.strftime("%d%m%y")
    data = {}
    if os.path.exists(counter_file):
        try:
            with open(counter_file, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = {k: int(v) for k, v in loaded.items() if str(v).isdigit()}
        except Exception:
            data = {}
    data[key] = data.get(key, 0) + 1
    if len(data) > 400:
        data = dict(sorted(data.items())[-400:])
    tmp = counter_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, counter_file)
    return f"2{key}{data[key]}", data[key]


def safe_filename(s):
    s = re.sub(r"[\\/:*?\"<>|]+", "_", str(s or "")).strip(" .")
    return s or "Без_имени"


def add_system_fields(data, today=None):
    today = today or datetime.date.today()
    d = dict(data)
    d["short_name"] = short_name(d.get("full_name"))
    d["day"] = f"{today.day:02d}"
    d["month"] = MONTHS_RU[today.month]
    d["year"] = str(today.year)[-2:]
    d["year_plus_3"] = str(today.year + 3)[-2:]
    d["date_full"] = f"{today.day:02d}.{today.month:02d}.{today.year}"
    return d


def to_pdf(docx_path, pdf_path):
    """docx2pdf (нужен Word), иначе LibreOffice headless. Возвращает (ok, сообщение)."""
    if docx2pdf_convert:
        try:
            docx2pdf_convert(docx_path, pdf_path)
            return True, ""
        except Exception as e:
            last = str(e)
    else:
        last = "библиотека docx2pdf не установлена"
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if soffice:
        try:
            subprocess.run([soffice, "--headless", "--convert-to", "pdf",
                            "--outdir", os.path.dirname(pdf_path), docx_path],
                           check=True, timeout=180,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            made = os.path.join(os.path.dirname(pdf_path),
                                os.path.splitext(os.path.basename(docx_path))[0] + ".pdf")
            if os.path.exists(made):
                if made != pdf_path:
                    os.replace(made, pdf_path)
                return True, ""
        except Exception as e:
            last = str(e)
    return False, last


def write_journal(row, path=None):
    path = path or JOURNAL
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        if new:
            w.writerow(["Дата", "Номер договора", "ФИО", "Телефон", "Email", "Файл"])
        w.writerow(row)


def build_contract(client_data, today=None, template=None, word_dir=None,
                   pdf_dir=None, counter_file=None, journal=None, make_pdf=True):
    """
    Создаёт договор из данных клиента.
    Возвращает {'number','docx','pdf','warning'}.
    Бросает исключение, если нет шаблона или остались незаполненные поля.
    """
    template = template or TEMPLATE
    word_dir = word_dir or WORD_DIR
    pdf_dir = pdf_dir or PDF_DIR

    if not os.path.exists(template):
        raise FileNotFoundError(f"Шаблон не найден: {template}")
    os.makedirs(word_dir, exist_ok=True)
    os.makedirs(pdf_dir, exist_ok=True)

    today = today or datetime.date.today()
    number, _ = next_contract_number(today, counter_file)
    data = add_system_fields(client_data, today)
    data["contract_num"] = number

    doc = Document(template)
    fill_document(doc, data)
    squeeze_empty_paragraphs_in_cells(doc)

    left = find_unfilled(doc)
    if left:
        raise ValueError("В договоре остались незаполненные поля: " + ", ".join(left))

    surname = safe_filename((data.get("full_name") or "Клиент").split()[0])
    base = f"Договор_{number}_{surname}"
    docx_path = os.path.join(word_dir, base + ".docx")
    pdf_path = os.path.join(pdf_dir, base + ".pdf")
    doc.save(docx_path)

    result = {"number": number, "docx": docx_path, "pdf": None, "warning": ""}
    if make_pdf:
        ok, msg = to_pdf(docx_path, pdf_path)
        if ok:
            result["pdf"] = pdf_path
        else:
            result["warning"] = f"PDF не создан ({msg}). Word-файл сохранён."

    write_journal([data["date_full"], number, data.get("full_name", ""),
                   data.get("phone", ""), data.get("email", "")  , docx_path], journal)
    return result
