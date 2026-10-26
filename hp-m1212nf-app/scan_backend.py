"""Получение изображений со сканера.

Windows: WIA (Windows Image Acquisition) через COM-объект WIA.DeviceManager —
    работает с драйвером HP для M1212nf (USB и сеть).
Linux/macOS: SANE (`scanimage`), backend hpaio из пакета HPLIP.

Сканирование всегда идёт в оттенках серого или цвете (даже для Ч/Б режима):
бинаризация в image_tools даёт заметно более чистый текст, чем порог сканера.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass

from PIL import Image

IS_WINDOWS = sys.platform == "win32"

# Размеры области сканирования в дюймах.
PAGE_SIZES = {"A4": (8.27, 11.69), "Letter": (8.5, 11.0)}


@dataclass
class ScanSettings:
    dpi: int = 300
    color: bool = False  # False — оттенки серого
    source: str = "flatbed"  # "flatbed" | "feeder"
    page_size: str = "A4"
    brightness: int = 0  # -100..100 (0 — по умолчанию)
    contrast: int = 0


class ScanError(Exception):
    pass


@dataclass
class ScannerInfo:
    id: str
    name: str


def list_scanners() -> list[ScannerInfo]:
    return _wia_list() if IS_WINDOWS else _sane_list()


def scan(device_id: str, settings: ScanSettings) -> list[Image.Image]:
    """Сканирует одну страницу со стекла или все листы из автоподатчика."""
    return _wia_scan(device_id, settings) if IS_WINDOWS else _sane_scan(device_id, settings)


def pick_default(scanners: list[ScannerInfo]) -> int:
    for i, s in enumerate(scanners):
        if "1212" in s.name or "M1210" in s.name:
            return i
    return 0


# ----------------------------------------------------------------- Windows / WIA

WIA_DEVICE_TYPE_SCANNER = 1
# Идентификаторы свойств WIA.
WIA_DPS_DOCUMENT_HANDLING_SELECT = 3088
WIA_DPS_PAGES = 3096
WIA_IPS_CUR_INTENT = 6146
WIA_IPS_XRES = 6147
WIA_IPS_YRES = 6148
WIA_IPS_XPOS = 6149
WIA_IPS_YPOS = 6150
WIA_IPS_XEXTENT = 6151
WIA_IPS_YEXTENT = 6152
WIA_IPS_BRIGHTNESS = 6154
WIA_IPS_CONTRAST = 6155
WIA_IPA_DATATYPE = 4103
WIA_IPA_ITEM_NAME = 4099

FEEDER, FLATBED = 1, 2
INTENT_COLOR, INTENT_GRAY = 1, 2
DATATYPE_GRAY, DATATYPE_COLOR = 2, 3
# BMP поддерживают все драйверы; размер не важен — файл временный.
WIA_FORMAT_BMP = "{B96B3CAB-0728-11D3-9D7B-0000F81EF32E}"
WIA_ERROR_PAPER_EMPTY = 0x80210003
WIA_ERROR_PAPER_JAM = 0x80210002
WIA_ERROR_BUSY = 0x80210006
WIA_ERROR_OFFLINE = 0x80210005


def _hresult(exc) -> int:
    """Достаёт HRESULT из pywintypes.com_error как беззнаковое число."""
    codes = [exc.args[0] if exc.args else 0]
    if len(exc.args) > 2 and exc.args[2]:
        codes.append(exc.args[2][5] or 0)
    for c in codes:
        c &= 0xFFFFFFFF
        if c >> 16 == 0x8021:  # семейство ошибок WIA
            return c
    return codes[-1] & 0xFFFFFFFF


def _set_prop(props, pid: int, value) -> bool:
    for p in props:
        if p.PropertyID == pid:
            try:
                p.Value = value
                return True
            except Exception:
                return False
    return False


def _get_prop(props, pid: int, attr: str = "Value"):
    for p in props:
        if p.PropertyID == pid:
            try:
                return getattr(p, attr)
            except Exception:
                return None
    return None


def _wia_list() -> list[ScannerInfo]:
    import win32com.client

    dm = win32com.client.Dispatch("WIA.DeviceManager")
    result = []
    for info in dm.DeviceInfos:
        if info.Type == WIA_DEVICE_TYPE_SCANNER:
            name = _get_prop(info.Properties, 7) or info.DeviceID  # 7 = WIA_DIP_DEV_NAME
            result.append(ScannerInfo(info.DeviceID, str(name)))
    return result


def _wia_scan(device_id: str, s: ScanSettings) -> list[Image.Image]:
    import pythoncom
    import pywintypes
    import win32com.client

    pythoncom.CoInitialize()  # вызывается из рабочего потока GUI
    try:
        dm = win32com.client.Dispatch("WIA.DeviceManager")
        info = next((i for i in dm.DeviceInfos if i.DeviceID == device_id), None)
        if info is None:
            raise ScanError("Сканер не найден. Проверьте, что МФУ включено и драйвер HP установлен.")
        try:
            dev = info.Connect()
        except pywintypes.com_error as e:
            raise ScanError(_wia_message(_hresult(e))) from e

        feeder = s.source == "feeder"
        _set_prop(dev.Properties, WIA_DPS_DOCUMENT_HANDLING_SELECT, FEEDER if feeder else FLATBED)
        _set_prop(dev.Properties, WIA_DPS_PAGES, 1)
        item = _pick_item(dev, feeder)
        _configure_item(item, s)

        pages: list[Image.Image] = []
        with tempfile.TemporaryDirectory() as tmp:
            while True:
                try:
                    wia_img = item.Transfer(WIA_FORMAT_BMP)
                except pywintypes.com_error as e:
                    code = _hresult(e)
                    if code == WIA_ERROR_PAPER_EMPTY and (pages or not feeder):
                        break  # автоподатчик опустел — всё отсканировано
                    raise ScanError(_wia_message(code)) from e
                path = os.path.join(tmp, f"page{len(pages)}.bmp")
                wia_img.SaveFile(path)
                with Image.open(path) as im:
                    im.load()
                    pages.append(im.copy())
                if not feeder:
                    break
        return pages
    finally:
        pythoncom.CoUninitialize()


def _pick_item(dev, feeder: bool):
    """У WIA 2.0-драйверов стекло и податчик — разные элементы; ищем нужный по имени."""
    items = list(dev.Items)
    if len(items) > 1:
        want = "feed" if feeder else "flat"
        for it in items:
            name = str(_get_prop(it.Properties, WIA_IPA_ITEM_NAME) or "").lower()
            if want in name:
                return it
    return dev.Items[1]  # коллекции WIA нумеруются с 1


def _configure_item(item, s: ScanSettings) -> None:
    props = item.Properties
    _set_prop(props, WIA_IPS_CUR_INTENT, INTENT_COLOR if s.color else INTENT_GRAY)
    _set_prop(props, WIA_IPA_DATATYPE, DATATYPE_COLOR if s.color else DATATYPE_GRAY)
    _set_prop(props, WIA_IPS_XRES, s.dpi)
    _set_prop(props, WIA_IPS_YRES, s.dpi)
    _set_prop(props, WIA_IPS_XPOS, 0)
    _set_prop(props, WIA_IPS_YPOS, 0)
    w_in, h_in = PAGE_SIZES.get(s.page_size, PAGE_SIZES["A4"])
    for pid, inches in ((WIA_IPS_XEXTENT, w_in), (WIA_IPS_YEXTENT, h_in)):
        px = int(inches * s.dpi)
        limit = _get_prop(props, pid, "SubTypeMax")
        if isinstance(limit, int) and limit > 0:
            px = min(px, limit)
        _set_prop(props, pid, px)
    for pid, percent in ((WIA_IPS_BRIGHTNESS, s.brightness), (WIA_IPS_CONTRAST, s.contrast)):
        # Диапазон у драйверов разный (-100..100, -1000..1000) — масштабируем по максимуму.
        hi = _get_prop(props, pid, "SubTypeMax")
        if percent and isinstance(hi, int) and hi > 0:
            _set_prop(props, pid, round(percent / 100 * hi))


def _wia_message(code: int) -> str:
    return {
        WIA_ERROR_PAPER_EMPTY: "В автоподатчике нет бумаги.",
        WIA_ERROR_PAPER_JAM: "Замятие бумаги в автоподатчике.",
        WIA_ERROR_BUSY: "Сканер занят. Подождите и повторите.",
        WIA_ERROR_OFFLINE: "Сканер недоступен (выключен или нет связи по сети/USB).",
    }.get(code, f"Ошибка сканера WIA (код 0x{code:08X}).")


# ----------------------------------------------------------------- Linux / SANE

def _sane_list() -> list[ScannerInfo]:
    if not shutil.which("scanimage"):
        raise ScanError("Не найден scanimage. Установите: sudo apt install sane-utils hplip")
    out = subprocess.run(["scanimage", "-f", "%d|%v %m%n"], capture_output=True, text=True, timeout=60)
    result = []
    for line in out.stdout.splitlines():
        if "|" in line:
            dev, name = line.split("|", 1)
            result.append(ScannerInfo(dev.strip(), name.strip()))
    return result


def _sane_scan(device_id: str, s: ScanSettings) -> list[Image.Image]:
    w_in, h_in = PAGE_SIZES.get(s.page_size, PAGE_SIZES["A4"])
    base = ["scanimage", "-d", device_id, "--resolution", str(s.dpi),
            "--mode", "Color" if s.color else "Gray",
            "-x", f"{w_in * 25.4:.1f}", "-y", f"{h_in * 25.4:.1f}", "--format=pnm"]
    if s.brightness:
        base += ["--brightness", str(s.brightness)]
    if s.contrast:
        base += ["--contrast", str(s.contrast)]

    with tempfile.TemporaryDirectory() as tmp:
        if s.source == "feeder":
            cmd = base + ["--source", "ADF", f"--batch={os.path.join(tmp, 'p%03d.pnm')}"]
        else:
            cmd = base + ["--source", "Flatbed"]
        proc = subprocess.run(cmd, capture_output=True, timeout=600)
        if s.source == "feeder":
            files = sorted(f for f in os.listdir(tmp) if f.endswith(".pnm"))
            if not files:
                raise ScanError(_sane_error(proc.stderr) or "В автоподатчике нет бумаги.")
            pages = []
            for f in files:
                with Image.open(os.path.join(tmp, f)) as im:
                    im.load()
                    pages.append(im.copy())
            return pages
        if proc.returncode != 0 or not proc.stdout:
            raise ScanError(_sane_error(proc.stderr) or "Сканирование не удалось.")
        path = os.path.join(tmp, "page.pnm")
        with open(path, "wb") as fh:
            fh.write(proc.stdout)
        with Image.open(path) as im:
            im.load()
            return [im.copy()]


def _sane_error(stderr: bytes) -> str:
    return stderr.decode(errors="replace").strip().splitlines()[-1] if stderr.strip() else ""
