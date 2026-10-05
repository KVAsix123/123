# -*- coding: utf-8 -*-
"""
Парсер данных клиента для генератора договоров.

Главная функция:
    data, issues = parse_client(raw_text)

Три слоя:
  1. Регулярки   — быстро, бесплатно, офлайн, покрывают большинство случаев.
  2. Валидация   — контрольные суммы БИК / счетов / ИНН / СНИЛС, формат телефона и e-mail.
  3. ИИ          — добирает поля, которые не нашли регулярки, и читает фотографии
                   документов. Провайдер настраивается в llm_config.json
                   (DeepSeek, Gemini, Ollama и т.д.); без него модуль работает офлайн.

Модуль ничего не печатает и не показывает окон — возвращает словарь и список замечаний,
чтобы GUI сам решил, что подсветить.

Проверить из консоли:  python parser_klienta.py текст_клиента.txt
"""

import json
import os
import re

# --- поля шаблона -----------------------------------------------------------

FIELD_LABELS = {
    "full_name":            "ФИО полностью",
    "passport_series":      "Серия паспорта",
    "passport_number":      "Номер паспорта",
    "passport_issued_by":   "Кем выдан (с датой и кодом)",
    "registration_address": "Адрес регистрации",
    "phone":                "Телефон",
    "email":                "Email",
    "bank_account":         "Расчётный счёт",
    "bank_name":            "Наименование банка",
    "bank_corr_account":    "Корр. счёт",
    "bank_bik":             "БИК",
}
FIELDS = list(FIELD_LABELS)

# поля, без которых договор выпускать нельзя
CRITICAL = ("full_name", "passport_series", "passport_number", "registration_address")

# Мини-справочник БИК -> (банк, корр. счёт). Пары проверены контрольным ключом ЦБ.
# Расширяется из справочника БИК ЦБ РФ (cbr.ru) или из файла bik.json рядом со скриптом.
BIK_BOOK = {
    "044525225": ("ПАО Сбербанк", "30101810400000000225"),
    "044030653": ("Северо-Западный банк ПАО Сбербанк", "30101810500000000653"),
    "044525593": ("АО «Альфа-Банк»", "30101810200000000593"),
    "044525974": ("АО «ТБанк»", "30101810145250000974"),
    "044525187": ("Банк ВТБ (ПАО)", "30101810700000000187"),
    "044525700": ("АО «Райффайзенбанк»", "30101810200000000700"),
    "044525823": ("Банк ГПБ (АО)", "30101810200000000823"),
    "044525360": ("ПАО «Совкомбанк»", "30101810445250000360"),
    "044525256": ("ПАО РОСБАНК", "30101810000000000256"),
    "044525214": ("АО «Почта Банк»", "30101810245250000214"),
    "044525659": ("ПАО «МОСКОВСКИЙ КРЕДИТНЫЙ БАНК»", "30101810745250000659"),
    "044525999": ("Точка ПАО Банк «ФК Открытие»", "30101810845250000999"),
    "044525266": ("АО «Банк ДОМ.РФ»", "30101810345250000266"),
    "044525092": ("ООО «Банк Точка»", "30101810645250000092"),
    "046577674": ("Уральский банк ПАО Сбербанк", "30101810500000000674"),
    "040349602": ("Юго-Западный банк ПАО Сбербанк", "30101810100000000602"),
}


class Issue:
    """Замечание по полю. level: 'error' | 'warn' | 'info'."""

    def __init__(self, level, field, message):
        self.level = level
        self.field = field
        self.message = message

    def __repr__(self):
        return f"[{self.level}] {FIELD_LABELS.get(self.field, self.field)}: {self.message}"


# --- контрольные суммы ------------------------------------------------------

def digits(s):
    return re.sub(r"\D", "", str(s or ""))


def check_bik(bik):
    """БИК РФ: 9 цифр, первые две — код страны 04."""
    b = digits(bik)
    if len(b) != 9:
        return False, "должно быть 9 цифр"
    if not b.startswith("04"):
        return False, "российский БИК начинается с 04"
    return True, ""


_W = [7, 1, 3] * 8  # веса ЦБ РФ, используются первые 23


def _account_key_ok(prefix, account):
    s = prefix + account
    if len(s) != 23 or not s.isdigit():
        return False
    return sum((int(d) * w) % 10 for d, w in zip(s, _W)) % 10 == 0


def check_account(account, bik, is_corr=False):
    """
    Контрольный ключ счёта по алгоритму ЦБ РФ.
    Расчётный счёт проверяется с префиксом = последние 3 цифры БИК,
    корреспондентский — с префиксом '0' + 5-6-я цифры БИК.
    """
    acc, b = digits(account), digits(bik)
    if len(acc) != 20:
        return False, "должно быть 20 цифр"
    if len(b) != 9:
        return False, "нельзя проверить ключ: не задан корректный БИК"
    if is_corr:
        if not acc.startswith("301"):
            return False, "корр. счёт должен начинаться с 301"
        prefix = "0" + b[4:6]
    else:
        if acc.startswith("301"):
            return False, "это похоже на корр. счёт, а не на расчётный"
        prefix = b[6:9]
    if not _account_key_ok(prefix, acc):
        return False, "не сходится контрольный ключ — опечатка в счёте или БИК"
    return True, ""


def check_inn(inn):
    n = digits(inn)

    def ctrl(seq, weights):
        return sum(int(d) * w for d, w in zip(seq, weights)) % 11 % 10

    if len(n) == 10:
        ok = ctrl(n[:9], [2, 4, 10, 3, 5, 9, 4, 6, 8]) == int(n[9])
    elif len(n) == 12:
        ok = (ctrl(n[:10], [7, 2, 4, 10, 3, 5, 9, 4, 6, 8]) == int(n[10])
              and ctrl(n[:11], [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]) == int(n[11]))
    else:
        return False, "ИНН должен быть 10 или 12 цифр"
    return (True, "") if ok else (False, "не сходится контрольная цифра")


def check_snils(snils):
    n = digits(snils)
    if len(n) != 11:
        return False, "СНИЛС должен быть 11 цифр"
    total = sum(int(d) * (9 - i) for i, d in enumerate(n[:9]))
    ctrl = total if total < 100 else (0 if total in (100, 101) else total % 101)
    ctrl = 0 if ctrl in (100, 101) else ctrl
    return (True, "") if ctrl == int(n[9:]) else (False, "не сходится контрольное число")


# --- регулярки --------------------------------------------------------------

RE_EMAIL = re.compile(r"[\w.%+\-]+@[\w\-]+(?:\.[\w\-]+)*\.[^\W\d_]{2,}", re.UNICODE)
RE_PHONE = re.compile(r"(?:\+?7|8)[\s\-.()]*(\d{3})[\s\-.()]*(\d{3})[\s\-.]*(\d{2})[\s\-.]*(\d{2})(?!\d)")
RE_SUBDIV = re.compile(r"\b(\d{3})\s*-\s*(\d{3})\b")
RE_DATE = re.compile(r"\b\d{2}[.\-/]\d{2}[.\-/]\d{4}\b")
RE_ACC20 = re.compile(r"\b\d{20}\b")
RE_BIK = re.compile(r"\b04\d{7}\b")
RE_INN = re.compile(r"ИНН[^\d]{0,10}(\d{10,12})", re.I)
RE_SNILS = re.compile(r"\b\d{3}-\d{3}-\d{3}[\s-]\d{2}\b")
# Номер карты: 16 цифр, возможно группами по 4. Сам номер никуда не
# записывается — для договора он не нужен, фиксируем только факт, что прислали
# карту вместо счёта.
RE_CARD = re.compile(r"(?<!\d)(?:\d{4}[ \-]?){3}\d{4}(?!\d)")

_PATRON = r"(?:вич|вна|ична|оглы|кызы|уулу)"
RE_FIO3 = re.compile(
    r"\b([А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?)\s+([А-ЯЁ][а-яё]+)\s+([А-ЯЁ][а-яё]*" + _PATRON + r")\b")
RE_FIO3_ALT = re.compile(  # порядок «Имя Отчество Фамилия»
    r"\b([А-ЯЁ][а-яё]+)\s+([А-ЯЁ][а-яё]*" + _PATRON + r")\s+([А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?)\b")
RE_FIO2 = re.compile(r"^\s*([А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?)\s+([А-ЯЁ][а-яё]+)\s*$", re.M)

# где заканчивается свободнотекстовое поле
_STOP = (r"(?=\n|$|\bзарегистр|\bпропис|\bадрес|\bместо жительства|\bживу|\bпроживаю"
         r"|\bтел\b|\bтелефон|\bмоб\b|\bпочта|\be-?mail|\bбанк|\bБИК\b|\bр/с|\bк/с"
         r"|\bрасч[ёе]тный|\bкорр|\bИНН|\bСНИЛС|\bпаспорт"
         r"|\b\d{6}\b\s*,?\s*(?:г\.|гор\.|обл\.|респ|пос\.|с\.|д\.|[А-ЯЁ]))")

RE_ADDR_HINT = re.compile(r"(?:\bг\.|\bгор\.|\bобл\.|\bул\.|\bулица|\bпр-кт|\bпроспект|\bд\.|\bкв\.|\bресп)", re.I)


def normalize_phone(raw):
    m = RE_PHONE.search(raw or "")
    if not m:
        return ""
    a, b, c, d = m.groups()
    return f"+7 ({a}) {b}-{c}-{d}"


def _clean(s):
    return re.sub(r"\s+", " ", (s or "")).strip(" ,;:.—-")


def _after(text, *markers):
    """Текст после маркера до ближайшего следующего поля."""
    for mk in markers:
        m = re.search(mk + r"\s*[:\-]?\s*(.{3,200}?)" + _STOP, text, re.I | re.S)
        if m:
            got = _clean(m.group(1))
            if got:
                return got
    return ""


# --- слой 1: регулярки ------------------------------------------------------

RE_ADDR_PART = re.compile(
    r"(?:\bКРАЙ\b|\bОБЛ|\bРЕСП|\bОКРУГ\b|\bРАЙОН\b|\bР-?Н\b|\bГОРОД\b|\bГ\.|\bГОР\.|"
    r"\bПУНКТ\b|\bПОС\b|\bСЕЛО\b|\bС\.|\bУЛ\.|\bУЛИЦА\b|\bПР-КТ|\bПРОСПЕКТ\b|\bПЕР\.|"
    r"\bШОССЕ\b|\bБУЛЬВАР\b|\bНАБ\.|\bД\.|\bДОМ\b|\bКВ\.|\bКОРП|\bСТР\.|\b\d{6}\b)", re.I)

# подписи полей на странице регистрации — их в адрес тащить не надо
RE_ADDR_LABEL = re.compile(r"^\s*(?:Рег-?н|Регион|Район|Пункт|Улица|Дом|Кв|Адрес)\s*:\s*", re.I)


# Слова-паразиты, которые распознавалка любит приклеивать к названию банка
RE_BANK_JUNK = re.compile(r"^(?:получател[ьяею]\w*|банка?|реквизит\w*|наименовани\w*)"
                          r"[\s:,\-]*", re.I)

RE_JUNK_CHARS = re.compile(r"[{}\[\]|~^`\\<>§№*]")
RE_STRONG_ADDR = re.compile(
    r"(?:\bКРАЙ\b|\bОБЛ|\bРЕСП|\bОКРУГ\b|\bГОРОД\b|\bГ\.|\bУЛ\b|\bУЛИЦА\b|\bПР-?[ДК]|"
    r"\bПРОСПЕКТ\b|\bПЕР\b|\bШОССЕ\b|\bДОМ\b|\bД\.|\bКВ\b|\bКВАРТИРА\b|\bКОРП)", re.I)


def _case_switches(word):
    return sum(1 for a, b in zip(word, word[1:]) if a.isupper() != b.isupper())


def looks_garbled(text, need_addr_markers=0):
    """
    Похоже ли на кашу после распознавания.

    В нормальном русском слове регистр меняется максимум один раз — после первой
    буквы. «КвАБаЗНТЗЙСкИЙ» переключается четыре раза, и таких слов в живом
    тексте не бывает. Плюс смотрим на мусорные символы и на то, есть ли в строке
    вообще признаки адреса.

    Лучше оставить поле пустым и сказать об этом, чем подставить нечитаемое
    в договор, который человек подпишет.
    """
    t = (text or "").strip()
    if not t:
        return False
    words = re.findall(r"[А-Яа-яЁё]{4,}", t)
    weird = sum(1 for w in words if _case_switches(w) >= 2)
    junk = len(RE_JUNK_CHARS.findall(t))

    if weird >= 2 or junk >= 2:
        return True
    if weird and junk:
        return True
    if need_addr_markers and len(RE_STRONG_ADDR.findall(t)) < need_addr_markers:
        return True
    return False


RE_CAPS_WORD = re.compile(r"^[А-ЯЁ][А-ЯЁ\-]{1,24}$")


def _titlecase(w):
    return "-".join(part.capitalize() for part in w.split("-"))


def _fio_from_caps_lines(text):
    """
    На странице паспорта фамилия, имя и отчество напечатаны ЗАГЛАВНЫМИ и каждое
    на своей строке. Ищем подряд идущие такие строки, последняя из которых
    похожа на отчество, и склеиваем в «Фамилия Имя Отчество».
    """
    lines = [l.strip(" .,:;") for l in (text or "").splitlines()]
    run = []
    for line in lines + [""]:
        if RE_CAPS_WORD.match(line):
            run.append(line)
            continue
        if len(run) >= 3:
            for i in range(len(run) - 2):
                a, b, c = run[i], run[i + 1], run[i + 2]
                if re.search(r"(?:ВИЧ|ВНА|ИЧНА|ОГЛЫ|КЫЗЫ)$", c):
                    return " ".join(_titlecase(x) for x in (a, b, c))
        if len(run) == 2 and re.search(r"(?:ВИЧ|ВНА|ИЧНА)$", run[1]):
            return " ".join(_titlecase(x) for x in run)
        run = []
    return ""


def _collect_address(text):
    """
    На странице регистрации адрес разбит на отдельные строки
    («КРАЙ СТАВРОПОЛЬСКИЙ» / «Г. СТАВРОПОЛЬ» / «УЛ. ... Д. ... КВ. ...»).
    Склеиваем идущие подряд такие строки в один адрес и берём самую полную цепочку.
    """
    runs, cur = [], []
    stop = re.compile(r"(?:зарегистрирован|выдан|отдел\s+по\s+вопросам|наименование|заверил|"
                      r"место\s+жительства|паспорт|подразделени|миграц|полици|умвд|мвд)", re.I)
    for raw in (text or "").splitlines():
        line = RE_ADDR_LABEL.sub("", raw).strip(" ,;.")
        if not line or stop.search(line) or len(line) > 120:
            if cur:
                runs.append(cur)
            cur = []
            continue
        hits = len(RE_ADDR_PART.findall(line))
        # продолжение адреса: название улицы само по себе меток не содержит
        # («СЕВЕРНЫЙ ВЛАСИХИНСКИЙ» после «ПР-Д»), поэтому внутри начатой цепочки
        # принимаем и строки без меток — лишь бы это был текст, а не дата
        cont = bool(cur) and not re.fullmatch(r"[\d.\-/\s]+", line)
        if (hits or cont) and len(line) > 2:
            cur.append(_clean(line))
        else:
            if cur:
                runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    if not runs:
        return ""
    best = max(runs, key=lambda r: (len(RE_ADDR_PART.findall(" ".join(r))), len(" ".join(r))))
    return _clean(", ".join(best))


def parse_regex(text):
    """
    Сначала вынимаем все однозначно опознаваемые токены (телефон, почта, счета,
    БИК, паспорт, даты), затем вырезаем их из текста и по остатку разбираем
    свободнотекстовые поля — ФИО, «кем выдан», адрес, банк.
    """
    d = {}
    t = text or ""

    # --- однозначные токены ---
    ph = normalize_phone(t)
    if ph:
        d["phone"] = ph
    em = RE_EMAIL.search(t)
    if em:
        d["email"] = em.group(0).lower()
    bik = RE_BIK.search(t)
    if bik:
        d["bank_bik"] = bik.group(0)
    for acc in RE_ACC20.findall(t):
        if acc.startswith("301"):
            d.setdefault("bank_corr_account", acc)
        else:
            d.setdefault("bank_account", acc)
    inn = RE_INN.search(t)
    if inn:
        d["inn"] = inn.group(1)
    sn = RE_SNILS.search(t)
    if sn:
        d["snils"] = sn.group(0)

    m = re.search(r"(?:паспорт|сери[яи])[^\d\n]{0,30}(\d{2}\s?\d{2})[^\d\n]{0,15}(\d{6})(?!\d)", t, re.I)
    if not m:
        m = re.search(r"(?<!\d)(\d{2}\s\d{2})\s*(?:№|N|#|No)?\s*(\d{6})(?!\d)", t)
    if not m:
        m = re.search(r"(?<!\d)(\d{4})\s+(?:№|N|#|No)?\s*(\d{6})(?!\d)", t)
    if m:
        d["passport_series"] = digits(m.group(1))
        d["passport_number"] = m.group(2)

    date_m = RE_DATE.search(t)
    sub_m = RE_SUBDIV.search(t)

    # --- маскируем найденное, чтобы оно не попало в свободные поля ---
    masked = t
    for pat in (RE_PHONE, RE_EMAIL, RE_ACC20, RE_BIK, RE_SNILS):
        masked = pat.sub("\n", masked)
    if m:
        for grp in (m.group(1), m.group(2)):      # только цифры, иначе затрём ФИО рядом
            masked = masked.replace(grp, "\n", 1)
    masked = RE_INN.sub("\n", masked)

    # --- ФИО ---
    fm = RE_FIO3.search(masked)
    if fm:
        d["full_name"] = " ".join(fm.groups())
    else:
        fm = RE_FIO3_ALT.search(masked)
        if fm:  # переставляем в «Фамилия Имя Отчество»
            name, patr, surname = fm.groups()
            d["full_name"] = f"{surname} {name} {patr}"
        else:
            labeled = _after(masked, r"ФИО", r"Ф\.?И\.?О")
            if labeled and 1 < len(labeled.split()) <= 3:
                d["full_name"] = labeled
            else:
                caps = _fio_from_caps_lines(masked)   # страница паспорта: ФИО заглавными
                if caps:
                    d["full_name"] = caps
                else:
                    fm = RE_FIO2.search(masked)       # строка ровно из двух слов
                    if fm:
                        d["full_name"] = " ".join(fm.groups())

    # --- кем выдан (+ дата и код подразделения) ---
    issued = _after(masked, r"кем выдан", r"выдан(?:ный)?")
    if issued:
        parts = [issued]
        if date_m and date_m.group(0) not in issued:
            parts.append(date_m.group(0))
        if sub_m and digits(sub_m.group(0)) not in digits(issued):
            parts.append(f"код подразделения {sub_m.group(1)}-{sub_m.group(2)}")
        d["passport_issued_by"] = ", ".join(parts)

    # --- адрес регистрации ---
    addr = _after(masked, r"адрес регистрации", r"зарегистрирован(?:а|ный)?(?:\s+по\s+адресу)?",
                  r"прописк[аи]", r"прописан(?:а)?", r"рег\.", r"место жительства",
                  r"адрес", r"живу", r"проживаю")
    if addr and not RE_ADDR_PART.search(addr):
        addr = ""                                   # поймали дату или мусор, не адрес
    if not addr:
        for chunk in re.split(r"[\n\r]+", masked):
            idx = re.search(r"\b\d{6}\b.*", chunk)       # адрес начинается с индекса
            if idx and RE_ADDR_HINT.search(idx.group(0)):
                addr = _clean(idx.group(0))
                break
    if not addr:
        addr = _collect_address(masked)             # адрес, разнесённый по строкам
    if not addr:
        for chunk in re.split(r"[\n\r]+", masked):
            if len(RE_ADDR_HINT.findall(chunk)) >= 2:
                addr = _clean(chunk)
                break
    if addr:
        addr = re.sub(r"^(?:в|по адресу|адресу)\s+", "", _clean(addr), flags=re.I)
        addr = _clean(re.split(r"\s*(?:тел|e-?mail|почт)", addr, flags=re.I)[0])
        addr = re.sub(r"^[\d.\-/\s]*(?=[А-ЯЁа-яё])", "", addr)      # срезаем дату регистрации
        if looks_garbled(addr, need_addr_markers=2):
            d["_addr_garbled"] = "1"                               # в договор не пускаем
        else:
            d["registration_address"] = addr
    elif re.search(r"(?:ЗАРЕГИСТРИРОВАН|МЕСТО\s+ЖИТЕЛЬСТВА)", masked, re.I):
        # страницу регистрации видно, а адрес с неё не собрался — значит каша
        d["_addr_garbled"] = "1"

    # --- банк ---
    bank = _after(masked, r"наименование банка", r"банк[\s\-]*получател[ьяею]\w*",
                  r"банк[\s\-]*плательщика", r"банк")
    if not bank:
        for line in masked.splitlines():
            if re.search(r"(ПАО|АО|ООО|Банк|БАНК)", line) and len(line) < 120:
                bank = _clean(line)
                break
    if bank:
        bank = RE_BANK_JUNK.sub("", _clean(bank))                  # «получатель:» — не банк
        bank = _clean(re.split(r"\s*(?:БИК|р/с|к/с|ИНН|КПП)", bank, flags=re.I)[0])
        if len(bank) >= 3 and not looks_garbled(bank):
            d["bank_name"] = bank

    if not d.get("bank_account") and RE_CARD.search(re.sub(r"\b\d{20}\b", "", t)):
        d["_card_seen"] = "1"

    return {k: v for k, v in d.items() if v}


# --- автодополнение по БИК --------------------------------------------------

def _load_bik_book():
    book = dict(BIK_BOOK)
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bik.json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                for k, v in json.load(f).items():
                    book[digits(k)] = (v.get("name", ""), digits(v.get("corr", "")))
        except Exception:
            pass
    return book


def enrich_from_bik(d):
    """По известному БИК подставляет банк и корр. счёт, если их не прислали."""
    bik = digits(d.get("bank_bik"))
    if not bik:
        return d
    rec = _load_bik_book().get(bik)
    if rec:
        name, corr = rec
        if name:
            # справочник точнее распознанного текста: короткий или странный
            # результат разбора заменяем, осмысленный оставляем как есть
            cur = (d.get("bank_name") or "").strip()
            if len(cur) < 5 or looks_garbled(cur):
                d["bank_name"] = name
        if corr:
            d.setdefault("bank_corr_account", corr)
    return d


# --- слой 3: ИИ-добор (текст и фото) ---------------------------------------

_RULES = (
    "Если поля нет — ставь пустую строку, НИЧЕГО НЕ ВЫДУМЫВАЙ и не достраивай по догадке. "
    "full_name — в порядке «Фамилия Имя Отчество». "
    "passport_series — ровно 4 цифры без пробелов, passport_number — ровно 6 цифр. "
    "passport_issued_by — кем выдан, дата выдачи и код подразделения одной строкой. "
    "registration_address — адрес регистрации с индексом, одной строкой. "
    "bank_account и bank_corr_account — по 20 цифр без пробелов, bank_bik — 9 цифр. "
    "phone — в формате +7 (XXX) XXX-XX-XX. "
    "Ключи: " + ", ".join(FIELDS) + ". Верни ТОЛЬКО JSON-объект, без пояснений и без markdown."
)

_SYSTEM_TEXT = ("Ты извлекаешь реквизиты физлица из текста, который клиент прислал "
                "в свободной форме. " + _RULES)

_SYSTEM_PHOTO = (
    "На фотографиях — документы одного человека: разворот паспорта РФ, страница "
    "с регистрацией по месту жительства, иногда реквизиты счёта из банковского "
    "приложения. Извлеки данные.\n"
    "Важное:\n"
    "— ПАСПОРТ — ГЛАВНЫЙ ИСТОЧНИК. ФИО, серию, номер, «кем выдан» и особенно адрес "
    "регистрации бери из паспорта, даже если в подписи клиента или на другом "
    "фото написано иначе или короче (например, только город). Из остальных "
    "источников заполняй лишь то, чего в паспорте нет: телефон, почту, банк.\n"
    "— Цифры переписывай ровно как видишь, не «исправляй» их на правдоподобные.\n"
    "— Если символ не разобрать, оставь поле пустым: пустое поле лучше выдуманного.\n"
    "— registration_address бери со страницы «МЕСТО ЖИТЕЛЬСТВА» из синего штампа, "
    "одной строкой в порядке регион, город, улица, дом, квартира. Дату регистрации "
    "и название отдела миграции в адрес НЕ включай.\n"
    "— passport_issued_by — название органа после слов «Паспорт выдан» (сами эти "
    "слова не включай), затем дата выдачи и код подразделения.\n"
    "— Дополнительно верни поле mrz_line: нижнюю из двух строк машиночитаемой "
    "зоны внизу страницы с фотографией, ровно 44 символа, как напечатано, вместе "
    "со знаками «<». Если её не видно — пустая строка.\n"
    + _RULES)


def _clean_llm_result(obj, extra=()):
    allowed = set(FIELDS) | set(extra)
    return {k: str(v).strip() for k, v in (obj or {}).items()
            if k in allowed and str(v).strip()}


def parse_llm(text, known=None):
    """Добор недостающих полей по тексту. {} — если ИИ не настроен или упал."""
    try:
        import llm_provider
        if not llm_provider.text_available():
            return {}
        hint = ""
        if known:
            hint = ("\n\nЧасть полей уже разобрана, перепроверь их и заполни остальные:\n"
                    + json.dumps(known, ensure_ascii=False, indent=2))
        prompt = f"{_SYSTEM_TEXT}\n\n<текст_клиента>\n{text}\n</текст_клиента>{hint}"
        return _clean_llm_result(llm_provider.ask_json_text(prompt))
    except Exception:
        return {}


def parse_images(images, extra_text=""):
    """
    Разбор фотографий документов. images — список (bytes, mime).
    Возвращает (data, issues). Бросает исключение, если зрение не настроено
    или сервис ответил ошибкой — бот покажет причину пользователю.
    """
    import llm_provider
    prompt = _SYSTEM_PHOTO
    if extra_text and extra_text.strip():
        prompt += f"\n\nКлиент дополнительно написал текстом:\n{extra_text.strip()}"
    raw = _clean_llm_result(llm_provider.ask_json_images(prompt, images), extra=("mrz_line",))
    mrz_line = raw.pop("mrz_line", "")
    data = raw

    # текстовую подпись к фото прогоняем ещё и регулярками — бесплатно и точнее
    if extra_text and extra_text.strip():
        for k, v in parse_regex(extra_text).items():
            data.setdefault(k, v)

    # Главная проверка: серию и номер, прочитанные моделью «на глаз», сверяем
    # с машиночитаемой зоной. У неё есть контрольные цифры, поэтому если ключи
    # сходятся — данные из МЧЗ точнее, и они побеждают.
    checked = False
    if mrz_line:
        try:
            import mrz as _mrz
            fields, notes = _mrz.parse_bottom(mrz_line)
            if fields and not notes:
                data = apply_mrz(data, fields, page_text=" ".join(data.values()))
                checked = True
        except Exception:
            pass

    data = normalize(enrich_from_bik(data))
    if checked:
        data["_mrz_checked"] = "1"
    return data, validate(data)


# --- слой 2: валидация ------------------------------------------------------

def validate(d):
    issues = []

    for f in FIELDS:
        if not d.get(f):
            issues.append(Issue("error" if f in CRITICAL else "warn", f,
                                "не найдено — заполните вручную"))

    fio = (d.get("full_name") or "").split()
    if len(fio) == 2:
        issues.append(Issue("warn", "full_name", "нет отчества"))
    elif len(fio) == 1:
        issues.append(Issue("error", "full_name", "указано одно слово"))
    elif len(fio) > 3:
        issues.append(Issue("warn", "full_name", "больше трёх слов — проверьте"))

    s = digits(d.get("passport_series"))
    if s and len(s) != 4:
        issues.append(Issue("error", "passport_series", "должно быть 4 цифры"))
    n = digits(d.get("passport_number"))
    if n and len(n) != 6:
        issues.append(Issue("error", "passport_number", "должно быть 6 цифр"))

    issued = d.get("passport_issued_by") or ""
    if issued and not RE_DATE.search(issued):
        issues.append(Issue("warn", "passport_issued_by", "нет даты выдачи"))
    if issued and not RE_SUBDIV.search(issued):
        issues.append(Issue("warn", "passport_issued_by", "нет кода подразделения"))

    if d.get("email") and not RE_EMAIL.fullmatch(d["email"]):
        issues.append(Issue("error", "email", "некорректный адрес"))
    if d.get("phone") and len(digits(d["phone"])) != 11:
        issues.append(Issue("error", "phone", "не похоже на российский номер"))

    if d.get("_addr_garbled") and not d.get("registration_address"):
        issues = [i for i in issues
                  if not (i.field == "registration_address" and "не найдено" in i.message)]
        issues.append(Issue("error", "registration_address",
                            "адрес распознан неразборчиво — впишите вручную или "
                            "переснимите страницу регистрации крупнее"))
    for f in ("registration_address", "passport_issued_by", "full_name", "bank_name"):
        if d.get(f) and looks_garbled(d[f]):
            issues.append(Issue("error", f, "похоже на ошибку распознавания — проверьте"))

    if d.get("_card_seen") and not d.get("bank_account"):
        issues.append(Issue("error", "bank_account",
                            "прислан номер карты — нужен 20-значный расчётный счёт "
                            "(клиент берёт его в приложении банка: «реквизиты счёта»)"))

    bik = d.get("bank_bik")
    if bik:
        ok, msg = check_bik(bik)
        if not ok:
            issues.append(Issue("error", "bank_bik", msg))
    for field, is_corr in (("bank_account", False), ("bank_corr_account", True)):
        if d.get(field):
            ok, msg = check_account(d[field], bik, is_corr=is_corr)
            if not ok:
                issues.append(Issue("error", field, msg))

    for key, fn in (("inn", check_inn), ("snils", check_snils)):
        if d.get(key):
            ok, msg = fn(d[key])
            if not ok:
                issues.append(Issue("error", key, msg))

    return issues


# --- точка входа ------------------------------------------------------------

def _clean_authority(name):
    """
    Подчищает название органа после распознавания: «САНКТ - ПЕТЕРБУРГУ» склеивает
    обратно, убирает одиночную цифру, прилипшую в конце от кода подразделения
    или номера страницы, и схлопывает лишние пробелы.
    """
    t = _clean(name or "")
    if not t:
        return ""
    t = re.split(r"[,;]?\s*код\s+подразделени\w*", t, flags=re.I)[0]   # добавим заново из МЧЗ
    t = re.split(r"\b\d{2}[.\-/]\d{2}[.\-/]\d{4}\b", t)[0]
    t = re.sub(r"(?<=[А-ЯЁа-яё])\s+-\s+(?=[А-ЯЁа-яё])", "-", t)      # САНКТ - ПЕТЕРБУРГУ
    t = re.sub(r"[\s,]+\d{1,2}$", "", t)                             # ... ОБЛАСТИ 5
    t = re.sub(r"\s{2,}", " ", t).strip(" ,;")
    # в паспорте орган выдачи напечатан прописными; если распознано почти так же,
    # дотягиваем до прописных — уходят «пО г» и прочие огрехи регистра
    letters = [c for c in t if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) >= 0.7:
        t = t.upper()
    return t


def apply_mrz(data, mrz_fields, page_text=""):
    """
    Накладывает данные машиночитаемой зоны на то, что прочиталось со страницы.
    МЧЗ приоритетнее: моноширинный шрифт плюс контрольные цифры, тогда как серия
    с номером на развороте набраны вертикально и распознаются редко.
    Названия органа выдачи в МЧЗ нет — его берём из текста страницы.
    """
    d = dict(data)
    if not mrz_fields:
        return d

    for key in ("passport_series", "passport_number"):
        if mrz_fields.get(key):
            d[key] = mrz_fields[key]

    issued = (d.get("passport_issued_by") or "").strip()
    date = mrz_fields.get("issue_date", "")
    sub = mrz_fields.get("subdivision", "")

    name = ""
    rx = re.compile(r"((?:ГУ|ОУФМС|УФМС|ТП|ОВД|МВД|ГУВД|ОТДЕЛ(?:ЕНИЕ)?)[^\n]{0,120})", re.I)
    m = rx.search(issued) or rx.search(page_text or "")
    if m:
        name = _clean(m.group(1))
    elif issued:
        name = issued
    name = _clean_authority(name)

    parts = [name] if name else []
    if date and date not in name:
        parts.append(date)
    if sub and sub not in name:
        parts.append(f"код подразделения {sub}")
    if parts:
        d["passport_issued_by"] = ", ".join(parts)
    return d


def normalize(data):
    """Приводит цифровые поля и телефон к единому виду."""
    d = dict(data)
    for f in ("passport_series", "passport_number", "bank_account",
              "bank_corr_account", "bank_bik"):
        if d.get(f):
            d[f] = digits(d[f])
    if d.get("phone"):
        d["phone"] = normalize_phone(d["phone"]) or d["phone"]
    if d.get("passport_issued_by"):     # «Паспорт выдан ГУ МВД…» -> «ГУ МВД…»
        d["passport_issued_by"] = re.sub(r"^\s*(?:паспорт\s+)?выдан\s*[:\-]?\s*", "",
                                         d["passport_issued_by"], flags=re.I)
    return d


def parse_client(text, use_llm=True):
    """
    Разбирает свободный текст клиента.
    Возвращает (data, issues). Claude вызывается только ради недостающих полей.
    """
    data = parse_regex(text)
    data = enrich_from_bik(data)
    if any(not data.get(f) for f in FIELDS) and use_llm:
        for k, v in parse_llm(text, known=data).items():
            data.setdefault(k, v)
        data = enrich_from_bik(data)

    return normalize(data), validate(normalize(data))


if __name__ == "__main__":
    import sys
    src = open(sys.argv[1], encoding="utf-8").read() if len(sys.argv) > 1 else sys.stdin.read()
    d, iss = parse_client(src, use_llm=bool(os.environ.get("ANTHROPIC_API_KEY")))
    print(json.dumps(d, ensure_ascii=False, indent=2))
    for i in iss:
        print(i)
