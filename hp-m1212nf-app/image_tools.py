"""Обработка и компактное сохранение сканов.

Главная идея: сканер (через WIA/SANE) отдаёт несжатое изображение
(BMP/PNM — до ~100 МБ на лист A4 в цвете при 600 dpi). Здесь мы
улучшаем его (белый фон, контраст) и кодируем подходящим способом:
  * текст      -> 1 бит/пиксель (порог Оцу), ~10–60 КБ на страницу;
  * серый/цвет -> JPEG с настраиваемым качеством, ~0.2–1.5 МБ на страницу.
"""
from __future__ import annotations

import io
import os
from dataclasses import dataclass

from PIL import Image, ImageFilter, ImageOps


@dataclass(frozen=True)
class Preset:
    title: str
    dpi: int
    mode: str  # "bw" | "gray" | "color"
    quality: int  # качество JPEG (для "bw" не используется)
    clean_background: bool


PRESETS: dict[str, Preset] = {
    "text_bw": Preset("Текстовый документ (Ч/Б, минимальный размер)", 300, "bw", 0, True),
    "doc_gray": Preset("Документ в оттенках серого", 300, "gray", 75, True),
    "doc_color": Preset("Цветной документ", 300, "color", 75, True),
    "photo": Preset("Фото (высокое качество)", 600, "color", 90, False),
}
DEFAULT_PRESET = "doc_gray"

SAVE_FORMATS = {
    "PDF (многостраничный)": ".pdf",
    "JPEG": ".jpg",
    "PNG": ".png",
    "TIFF (многостраничный)": ".tif",
}


def otsu_threshold(gray: Image.Image) -> int:
    """Порог Оцу по гистограмме: делит пиксели на «чернила» и «бумагу»."""
    hist = gray.histogram()[:256]
    total = sum(hist)
    sum_all = sum(i * h for i, h in enumerate(hist))
    sum_b = w_b = 0
    best_t, best_var = 127, -1.0
    for t in range(256):
        w_b += hist[t]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += t * hist[t]
        m_b = sum_b / w_b
        m_f = (sum_all - sum_b) / w_f
        var = w_b * w_f * (m_b - m_f) ** 2
        if var > best_var:
            best_var, best_t = var, t
    return best_t


def clean_background(img: Image.Image) -> Image.Image:
    """Делает фон бумаги чисто белым и растягивает контраст.

    Серый «шум» бумаги — главный враг размера JPEG: каждый
    случайный оттенок нужно кодировать. Чистый белый сжимается почти в ноль.
    """
    gray = img.convert("L")
    hist = gray.histogram()
    total = sum(hist)
    # Цвет бумаги — самый частый оттенок в светлой половине.
    paper = max(range(128, 256), key=lambda v: hist[v])
    if hist[paper] < total * 0.05:
        return img  # светлого фона почти нет (фото/тёмный лист) — не трогаем
    white = max(paper - 12, 140)
    # Чёрная точка — 0.5-й перцентиль, чтобы текст стал насыщенно-чёрным.
    acc, black = 0, 0
    for v, h in enumerate(hist):
        acc += h
        if acc >= total * 0.005:
            black = v
            break
    black = min(black, white - 64)
    scale = 255.0 / (white - black)
    lut = [0 if v <= black else 255 if v >= white else round((v - black) * scale) for v in range(256)]
    if img.mode == "RGB":
        return img.point(lut * 3)
    return gray.point(lut)


def process(img: Image.Image, mode: str, clean: bool) -> Image.Image:
    """Приводит сырой скан к целевому режиму: 'bw', 'gray' или 'color'."""
    if mode == "color":
        img = img.convert("RGB")
    else:
        img = img.convert("L")
    if clean:
        img = clean_background(img)
    if mode == "bw":
        # Лёгкая резкость перед бинаризацией делает штрихи букв ровнее.
        img = img.filter(ImageFilter.UnsharpMask(radius=1.5, percent=80, threshold=2))
        t = otsu_threshold(img)
        img = img.point([255 if v > t else 0 for v in range(256)], "1")
    return img


def encode_page(img: Image.Image, quality: int, dpi: int) -> tuple[bytes, str]:
    """Кодирует страницу в компактный поток: PNG для 1-бит, иначе JPEG."""
    buf = io.BytesIO()
    if img.mode == "1":
        img.save(buf, "PNG", optimize=True, dpi=(dpi, dpi))
        return buf.getvalue(), ".png"
    img.save(buf, "JPEG", quality=quality, optimize=True, progressive=True, dpi=(dpi, dpi))
    return buf.getvalue(), ".jpg"


def save_pages(pages: list[Image.Image], path: str, quality: int, dpi: int | list[int]) -> list[str]:
    """Сохраняет страницы (dpi — общий или свой для каждой). Возвращает созданные файлы."""
    if not pages:
        raise ValueError("Нет страниц для сохранения")
    dpis = list(dpi) if isinstance(dpi, (list, tuple)) else [dpi] * len(pages)
    ext = os.path.splitext(path)[1].lower()

    if ext == ".pdf":
        import pymupdf

        doc = pymupdf.open()
        for img, d in zip(pages, dpis):
            data, _ = encode_page(img, quality, d)
            page = doc.new_page(width=img.width / d * 72, height=img.height / d * 72)
            page.insert_image(page.rect, stream=data)  # JPEG встраивается без пересжатия
        doc.save(path, garbage=3, deflate=True)
        doc.close()
        return [path]

    if ext in (".tif", ".tiff"):
        # Сжатие общее для файла: CCITT G4 если все страницы Ч/Б, иначе JPEG.
        if all(img.mode == "1" for img in pages):
            frames, opts = pages, {"compression": "group4"}
        else:
            frames = [img.convert("L") if img.mode == "1" else img for img in pages]
            opts = {"compression": "jpeg", "quality": quality or 85}
        frames[0].save(path, "TIFF", dpi=(dpis[0], dpis[0]), save_all=True,
                       append_images=frames[1:], **opts)
        return [path]

    # Одностраничные форматы: при нескольких страницах — file_001.jpg, file_002.jpg…
    base = os.path.splitext(path)[0]
    out = []
    for i, (img, d) in enumerate(zip(pages, dpis), 1):
        target = path if len(pages) == 1 else f"{base}_{i:03d}{ext}"
        if ext in (".jpg", ".jpeg"):
            img.convert("L" if img.mode == "1" else img.mode).save(
                target, "JPEG", quality=quality or 85, optimize=True, dpi=(d, d))
        else:
            img.save(target, "PNG", optimize=True, dpi=(d, d))
        out.append(target)
    return out


def estimate_size(pages: list[Image.Image], quality: int, dpi: int) -> int:
    """Оценка итогового размера PDF в байтах (по первой странице)."""
    if not pages:
        return 0
    data, _ = encode_page(pages[0], quality, dpi)
    return len(data) * len(pages)


def human_size(n: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def rotate(img: Image.Image, degrees: int) -> Image.Image:
    return img.rotate(-degrees, expand=True)


def autocrop_to_content(img: Image.Image, margin_px: int = 0) -> Image.Image:
    """Обрезает пустые белые поля (полезно при сканировании мелких документов/фото)."""
    inv = ImageOps.invert(img.convert("L")).point(lambda v: 255 if v > 30 else 0)
    box = inv.getbbox()
    if not box:
        return img
    l, t, r, b = box
    return img.crop((max(l - margin_px, 0), max(t - margin_px, 0),
                     min(r + margin_px, img.width), min(b + margin_px, img.height)))
