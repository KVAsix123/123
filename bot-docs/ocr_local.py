# -*- coding: utf-8 -*-
"""
Локальное распознавание текста с фото — без интернета и без оплаты.

Данные не покидают компьютер: картинка обрабатывается здесь же, наружу
уходит только ничего (в облако не ходим вообще).

Движок выбирается автоматически из того, что установлено:
    1. PaddleOCR   — быстрый на CPU, кириллица «из коробки»  (pip install paddlepaddle paddleocr)
    2. EasyOCR     — ставится одной командой, тянет torch    (pip install easyocr)
    3. RapidOCR    — самый лёгкий                            (pip install rapidocr-onnxruntime)
    4. Tesseract   — запасной, на фото слабее остальных      (установщик + языковой пакет rus)

Хватит любого одного. Если не стоит ни один — vision_ready() вернёт False,
и бот честно попросит прислать данные текстом.

Телефонное фото — тяжёлый случай для любого OCR (блики, наклон, тени), поэтому
перед распознаванием картинка выпрямляется и чистится, а результат всё равно
проходит через контрольные суммы счетов в parser_klienta.
"""

import io
import os
import re

try:
    from PIL import Image, ImageOps, ImageFilter
except ImportError:
    Image = None

try:
    import numpy as np
except ImportError:
    np = None

try:
    import cv2
except ImportError:
    cv2 = None

_ENGINE = None          # кэш выбранного движка, чтобы не грузить модель на каждое фото
_ENGINE_NAME = ""


# --- предобработка ----------------------------------------------------------

def preprocess(blob, max_side=2200):
    """
    Готовит фото к распознаванию: поворот по EXIF, серый, выравнивание контраста,
    лёгкое шумоподавление, увеличение мелких картинок.
    Возвращает PNG-байты. При любой проблеме — исходник без изменений.
    """
    if Image is None:
        return blob
    try:
        img = Image.open(io.BytesIO(blob))
        img = ImageOps.exif_transpose(img)         # телефон часто пишет поворот в EXIF
        img = img.convert("L")                     # серый: цвет для текста только мешает

        w, h = img.size
        if max(w, h) > max_side:                   # слишком большое — ужимаем
            k = max_side / max(w, h)
            img = img.resize((int(w * k), int(h * k)), Image.LANCZOS)
        elif max(w, h) < 1000:                     # слишком мелкое — растягиваем
            k = 1000 / max(w, h)
            img = img.resize((int(w * k), int(h * k)), Image.LANCZOS)

        if cv2 is not None and np is not None:
            a = np.array(img)
            a = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(a)  # вытягиваем тени
            a = cv2.fastNlMeansDenoising(a, None, 7, 7, 21)
            a = _deskew(a)
            img = Image.fromarray(a)
        else:
            img = ImageOps.autocontrast(img)
            img = img.filter(ImageFilter.SHARPEN)

        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()
    except Exception:
        return blob


def _deskew(a):
    """Выравнивает небольшой наклон страницы (до ~15 градусов)."""
    try:
        inv = cv2.bitwise_not(a)
        thr = cv2.threshold(inv, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)[1]
        coords = cv2.findNonZero(thr)
        if coords is None:
            return a
        angle = cv2.minAreaRect(coords)[-1]
        if angle < -45:
            angle += 90
        elif angle > 45:
            angle -= 90
        if abs(angle) < 0.3 or abs(angle) > 15:
            return a
        h, w = a.shape[:2]
        m = cv2.getRotationMatrix2D((w // 2, h // 2), angle, 1.0)
        return cv2.warpAffine(a, m, (w, h), flags=cv2.INTER_CUBIC,
                              borderMode=cv2.BORDER_REPLICATE)
    except Exception:
        return a


# --- выбор движка -----------------------------------------------------------

def _try_paddle():
    from paddleocr import PaddleOCR
    eng = PaddleOCR(use_angle_cls=True, lang="ru", show_log=False)

    def run(png):
        import numpy as _np
        from PIL import Image as _I
        arr = _np.array(_I.open(io.BytesIO(png)).convert("RGB"))
        res = eng.ocr(arr, cls=True) or []
        lines = []
        for page in res:
            for item in (page or []):
                try:
                    lines.append(item[1][0])
                except Exception:
                    pass
        return "\n".join(lines)
    return run, "PaddleOCR (ru)"


def _try_easyocr():
    import easyocr
    eng = easyocr.Reader(["ru", "en"], gpu=False, verbose=False)

    def run(png):
        return "\n".join(eng.readtext(png, detail=0, paragraph=True))
    return run, "EasyOCR (ru+en)"


def _try_rapidocr():
    from rapidocr_onnxruntime import RapidOCR
    eng = RapidOCR()

    def run(png):
        res, _ = eng(png)
        return "\n".join(r[1] for r in (res or []))
    return run, "RapidOCR"


def _try_tesseract():
    import pytesseract
    langs = pytesseract.get_languages(config="")
    lang = "rus+eng" if "rus" in langs else "eng"
    if lang == "eng":
        raise RuntimeError("для Tesseract не установлен языковой пакет rus")

    def run(png):
        from PIL import Image as _I
        return pytesseract.image_to_string(_I.open(io.BytesIO(png)), lang=lang)
    return run, "Tesseract (rus)"


_CANDIDATES = (_try_paddle, _try_easyocr, _try_rapidocr, _try_tesseract)


def get_engine():
    """Возвращает (функция_распознавания, имя) или (None, причина)."""
    global _ENGINE, _ENGINE_NAME
    if _ENGINE is not None:
        return _ENGINE, _ENGINE_NAME
    problems = []
    for factory in _CANDIDATES:
        try:
            _ENGINE, _ENGINE_NAME = factory()
            return _ENGINE, _ENGINE_NAME
        except Exception as e:
            problems.append(f"{factory.__name__[5:]}: {str(e)[:60]}")
    return None, "не установлен ни один OCR-движок (" + "; ".join(problems) + ")"


def vision_ready():
    return get_engine()[0] is not None


def engine_name():
    eng, name = get_engine()
    return name if eng else ""


# --- исправление типичных ошибок распознавания ------------------------------

# Буквы, которые OCR путает с цифрами. Применяем ТОЛЬКО там, где по формату
# обязаны быть цифры, — в обычном тексте такая замена всё бы испортила.
_LOOKALIKE = {
    "О": "0", "O": "0", "о": "0", "o": "0", "Q": "0", "Ø": "0",
    "l": "1", "I": "1", "|": "1", "і": "1", "L": "1",
    "Z": "2", "z": "2",
    "З": "3", "з": "3",
    "Ч": "4", "ч": "4", "A": "4",
    "S": "5", "s": "5", "Ѕ": "5",
    "б": "6", "Б": "6", "G": "6",
    "Т": "7", "T": "7",
    "В": "8", "B": "8", "Ввв": "8",
    "g": "9", "q": "9", "Э": "9",
}


def digits_from_ocr(s):
    """Приводит «почти число» к цифрам: 4О1783l0 -> 40178310."""
    out = []
    for ch in str(s or ""):
        if ch.isdigit():
            out.append(ch)
        elif ch in _LOOKALIKE:
            out.append(_LOOKALIKE[ch])
    return "".join(out)


_D = "0-9OОоoQØlI|іLZzЗзЧчASsЅбБGТTВBgqЭ"      # цифры + их двойники-буквы


def _mostly_digits(tok, share=0.5):
    """Защита от порчи обычных слов: в куске уже должны быть настоящие цифры."""
    d = sum(c.isdigit() for c in tok)
    letters = sum(c.isalpha() for c in tok)
    return d > 0 and d >= (d + letters) * share


def _fix_token(m):
    tok = m.group(0)
    return digits_from_ocr(tok) if _mostly_digits(tok) else tok


def _fix_keep_seps(m):
    """Чинит цифры, оставляя разделители на месте: О5.О5.2О2О -> 05.05.2020."""
    tok = m.group(0)
    if not _mostly_digits(tok):
        return tok
    return "".join(_LOOKALIKE.get(c, c) if not c.isdigit() and c not in ".-/ " else c
                   for c in tok)


# Порядок важен: сначала формы с разделителями, потом сплошные длинные номера.
_REPAIRS = (
    re.compile(r"(?<![\w])[%s]{2}[.\-/][%s]{2}[.\-/][%s]{4}(?![\w])" % (_D, _D, _D)),  # дата
    re.compile(r"(?<![\w])[%s]{3}\s*-\s*[%s]{3}(?![\w])" % (_D, _D)),                   # код подразделения
    re.compile(r"(?<![\w])[%s]{2}\s[%s]{2}(?=\s|$|\s*№)" % (_D, _D)),                   # серия 41 20
)
_RE_LONG = re.compile(r"[%s]{9,}" % _D)


def repair_numeric_tokens(text):
    """
    Чинит в распознанном тексте только то, что по форме обязано быть числом:
    даты, код подразделения, серию, длинные номера счетов и БИК.
    Обычные слова не трогает — проверка _mostly_digits не даёт испортить текст.
    """
    t = text or ""
    for rx in _REPAIRS:
        t = rx.sub(_fix_keep_seps, t)

    def long_fix(m):
        tok = m.group(0)
        if not _mostly_digits(tok, 0.7):
            return tok
        d = digits_from_ocr(tok)
        return d if len(d) in (9, 20) else tok
    t = _RE_LONG.sub(long_fix, t)

    # «№ 99887Т» — номер паспорта сразу после знака номера
    t = re.sub(r"(№|N{1,2}\s?)\s*([%s]{6})(?![%s])" % (_D, _D),
               lambda m: m.group(1) + (digits_from_ocr(m.group(2))
                                       if _mostly_digits(m.group(2)) else m.group(2)), t)
    return t


# --- точка входа ------------------------------------------------------------

def read_images(images):
    """
    images — список (bytes, mime). Возвращает распознанный текст одной строкой
    на блок. Бросает исключение, если ни один движок не установлен.
    """
    run, name = get_engine()
    if run is None:
        raise RuntimeError(name)
    chunks = []
    for blob, _mime in images:
        text = run(preprocess(blob)) or ""
        text = repair_numeric_tokens(text)
        if text.strip():
            chunks.append(text.strip())
    return "\n".join(chunks)
