"""Печать на HP LaserJet M1212nf (и любом другом принтере системы).

Windows: PDF и изображения растрируются через PyMuPDF/Pillow и рисуются
    прямо в контекст принтера (GDI) — не нужен Acrobat или другая программа
    просмотра PDF. Прочие файлы (DOCX, TXT…) печатаются через связанную
    с ними программу (ShellExecute "printto").
Linux/macOS: выбранные страницы собираются во временный PDF и отправляются в CUPS (`lp`).

У M1212nf нет автоматического дуплекса, поэтому двусторонняя печать —
ручная: сначала нечётные стороны, затем пользователь перекладывает стопку,
и печатаются чётные в обратном порядке.
"""
from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Callable

from PIL import Image

IS_WINDOWS = sys.platform == "win32"

PDF_EXT = {".pdf"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tif", ".tiff", ".webp"}


@dataclass
class PrintOptions:
    copies: int = 1
    page_range: str = ""  # "" — все; иначе «1-3, 5, 8-»
    fit: bool = True  # True — вписать в область печати; False — 100 %
    dpi: int = 600
    manual_duplex: bool = False
    reverse_second_side: bool = True
    rotate_second_side: bool = False


@dataclass(frozen=True)
class PageRef:
    kind: str  # "pdf" | "image" | "blank"
    path: str = ""
    index: int = 0


@dataclass
class PrintPlan:
    passes: list[list[PageRef]] = field(default_factory=list)
    other_files: list[str] = field(default_factory=list)  # печатаются внешней программой


class PrintError(Exception):
    pass


# ----------------------------------------------------------------- планирование

def parse_page_range(text: str, total: int) -> list[int]:
    """«1-3, 5, 8-» -> индексы с нуля. Пустая строка — все страницы."""
    text = text.replace(" ", "")
    if not text:
        return list(range(total))
    result: list[int] = []
    for part in text.split(","):
        if not part:
            continue
        m = re.fullmatch(r"(\d*)-(\d*)|(\d+)", part)
        if not m:
            raise PrintError(f"Неверный диапазон страниц: «{part}»")
        if m.group(3):
            lo = hi = int(m.group(3))
        else:
            lo = int(m.group(1) or 1)
            hi = int(m.group(2) or total)
        for p in range(max(lo, 1), min(hi, total) + 1):
            result.append(p - 1)
    if not result:
        raise PrintError("В указанном диапазоне нет страниц")
    return result


def count_pages(path: str) -> int:
    ext = os.path.splitext(path)[1].lower()
    if ext in PDF_EXT:
        import pymupdf

        with pymupdf.open(path) as doc:
            return doc.page_count
    if ext in IMAGE_EXT:
        with Image.open(path) as im:
            return getattr(im, "n_frames", 1)
    return 0  # неизвестно — печатает внешняя программа


def build_plan(files: list[str], opts: PrintOptions) -> PrintPlan:
    plan = PrintPlan()
    pages: list[PageRef] = []
    for path in files:
        ext = os.path.splitext(path)[1].lower()
        if ext in PDF_EXT or ext in IMAGE_EXT:
            kind = "pdf" if ext in PDF_EXT else "image"
            n = count_pages(path)
            # Диапазон применяется только когда файл один — иначе он неоднозначен.
            idx = parse_page_range(opts.page_range, n) if len(files) == 1 else range(n)
            pages += [PageRef(kind, path, i) for i in idx]
        else:
            plan.other_files.append(path)
    if not pages:
        return plan

    copies = max(1, opts.copies)
    if not opts.manual_duplex:
        plan.passes = [pages * copies]
        return plan

    # Каждая копия начинается с нового листа: при нечётном числе страниц
    # добавляем пустую оборотную сторону.
    doc: list[PageRef] = []
    for _ in range(copies):
        doc += pages
        if len(pages) % 2:
            doc.append(PageRef("blank"))
    front, back = doc[0::2], doc[1::2]
    if opts.reverse_second_side:
        back.reverse()
    plan.passes = [front, back]
    return plan


# ----------------------------------------------------------------- растеризация

class _Renderer:
    """Превращает PageRef в изображение в оттенках серого (принтер монохромный)."""

    def __init__(self, dpi: int):
        self.dpi = dpi
        self._docs: dict[str, object] = {}

    def render(self, ref: PageRef) -> tuple[Image.Image | None, tuple[float, float]]:
        """Возвращает (изображение, (ширина, высота) в дюймах)."""
        if ref.kind == "blank":
            return None, (8.27, 11.69)
        if ref.kind == "pdf":
            import pymupdf

            doc = self._docs.get(ref.path) or self._docs.setdefault(ref.path, pymupdf.open(ref.path))
            page = doc[ref.index]
            pix = page.get_pixmap(dpi=self.dpi, colorspace=pymupdf.csGRAY, alpha=False)
            img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
            return img, (page.rect.width / 72, page.rect.height / 72)
        with Image.open(ref.path) as im:
            im.seek(ref.index)
            img = im.convert("L")
            xdpi = (im.info.get("dpi") or (0, 0))[0] or 0
        size = (img.width / xdpi, img.height / xdpi) if xdpi > 30 else (0.0, 0.0)  # 0 — неизвестно
        return img, size

    def close(self):
        for d in self._docs.values():
            d.close()


def _rotate_for_paper(img: Image.Image | None, size: tuple[float, float]):
    """Альбомные страницы поворачиваем, чтобы они легли на книжный лист."""
    if img is not None and img.width > img.height:
        return img.rotate(90, expand=True), (size[1], size[0])
    return img, size


# ----------------------------------------------------------------- исполнение

def execute(printer: str, plan: PrintPlan, opts: PrintOptions,
            progress: Callable[[str], None], ask_flip: Callable[[int], bool]) -> None:
    """Печатает план. ask_flip(число_листов) вызывается между проходами
    дуплекса; если вернёт False — печать второй стороны отменяется."""
    for n, pass_pages in enumerate(plan.passes):
        if n == 1:
            if not ask_flip(len(plan.passes[0])):
                progress("Печать второй стороны отменена")
                return
        rotate180 = n == 1 and opts.rotate_second_side
        label = "" if len(plan.passes) == 1 else (" (лицевая сторона)" if n == 0 else " (оборотная сторона)")
        if IS_WINDOWS:
            _win_print_pages(printer, pass_pages, opts, rotate180, progress, label)
        else:
            _cups_print_pages(printer, pass_pages, opts, rotate180, progress, label)

    for path in plan.other_files:
        for _ in range(max(1, opts.copies)):
            progress(f"Отправка {os.path.basename(path)} через связанную программу…")
            _print_via_associated_app(printer, path)
    progress("Задание отправлено на принтер")


# ----------------------------------------------------------------- Windows / GDI

def _win_print_pages(printer, pages, opts, rotate180, progress, label):
    import win32con
    import win32ui
    from PIL import ImageWin

    dc = win32ui.CreateDC()
    dc.CreatePrinterDC(printer)
    try:
        printable_w = dc.GetDeviceCaps(win32con.HORZRES)
        printable_h = dc.GetDeviceCaps(win32con.VERTRES)
        dpi_x = dc.GetDeviceCaps(win32con.LOGPIXELSX)
        dpi_y = dc.GetDeviceCaps(win32con.LOGPIXELSY)
        paper_w = dc.GetDeviceCaps(win32con.PHYSICALWIDTH)
        paper_h = dc.GetDeviceCaps(win32con.PHYSICALHEIGHT)
        off_x = dc.GetDeviceCaps(win32con.PHYSICALOFFSETX)
        off_y = dc.GetDeviceCaps(win32con.PHYSICALOFFSETY)

        renderer = _Renderer(min(opts.dpi, max(dpi_x, 150)))
        dc.StartDoc("HP M1212nf — печать" + label)
        try:
            for i, ref in enumerate(pages, 1):
                progress(f"Печать страницы {i} из {len(pages)}{label}…")
                img, size = _rotate_for_paper(*renderer.render(ref))
                dc.StartPage()
                if img is not None:
                    if rotate180:
                        img = img.rotate(180)
                    if opts.fit or not size[0]:
                        # Вписываем в печатную область с сохранением пропорций.
                        k = min(printable_w / img.width, printable_h / img.height)
                        w, h = int(img.width * k), int(img.height * k)
                        x, y = (printable_w - w) // 2, (printable_h - h) // 2
                    else:
                        # 100 %: реальный размер, по центру физического листа.
                        w, h = int(size[0] * dpi_x), int(size[1] * dpi_y)
                        x, y = (paper_w - w) // 2 - off_x, (paper_h - h) // 2 - off_y
                    ImageWin.Dib(img).draw(dc.GetHandleOutput(), (x, y, x + w, y + h))
                dc.EndPage()
            dc.EndDoc()
        except Exception:
            dc.AbortDoc()
            raise
        finally:
            renderer.close()
    finally:
        dc.DeleteDC()


def _print_via_associated_app(printer: str, path: str) -> None:
    if IS_WINDOWS:
        import win32api

        try:
            win32api.ShellExecute(0, "printto", path, f'"{printer}"', ".", 0)
        except Exception as e:
            raise PrintError(f"Не удалось напечатать {os.path.basename(path)}: нет программы, "
                             f"умеющей печатать такие файлы. Сохраните его в PDF.") from e
    else:
        _run(["lp", "-d", printer, path])


# ----------------------------------------------------------------- Linux / CUPS

def _cups_print_pages(printer, pages, opts, rotate180, progress, label):
    import pymupdf

    progress(f"Подготовка {len(pages)} стр.{label}…")
    out = pymupdf.open()
    sources: dict[str, object] = {}
    renderer = _Renderer(opts.dpi)
    try:
        for ref in pages:
            if ref.kind == "pdf":
                src = sources.get(ref.path) or sources.setdefault(ref.path, pymupdf.open(ref.path))
                out.insert_pdf(src, from_page=ref.index, to_page=ref.index)
            elif ref.kind == "image":
                img, size = renderer.render(ref)
                w_in, h_in = size if size[0] else (8.27, 11.69)
                page = out.new_page(width=w_in * 72, height=h_in * 72)
                buf = io.BytesIO()
                img.save(buf, "PNG")
                page.insert_image(page.rect, stream=buf.getvalue(), keep_proportion=True)
            else:
                out.new_page(width=595, height=842)
            if rotate180:
                p = out[-1]
                p.set_rotation((p.rotation + 180) % 360)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "job.pdf")
            out.save(path, garbage=3, deflate=True)
            progress(f"Отправка в очередь печати{label}…")
            _run(["lp", "-d", printer, "-o", "print-scaling=" + ("fit" if opts.fit else "none"), path])
    finally:
        out.close()
        for s in sources.values():
            s.close()


def _run(cmd: list[str]) -> str:
    if not shutil.which(cmd[0]):
        raise PrintError(f"Не найдена команда {cmd[0]} (установите CUPS)")
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        raise PrintError(p.stderr.strip() or f"{cmd[0]} завершился с ошибкой")
    return p.stdout


# ----------------------------------------------------------------- принтеры и очередь

def list_printers() -> tuple[list[str], str]:
    """(список принтеров, принтер по умолчанию)."""
    if IS_WINDOWS:
        import win32print

        flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        names = [p[2] for p in win32print.EnumPrinters(flags, None, 1)]
        try:
            default = win32print.GetDefaultPrinter()
        except Exception:
            default = ""
    else:
        names, default = [], ""
        if shutil.which("lpstat"):
            out = subprocess.run(["lpstat", "-e"], capture_output=True, text=True).stdout
            names = [n.strip() for n in out.splitlines() if n.strip()]
            d = subprocess.run(["lpstat", "-d"], capture_output=True, text=True).stdout
            default = d.split(":", 1)[1].strip() if ":" in d else ""
    for n in names:  # если есть M1212 — выбираем его, а не системный по умолчанию
        if "1212" in n or "M1210" in n:
            default = n
            break
    return names, default


_STATUS_FLAGS = {
    0x00000002: "ошибка", 0x00000008: "замятие бумаги", 0x00000010: "нет бумаги",
    0x00000040: "проблема с бумагой", 0x00000080: "не в сети", 0x00000400: "печатает",
    0x00040000: "заканчивается тонер", 0x00080000: "нет тонера", 0x00100000: "открыта дверца",
    0x00000001: "приостановлен", 0x00000200: "занят",
}


def printer_status(printer: str) -> tuple[str, list[str]]:
    """(состояние, список заданий в очереди)."""
    if IS_WINDOWS:
        import win32print

        h = win32print.OpenPrinter(printer)
        try:
            info = win32print.GetPrinter(h, 2)
            jobs = win32print.EnumJobs(h, 0, 999, 1)
        finally:
            win32print.ClosePrinter(h)
        flags = [t for bit, t in _STATUS_FLAGS.items() if info["Status"] & bit]
        status = ", ".join(flags) if flags else "готов"
        return status, [f"{j['pDocument']} — {j['TotalPages'] or '?'} стр." for j in jobs]
    status = _run(["lpstat", "-p", printer]).strip()
    jobs = _run(["lpstat", "-o", printer]).strip().splitlines()
    return status, jobs


def clear_queue(printer: str) -> None:
    if IS_WINDOWS:
        import win32print

        h = win32print.OpenPrinter(printer, {"DesiredAccess": win32print.PRINTER_ALL_ACCESS})
        try:
            for j in win32print.EnumJobs(h, 0, 999, 1):
                win32print.SetJob(h, j["JobId"], 0, None, win32print.JOB_CONTROL_DELETE)
        finally:
            win32print.ClosePrinter(h)
    else:
        _run(["cancel", "-a", printer])


def printer_ip(printer: str) -> str | None:
    """IP-адрес сетевого принтера (для открытия его веб-интерфейса)."""
    if IS_WINDOWS:
        import win32print

        h = win32print.OpenPrinter(printer)
        try:
            text = win32print.GetPrinter(h, 2)["pPortName"] or ""
        finally:
            win32print.ClosePrinter(h)
    else:
        text = _run(["lpstat", "-v", printer]) if shutil.which("lpstat") else ""
    m = re.search(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", text)
    return m.group(1) if m else None
