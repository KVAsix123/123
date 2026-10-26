"""HP LaserJet M1212nf MFP — печать и сканирование.

Запуск:  python app.py [файлы для печати…]
"""
from __future__ import annotations

import datetime as dt
import json
import os
import queue
import sys
import tempfile
import threading
import tkinter as tk
import webbrowser
from tkinter import filedialog

import customtkinter as ctk
from PIL import Image, ImageTk

import image_tools as it
import print_backend as pb
import scan_backend as sb
import widgets as w
from viewer import FullscreenViewer

APP_TITLE = "HP LaserJet M1212nf"
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".hp_m1212nf_app.json")
CUSTOM_PRESET = "Свои настройки"
MODE_LABELS = {"bw": "Ч/Б", "gray": "Серый", "color": "Цвет"}
MODE_NAMES = {"bw": "Ч/Б 1 бит", "gray": "оттенки серого", "color": "цвет"}
SOURCE_LABELS = {"flatbed": "Стекло", "feeder": "Автоподатчик"}
SOURCE_HINTS = {"flatbed": "Положите документ лицом вниз, в угол со стрелкой.",
                "feeder": "Листы лицом вверх, верхним краем вперёд. До 35 листов."}
LAYOUT_LABELS = {"single": "1 скан = 1 стр.", "a4x2": "2 скана на A4"}
THEME_LABELS = {"system": "Авто", "light": "Светлая", "dark": "Тёмная"}
DPI_CHOICES = ["150", "200", "300", "600", "1200"]
PREVIEW_MAX = 1600  # длинная сторона уменьшенной копии для окна предпросмотра
PRESET_HINTS = {
    "text_bw": "Договоры, справки, распечатки без цветных печатей. 10–60 КБ на страницу.",
    "doc_gray": "Универсальный выбор для документов с печатями и подписями. 150–400 КБ.",
    "doc_color": "Документы с цветными печатями, бланки, диаграммы. 300–900 КБ.",
    "id_doc": "Паспорт, права, СНИЛС: цвет без «отбеливания», автообрезка по краю документа.",
    "photo": "Фотографии и рисунки. Крупные файлы — 2–5 МБ на лист.",
}


def _key(mapping: dict, label: str) -> str:
    return next(k for k, v in mapping.items() if v == label)


class ScannedPage:
    def __init__(self, raw: Image.Image, dpi: int):
        self.raw = raw
        self.dpi = dpi
        self.rotation = 0
        self._small = None

    def small(self) -> Image.Image:
        if self._small is None:
            k = PREVIEW_MAX / max(self.raw.size)
            self._small = self.raw.resize((int(self.raw.width * k), int(self.raw.height * k)),
                                          Image.LANCZOS) if k < 1 else self.raw.copy()
        return it.rotate(self._small, self.rotation)

    def snapshot(self) -> tuple[Image.Image, int, int]:
        return self.raw, self.rotation, self.dpi


# --------------------------------------------------------------------- итоговые страницы файла

def output_count(n_scans: int, layout: str) -> int:
    return n_scans if layout == "single" else (n_scans + 1) // 2


def output_page(snap: list, i: int, mode: str, clean: bool, layout: str) -> tuple[Image.Image, int]:
    """Страница i итогового файла: обработанный скан или 2 скана на листе A4."""
    def one(j):
        raw, rotation, dpi = snap[j]
        return it.process(it.rotate(raw, rotation), mode, clean), dpi

    if layout == "single":
        return one(i)
    return it.compose_on_a4([one(j) for j in (2 * i, 2 * i + 1) if j < len(snap)])


class App(ctk.CTk):
    def __init__(self, files_to_print: list[str]):
        self.cfg = self._load_cfg()
        ctk.set_appearance_mode(self.cfg.get("theme", "system"))
        super().__init__(fg_color=w.BG)
        self.title(APP_TITLE)
        self.geometry("1320x840")
        self.minsize(1120, 720)

        self._jobs: queue.Queue = queue.Queue()
        self._busy = False
        self.pages: list[ScannedPage] = []
        self.scanners: list[sb.ScannerInfo] = []
        self.print_files: list[str] = []
        self.selected = -1
        self._preview_ref = None
        self._thumb_widgets: list = []
        self._estimate_after = None
        self._estimate_token = 0

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._build_sidebar()

        content = ctk.CTkFrame(self, fg_color="transparent")
        content.grid(row=0, column=1, sticky="nsew", padx=(8, 20), pady=(16, 10))
        content.grid_columnconfigure(0, weight=1)
        content.grid_rowconfigure(1, weight=1)
        head = ctk.CTkFrame(content, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        self.page_title = ctk.CTkLabel(head, text="", font=w.font(24, "bold"), text_color=w.TEXT, anchor="w")
        self.page_title.pack(fill="x")
        self.page_subtitle = ctk.CTkLabel(head, text="", font=w.font(13), text_color=w.MUTED, anchor="w")
        self.page_subtitle.pack(fill="x")

        stack = ctk.CTkFrame(content, fg_color="transparent")
        stack.grid(row=1, column=0, sticky="nsew")
        stack.grid_columnconfigure(0, weight=1)
        stack.grid_rowconfigure(0, weight=1)
        self.screens = {}
        for name in ("scan", "print", "printer"):
            frame = ctk.CTkFrame(stack, fg_color="transparent")
            frame.grid(row=0, column=0, sticky="nsew")
            self.screens[name] = frame

        status = ctk.CTkFrame(content, fg_color="transparent", height=28)
        status.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self.status = tk.StringVar(value="Готово")
        ctk.CTkLabel(status, textvariable=self.status, font=w.font(12), text_color=w.MUTED,
                     anchor="w").pack(side="left")
        self.progress = ctk.CTkProgressBar(status, mode="indeterminate", width=180, height=6,
                                           progress_color=w.ACCENT, fg_color=w.FIELD)

        self._build_scan_screen(self.screens["scan"])
        self._build_print_screen(self.screens["print"])
        self._build_printer_screen(self.screens["printer"])
        self.show_screen("scan")

        self.bind("<Control-s>", lambda e: self.do_save())
        self.bind("<F11>", lambda e: self.open_fullscreen())
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll_jobs)
        self.after(300, self.refresh_devices)
        if files_to_print:
            self.add_print_files(files_to_print)
            self.show_screen("print")

    # ================================================================ боковая панель

    def _build_sidebar(self):
        side = ctk.CTkFrame(self, fg_color=w.SIDEBAR, corner_radius=0, width=240,
                            border_width=0)
        side.grid(row=0, column=0, sticky="nsw")
        side.grid_propagate(False)
        side.pack_propagate(False)

        logo = ctk.CTkFrame(side, fg_color="transparent")
        logo.pack(fill="x", padx=18, pady=(24, 28))
        ctk.CTkLabel(logo, text="hp", width=44, height=44, corner_radius=12, fg_color=w.ACCENT,
                     text_color="white", font=w.font(18, "bold")).pack(side="left")
        names = ctk.CTkFrame(logo, fg_color="transparent")
        names.pack(side="left", padx=10)
        ctk.CTkLabel(names, text="M1212nf MFP", font=w.font(16, "bold"), text_color=w.TEXT,
                     anchor="w").pack(fill="x")
        ctk.CTkLabel(names, text="Принтер и сканер", font=w.font(12), text_color=w.MUTED,
                     anchor="w").pack(fill="x")

        self.nav = {}
        for key, icon, label in (("scan", "▣", "Сканирование"), ("print", "▤", "Печать"),
                                 ("printer", "⚙", "Принтер")):
            b = ctk.CTkButton(side, text=f"  {icon}    {label}", anchor="w", height=44, corner_radius=10,
                              font=w.font(14), fg_color="transparent", text_color=w.TEXT,
                              hover_color=w.FIELD, command=lambda k=key: self.show_screen(k))
            b.pack(fill="x", padx=14, pady=2)
            self.nav[key] = b

        bottom = ctk.CTkFrame(side, fg_color="transparent")
        bottom.pack(side="bottom", fill="x", padx=14, pady=18)
        pill = ctk.CTkFrame(bottom, fg_color=w.FIELD, corner_radius=12)
        pill.pack(fill="x", pady=(0, 14))
        ctk.CTkLabel(pill, text="ПРИНТЕР", font=w.font(10, "bold"), text_color=w.MUTED,
                     anchor="w").pack(fill="x", padx=12, pady=(10, 0))
        row = ctk.CTkFrame(pill, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=(2, 10))
        self.side_dot = ctk.CTkLabel(row, text="●", font=w.font(14), text_color=w.MUTED, width=14)
        self.side_dot.pack(side="left")
        self.side_state = ctk.CTkLabel(row, text="поиск…", font=w.font(12), text_color=w.TEXT, anchor="w")
        self.side_state.pack(side="left", padx=6, fill="x")

        ctk.CTkLabel(bottom, text="Тема оформления", font=w.font(11), text_color=w.MUTED,
                     anchor="w").pack(fill="x", pady=(0, 4))
        self.theme_var = tk.StringVar(value=THEME_LABELS.get(self.cfg.get("theme", "system"), "Авто"))
        w.segmented(bottom, list(THEME_LABELS.values()), self.theme_var, self._change_theme).pack(fill="x")

    def _change_theme(self, label: str):
        mode = _key(THEME_LABELS, label)
        self.cfg["theme"] = mode
        ctk.set_appearance_mode(mode)

    def show_screen(self, key: str):
        titles = {"scan": ("Сканирование", "Сканируйте со стекла или из автоподатчика и сохраняйте в лёгкий PDF"),
                  "print": ("Печать", "PDF, фотографии и документы — с ручной двусторонней печатью"),
                  "printer": ("Принтер", "Состояние, очередь заданий и обслуживание")}
        self.screens[key].tkraise()
        self.page_title.configure(text=titles[key][0])
        self.page_subtitle.configure(text=titles[key][1])
        for k, b in self.nav.items():
            active = k == key
            b.configure(fg_color=w.ACCENT_SOFT if active else "transparent",
                        text_color=w.ACCENT if active else w.TEXT,
                        font=w.font(14, "bold" if active else "normal"))
        if key == "printer":
            self.refresh_printer_state()

    # ================================================================ фоновые задачи

    def run_bg(self, title: str, func, on_done=None):
        """Выполняет func() в фоновом потоке, чтобы окно не «зависало»."""
        if self._busy:
            w.info(self, "Подождите, предыдущая операция ещё выполняется.", "Минутку", "warning")
            return
        self._busy = True
        self.status.set(title)
        self.progress.pack(side="right")
        self.progress.start()

        def worker():
            try:
                self._jobs.put((on_done, func(), None))
            except Exception as e:  # noqa: BLE001 — любую ошибку показываем пользователю
                self._jobs.put((None, None, e))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_jobs(self):
        try:
            while True:
                item = self._jobs.get_nowait()
                if item[0] == "ui":  # обновление интерфейса из рабочего потока
                    item[1](*item[2])
                    continue
                if item[0] == "call":  # вызов в главном потоке с ожиданием результата
                    _, fn, box = item
                    try:
                        box["result"] = fn()
                    finally:
                        box["event"].set()
                    continue
                on_done, result, err = item
                self._busy = False
                self.progress.stop()
                self.progress.pack_forget()
                if err:
                    self.status.set("Ошибка")
                    w.error(self, str(err) or err.__class__.__name__)
                else:
                    self.status.set("Готово")
                    if on_done:
                        on_done(result)
        except queue.Empty:
            pass
        self.after(100, self._poll_jobs)

    def post(self, fn, *args):
        """Из рабочего потока: выполнить fn(*args) в главном потоке (Tk не потокобезопасен)."""
        self._jobs.put(("ui", fn, args))

    def call_in_ui(self, fn):
        """Из рабочего потока: выполнить fn в главном потоке и дождаться результата."""
        box = {"event": threading.Event(), "result": None}
        self._jobs.put(("call", fn, box))
        box["event"].wait()
        return box["result"]

    def set_status_threadsafe(self, text: str):
        self.post(self.status.set, text)

    # ================================================================ экран «Сканирование»

    def _build_scan_screen(self, root):
        root.grid_columnconfigure(1, weight=1)
        root.grid_rowconfigure(0, weight=1)

        left = ctk.CTkFrame(root, fg_color="transparent", width=360)
        left.grid(row=0, column=0, sticky="nsw", padx=(0, 16))
        left.grid_propagate(False)
        left.grid_rowconfigure(0, weight=1)
        left.grid_columnconfigure(0, weight=1)
        scroll = ctk.CTkScrollableFrame(left, fg_color="transparent", scrollbar_button_color=w.FIELD)
        scroll.grid(row=0, column=0, sticky="nsew")
        pad = {"fill": "x", "padx": 16}

        card = w.Card(scroll, "Сканер")
        card.pack(fill="x", pady=(0, 10), padx=(0, 6))
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(**pad, pady=(0, 14))
        self.scanner_var = tk.StringVar(value="Поиск…")
        self.scanner_menu = w.option_menu(row, [], self.scanner_var)
        self.scanner_menu.pack(side="left", fill="x", expand=True)
        w.tool_button(row, "↻", self.refresh_devices).pack(side="left", padx=(6, 0))

        card = w.Card(scroll, "Откуда сканировать")
        card.pack(fill="x", pady=(0, 10), padx=(0, 6))
        self.source_var = tk.StringVar(value=SOURCE_LABELS[self.cfg.get("source", "flatbed")])
        w.segmented(card, list(SOURCE_LABELS.values()), self.source_var,
                    lambda v: self.source_hint.configure(text=SOURCE_HINTS[_key(SOURCE_LABELS, v)])).pack(**pad)
        self.source_hint = ctk.CTkLabel(card, text=SOURCE_HINTS[_key(SOURCE_LABELS, self.source_var.get())],
                                        font=w.font(12), text_color=w.MUTED, wraplength=290, justify="left",
                                        anchor="w")
        self.source_hint.pack(**pad, pady=(6, 14))

        card = w.Card(scroll, "Что сканируем")
        card.pack(fill="x", pady=(0, 10), padx=(0, 6))
        self.preset_var = tk.StringVar()
        w.option_menu(card, [p.title for p in it.PRESETS.values()], self.preset_var,
                      lambda v: self._apply_preset()).pack(**pad)
        self.preset_hint = ctk.CTkLabel(card, text="", font=w.font(12), text_color=w.MUTED, wraplength=290,
                                        justify="left", anchor="w")
        self.preset_hint.pack(**pad, pady=(6, 14))

        card = w.Card(scroll, "Параметры")
        card.pack(fill="x", pady=(0, 10), padx=(0, 6))
        self._label(card, "Разрешение, dpi")
        self.dpi_var = tk.StringVar(value=str(self.cfg.get("dpi", 300)))
        w.segmented(card, DPI_CHOICES, self.dpi_var, lambda v: self._settings_changed(True)).pack(**pad)
        self._label(card, "Цвет")
        self.mode_var = tk.StringVar(value=MODE_LABELS[self.cfg.get("mode", "gray")])
        w.segmented(card, list(MODE_LABELS.values()), self.mode_var,
                    lambda v: self._settings_changed(True)).pack(**pad)
        q_head = ctk.CTkFrame(card, fg_color="transparent")
        q_head.pack(**pad, pady=(12, 0))
        ctk.CTkLabel(q_head, text="Сжатие", font=w.font(12), text_color=w.MUTED).pack(side="left")
        self.quality_label = ctk.CTkLabel(q_head, text="", font=w.font(12, "bold"), text_color=w.TEXT)
        self.quality_label.pack(side="right")
        self.quality_var = tk.IntVar(value=self.cfg.get("quality", 75))
        self.quality_slider = ctk.CTkSlider(card, from_=40, to=95, number_of_steps=55, variable=self.quality_var,
                                            button_color=w.ACCENT, button_hover_color=w.ACCENT_HOVER,
                                            progress_color=w.ACCENT, fg_color=w.FIELD,
                                            command=lambda v: self._settings_changed(True))
        self.quality_slider.pack(**pad, pady=(2, 0))
        hint = ctk.CTkFrame(card, fg_color="transparent")
        hint.pack(**pad)
        ctk.CTkLabel(hint, text="меньше файл", font=w.font(11), text_color=w.MUTED).pack(side="left")
        ctk.CTkLabel(hint, text="больше деталей", font=w.font(11), text_color=w.MUTED).pack(side="right")

        self.clean_var = tk.BooleanVar(value=self.cfg.get("clean", True))
        w.switch(card, "Белый фон (убрать серость бумаги)", self.clean_var,
                 lambda: self._settings_changed(True)).pack(**pad, pady=(10, 0))
        self.autocrop_var = tk.BooleanVar(value=self.cfg.get("autocrop", False))
        w.switch(card, "Обрезать по краю документа", self.autocrop_var,
                 lambda: self._settings_changed(True)).pack(**pad, pady=(8, 0))
        self._label(card, "Формат области сканирования")
        self.page_size_var = tk.StringVar(value=self.cfg.get("page_size", "A4"))
        w.segmented(card, list(sb.PAGE_SIZES), self.page_size_var).pack(**pad, pady=(0, 14))

        card = w.Card(scroll, "Несколько документов в один файл")
        card.pack(fill="x", pady=(0, 10), padx=(0, 6))
        self.series_var = tk.BooleanVar(value=self.cfg.get("series", True))
        w.switch(card, "Серия: после скана предложить следующий", self.series_var).pack(**pad)
        self._label(card, "Раскладка в файле")
        self.layout_var = tk.StringVar(value=LAYOUT_LABELS[self.cfg.get("layout", "single")])
        w.segmented(card, list(LAYOUT_LABELS.values()), self.layout_var,
                    lambda v: self._settings_changed()).pack(**pad)
        ctk.CTkLabel(card, text="Например, паспорт: разворот с фото, затем прописка — каждый скан "
                                "добавляется в конец файла. «2 скана на A4» кладёт их на один лист "
                                "в реальном размере, как ксерокопия.",
                     font=w.font(12), text_color=w.MUTED, wraplength=290, justify="left",
                     anchor="w").pack(**pad, pady=(6, 14))

        actions = ctk.CTkFrame(left, fg_color="transparent")
        actions.grid(row=1, column=0, sticky="ew", pady=(10, 0), padx=(0, 6))
        self.scan_button = w.primary_button(actions, "Сканировать", self.do_scan, height=50)
        self.scan_button.configure(font=w.font(15, "bold"))
        self.scan_button.pack(fill="x")
        row = ctk.CTkFrame(actions, fg_color="transparent")
        row.pack(fill="x", pady=(8, 0))
        row.grid_columnconfigure((0, 1), weight=1)
        w.secondary_button(row, "Сохранить…", self.do_save).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        w.secondary_button(row, "Напечатать копию", self.do_copy).grid(row=0, column=1, sticky="ew", padx=(4, 0))
        self.estimate_label = ctk.CTkLabel(actions, text="", font=w.font(12), text_color=w.MUTED,
                                           justify="left", anchor="w")
        self.estimate_label.pack(fill="x", pady=(8, 0))

        # Правая часть: панель инструментов, предпросмотр, лента миниатюр.
        right = w.Card(root)
        right.grid(row=0, column=1, sticky="nsew")
        tools = ctk.CTkFrame(right, fg_color="transparent")
        tools.pack(fill="x", padx=12, pady=(10, 4))
        self.preview_title = ctk.CTkLabel(tools, text="Предпросмотр", font=w.font(15, "bold"), text_color=w.TEXT)
        self.preview_title.pack(side="left", padx=(6, 14))
        for text, cmd in (("↺", lambda: self._rotate(-90)), ("↻", lambda: self._rotate(90)),
                          ("◀", lambda: self._move(-1)), ("▶", lambda: self._move(1))):
            w.tool_button(tools, text, cmd).pack(side="left")
        w.tool_button(tools, "✂ Обрезать", self._autocrop, width=96).pack(side="left", padx=(6, 0))
        w.tool_button(tools, "✕ Удалить", self._delete_page, width=90).pack(side="left")
        ctk.CTkButton(tools, text="⤢  На весь экран", command=self.open_fullscreen, height=32, corner_radius=8,
                      fg_color=w.ACCENT_SOFT, hover_color=w.CARD_BORDER, text_color=w.ACCENT,
                      font=w.font(13, "bold")).pack(side="right")
        w.tool_button(tools, "Очистить всё", self._clear_pages, width=110).pack(side="right", padx=6)

        holder = ctk.CTkFrame(right, fg_color=w.VIEWER_BG, corner_radius=12)
        holder.pack(fill="both", expand=True, padx=12, pady=6)
        self.preview = tk.Canvas(holder, background=w.VIEWER_BG, highlightthickness=0, cursor="hand2")
        self.preview.pack(fill="both", expand=True, padx=6, pady=6)
        self.preview.bind("<Configure>", lambda e: self._draw_preview())
        self.preview.bind("<Double-Button-1>", lambda e: self.open_fullscreen())

        self.strip = ctk.CTkScrollableFrame(right, orientation="horizontal", height=132, fg_color=w.FIELD,
                                            corner_radius=12, scrollbar_button_color=w.CARD_BORDER)
        self.strip.pack(fill="x", padx=12, pady=(4, 12))

        preset_key = self.cfg.get("preset", it.DEFAULT_PRESET)
        if preset_key in it.PRESETS:
            self.preset_var.set(it.PRESETS[preset_key].title)
        self._settings_changed(True)
        self._refresh_pages()

    def _label(self, card, text):
        ctk.CTkLabel(card, text=text, font=w.font(12), text_color=w.MUTED, anchor="w").pack(
            fill="x", padx=16, pady=(12, 4))

    def _preset_key(self) -> str | None:
        return next((k for k, p in it.PRESETS.items() if p.title == self.preset_var.get()), None)

    def _mode(self) -> str:
        return _key(MODE_LABELS, self.mode_var.get())

    def _layout(self) -> str:
        return _key(LAYOUT_LABELS, self.layout_var.get())

    def _apply_preset(self):
        key = self._preset_key()
        if key is None:
            return
        p = it.PRESETS[key]
        self.dpi_var.set(str(p.dpi))
        self.mode_var.set(MODE_LABELS[p.mode])
        if p.quality:
            self.quality_var.set(p.quality)
        self.clean_var.set(p.clean_background)
        self.autocrop_var.set(p.autocrop)
        if key == "id_doc" and not self.series_var.get():
            self.series_var.set(True)
        self._settings_changed(True)

    def _settings_changed(self, check_preset: bool = False):
        q = int(self.quality_var.get())
        mode = self._mode()
        if mode == "bw":
            self.quality_label.configure(text="не нужно для Ч/Б")
            self.quality_slider.configure(state="disabled", button_color=w.CARD_BORDER)
        else:
            desc = "очень компактно" if q < 60 else "оптимально" if q < 82 else "максимум деталей"
            self.quality_label.configure(text=f"{q} · {desc}")
            self.quality_slider.configure(state="normal", button_color=w.ACCENT)
        if check_preset:
            # Если параметры совпадают с профилем — показываем его, иначе «Свои настройки».
            match = next((k for k, p in it.PRESETS.items()
                          if p.dpi == int(self.dpi_var.get()) and p.mode == mode
                          and (p.mode == "bw" or p.quality == q) and p.clean_background == self.clean_var.get()
                          and p.autocrop == self.autocrop_var.get()), None)
            self.preset_var.set(it.PRESETS[match].title if match else CUSTOM_PRESET)
            self.preset_hint.configure(text=PRESET_HINTS.get(match, "Параметры выбраны вручную."))
        self._draw_preview()
        # Пересчёт размера — с задержкой, чтобы не считать на каждое движение ползунка.
        if self._estimate_after:
            self.after_cancel(self._estimate_after)
        self._estimate_after = self.after(500, self._update_estimate)

    def _update_estimate(self):
        self._estimate_after = None
        n_out = output_count(len(self.pages), self._layout())
        if not n_out:
            self.estimate_label.configure(text="Пока нет отсканированных страниц")
            return
        snap = [p.snapshot() for p in self.pages[:2]]
        mode, clean, layout, q = self._mode(), self.clean_var.get(), self._layout(), int(self.quality_var.get())
        self._estimate_token += 1
        token = self._estimate_token
        raw = sum(p.raw.width * p.raw.height * len(p.raw.getbands()) for p in self.pages)

        def work():
            img, dpi = output_page(snap, 0, mode, clean, layout)
            _, size = it.as_saved(img, q, dpi)
            return size * n_out

        def done(size):
            if token == self._estimate_token:  # результат устаревших настроек не показываем
                self.estimate_label.configure(
                    text=f"Страниц в файле: {n_out} · примерно {it.human_size(size)}\n"
                         f"без сжатия было бы {it.human_size(raw)}")

        threading.Thread(target=lambda: self.post(done, work()), daemon=True).start()

    def do_scan(self):
        idx = self._scanner_index()
        if idx < 0:
            w.info(self, "Проверьте, что МФУ включено и подключено (USB или сеть), а драйвер HP установлен. "
                         "Затем нажмите ↻ рядом со списком сканеров.", "Сканер не найден", "warning")
            return
        settings = sb.ScanSettings(dpi=int(self.dpi_var.get()), color=self._mode() == "color",
                                   source=_key(SOURCE_LABELS, self.source_var.get()),
                                   page_size=self.page_size_var.get())
        device = self.scanners[idx].id
        autocrop = self.autocrop_var.get()

        def work():
            images = sb.scan(device, settings)
            if autocrop:
                images = [it.autocrop_to_content(img, margin_px=settings.dpi // 25) for img in images]
            return images

        def done(images):
            for img in images:
                self.pages.append(ScannedPage(img, settings.dpi))
            self.selected = len(self.pages) - 1
            self._refresh_pages()
            self.status.set(f"Отсканировано страниц: {len(images)}")
            if self.series_var.get():
                self.after(150, self._ask_next_in_series)

        self.run_bg("Сканирование… не открывайте крышку", work, done)

    def _ask_next_in_series(self):
        n = len(self.pages)
        choice = w.Dialog.ask(
            self, f"Скан добавлен — в файле {n} стр.",
            "Положите следующий документ (например, разворот с пропиской) и нажмите «Сканировать следующий».\n\n"
            "Когда всё отсканировано — «Сохранить файл». Перед сохранением можно посмотреть каждую "
            "страницу на весь экран.",
            ["Сканировать следующий", "Сохранить файл", "Позже"], "success")
        if choice == 0:
            self.do_scan()
        elif choice == 1:
            self.do_save()

    def _scanner_index(self) -> int:
        names = [s.name for s in self.scanners]
        return names.index(self.scanner_var.get()) if self.scanner_var.get() in names else -1

    def _snapshot(self) -> list:
        return [p.snapshot() for p in self.pages]

    def open_fullscreen(self):
        if not self.pages:
            w.info(self, "Сначала отсканируйте документ.", "Нечего показать")
            return
        snap, mode, clean, layout = self._snapshot(), self._mode(), self.clean_var.get(), self._layout()
        q = int(self.quality_var.get())
        sel = max(self.selected, 0)
        start = sel if layout == "single" else sel // 2

        def render(i):
            img, dpi = output_page(snap, i, mode, clean, layout)
            saved, size = it.as_saved(img, q, dpi)
            codec = "PNG, 1 бит/пиксель" if img.mode == "1" else f"JPEG, качество {q}"
            mm = f"{img.width / dpi * 25.4:.0f}×{img.height / dpi * 25.4:.0f} мм"
            return saved, (f"Страница {i + 1} — так она будет выглядеть в файле  ·  {img.width}×{img.height} пикс. · "
                           f"{dpi} dpi · {mm} · {MODE_NAMES[mode]} · {codec} · {it.human_size(size)}")

        FullscreenViewer(self, output_count(len(snap), layout), render, start)

    def do_save(self):
        if not self.pages:
            w.info(self, "Сначала отсканируйте документ.", "Нечего сохранять")
            return
        fmt_by_ext = {v: k for k, v in it.SAVE_FORMATS.items()}
        last_ext = self.cfg.get("save_ext", ".pdf")
        if last_ext not in fmt_by_ext:
            last_ext = ".pdf"
        path = filedialog.asksaveasfilename(
            parent=self, title="Сохранить скан",
            initialdir=self.cfg.get("save_dir") or _documents_dir(),
            initialfile=f"Скан {dt.datetime.now():%Y-%m-%d %H-%M}{last_ext}",
            defaultextension=last_ext,
            filetypes=[(fmt_by_ext[last_ext], "*" + last_ext)] +
                      [(name, "*" + ext) for name, ext in it.SAVE_FORMATS.items() if ext != last_ext])
        if not path:
            return
        ext = os.path.splitext(path)[1].lower()
        if ext == ".jpeg":
            ext = ".jpg"
        if ext not in fmt_by_ext and ext != ".tiff":
            path, ext = path + ".pdf", ".pdf"  # неизвестное расширение — сохраняем как PDF
        self.cfg["save_dir"] = os.path.dirname(path)
        self.cfg["save_ext"] = ".tif" if ext == ".tiff" else ext
        snap, mode, clean, layout = self._snapshot(), self._mode(), self.clean_var.get(), self._layout()
        q = int(self.quality_var.get())

        def work():
            out = [output_page(snap, i, mode, clean, layout) for i in range(output_count(len(snap), layout))]
            return it.save_pages([o[0] for o in out], path, q, [o[1] for o in out])

        def done(files):
            total = sum(os.path.getsize(f) for f in files)
            name = os.path.basename(files[0]) + (f" и ещё {len(files) - 1}" if len(files) > 1 else "")
            choice = w.Dialog.ask(self, "Файл сохранён", f"{name}\nРазмер: {it.human_size(total)}",
                                  ["Открыть папку", "Открыть файл", "Закрыть"], "success")
            if choice == 0:
                _open_path(os.path.dirname(files[0]))
            elif choice == 1:
                _open_path(files[0])

        self.run_bg("Сохранение…", work, done)

    def do_copy(self):
        """Режим копира: печать отсканированных страниц."""
        if not self.pages:
            w.info(self, "Сначала отсканируйте документ.", "Нечего печатать")
            return
        printer = self.printer_var.get()
        if printer not in self.printers:
            w.info(self, "Выберите принтер на экране «Печать».", "Не выбран принтер", "warning")
            return
        opts = self._print_options()
        opts.page_range = ""
        mode = "bw" if self._mode() == "bw" else "gray"  # принтер монохромный
        snap, clean, layout = self._snapshot(), self.clean_var.get(), self._layout()

        def work():
            out = [output_page(snap, i, mode, clean, layout) for i in range(output_count(len(snap), layout))]
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "copy.pdf")
                it.save_pages([o[0] for o in out], path, 90, [o[1] for o in out])
                plan = pb.build_plan([path], opts)
                pb.execute(printer, plan, opts, self.set_status_threadsafe, self._ask_flip_threadsafe)

        self.run_bg("Печать копии…", work, lambda r: self.status.set("Копия отправлена на принтер"))

    # --- миниатюры и предпросмотр

    def _refresh_pages(self):
        for wdg in self._thumb_widgets:
            wdg.destroy()
        self._thumb_widgets.clear()
        if not self.pages:
            lbl = ctk.CTkLabel(self.strip, text="Здесь появятся отсканированные страницы", font=w.font(12),
                               text_color=w.MUTED)
            lbl.pack(side="left", padx=16, pady=40)
            self._thumb_widgets.append(lbl)
        for i, page in enumerate(self.pages):
            th = page.small().copy()
            th.thumbnail((80, 96))
            active = i == self.selected
            frame = ctk.CTkFrame(self.strip, fg_color=w.ACCENT_SOFT if active else "transparent",
                                 corner_radius=10, border_width=2,
                                 border_color=w.ACCENT if active else w.FIELD)
            frame.pack(side="left", padx=4, pady=4)
            pic = ctk.CTkLabel(frame, text="", image=ctk.CTkImage(th, th, size=th.size))
            pic.pack(padx=6, pady=(6, 0))
            num = ctk.CTkLabel(frame, text=str(i + 1), font=w.font(11, "bold"),
                               text_color=w.ACCENT if active else w.MUTED)
            num.pack(pady=(0, 2))
            for wdg in (frame, pic, num):
                wdg.bind("<Button-1>", lambda e, i=i: self._select(i))
                wdg.bind("<Double-Button-1>", lambda e, i=i: (self._select(i), self.open_fullscreen()))
            self._thumb_widgets.append(frame)
        n = len(self.pages)
        self.preview_title.configure(text=f"Предпросмотр · {self.selected + 1} из {n}" if n else "Предпросмотр")
        self.scan_button.configure(text=f"Сканировать следующий  (стр. {n + 1})" if n and self.series_var.get()
                                   else "Сканировать")
        self._draw_preview()
        self._settings_changed()

    def _select(self, i: int):
        self.selected = i
        self._refresh_pages()

    def _draw_preview(self):
        if not hasattr(self, "preview"):
            return
        c = self.preview
        c.delete("all")
        cw, ch = c.winfo_width(), c.winfo_height()
        if not (0 <= self.selected < len(self.pages)):
            c.create_text(cw // 2, ch // 2 - 30, text="▣", fill="#3A3D45", font=w.font(54))
            c.create_text(cw // 2, ch // 2 + 30, fill="#98A2B3", font=w.font(14), justify="center",
                          text="Положите документ на стекло или в автоподатчик\nи нажмите «Сканировать»")
            return
        img = it.process(self.pages[self.selected].small(), self._mode(), self.clean_var.get())
        img = img.convert("L" if img.mode == "1" else img.mode)
        img.thumbnail((max(cw - 24, 50), max(ch - 40, 50)))
        self._preview_ref = ImageTk.PhotoImage(img)
        c.create_image(cw // 2, (ch - 16) // 2, image=self._preview_ref)
        c.create_text(cw // 2, ch - 10, text="Двойной щелчок — на весь экран в реальном качестве",
                      fill="#667085", font=w.font(11))

    def _rotate(self, deg: int):
        if 0 <= self.selected < len(self.pages):
            p = self.pages[self.selected]
            p.rotation = (p.rotation + deg) % 360
            self._refresh_pages()

    def _move(self, delta: int):
        i, j = self.selected, self.selected + delta
        if 0 <= i < len(self.pages) and 0 <= j < len(self.pages):
            self.pages[i], self.pages[j] = self.pages[j], self.pages[i]
            self.selected = j
            self._refresh_pages()

    def _autocrop(self):
        if 0 <= self.selected < len(self.pages):
            p = self.pages[self.selected]
            p.raw = it.autocrop_to_content(p.raw, margin_px=p.dpi // 25)
            p._small = None
            self._refresh_pages()

    def _delete_page(self):
        if 0 <= self.selected < len(self.pages):
            del self.pages[self.selected]
            self.selected = min(self.selected, len(self.pages) - 1)
            self._refresh_pages()

    def _clear_pages(self):
        if self.pages and w.ask_yes_no(self, "Все отсканированные страницы будут удалены.", "Очистить всё?",
                                       yes="Удалить", kind="warning"):
            self.pages.clear()
            self.selected = -1
            self._refresh_pages()

    # ================================================================ экран «Печать»

    def _build_print_screen(self, root):
        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(0, weight=1)
        self.printers: list[str] = []

        files = w.Card(root)
        files.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        head = ctk.CTkFrame(files, fg_color="transparent")
        head.pack(fill="x", padx=16, pady=(14, 8))
        ctk.CTkLabel(head, text="Файлы для печати", font=w.font(15, "bold"), text_color=w.TEXT).pack(side="left")
        w.primary_button(head, "＋  Добавить файлы", self._pick_print_files, height=34, width=170).pack(side="right")
        w.tool_button(head, "Очистить", self._clear_print_files, width=90).pack(side="right", padx=6)
        self.files_frame = ctk.CTkScrollableFrame(files, fg_color=w.FIELD, corner_radius=12)
        self.files_frame.pack(fill="both", expand=True, padx=16, pady=(0, 16))

        opts = w.Card(root, "Параметры печати", width=340)
        opts.grid(row=0, column=1, sticky="ns")
        opts.pack_propagate(False)
        pad = {"fill": "x", "padx": 16}

        row = ctk.CTkFrame(opts, fg_color="transparent")
        row.pack(**pad)
        self.printer_var = tk.StringVar(value="Поиск…")
        self.printer_menu = w.option_menu(row, [], self.printer_var, lambda v: self.refresh_printer_state())
        self.printer_menu.pack(side="left", fill="x", expand=True)
        w.tool_button(row, "↻", self.refresh_devices).pack(side="left", padx=(6, 0))

        self._label(opts, "Копий")
        row = ctk.CTkFrame(opts, fg_color="transparent")
        row.pack(**pad)
        self.copies_var = tk.StringVar(value="1")
        w.secondary_button(row, "−", lambda: self._step_copies(-1), width=40).pack(side="left")
        ctk.CTkEntry(row, textvariable=self.copies_var, width=70, height=36, justify="center", corner_radius=8,
                     fg_color=w.FIELD, border_width=0, font=w.font(14, "bold"), text_color=w.TEXT).pack(
            side="left", padx=6)
        w.secondary_button(row, "+", lambda: self._step_copies(1), width=40).pack(side="left")

        self._label(opts, "Страницы")
        # Без textvariable: иначе CTkEntry не показывает текст-подсказку.
        self.range_entry = ctk.CTkEntry(opts, height=36, corner_radius=8, fg_color=w.FIELD, border_width=0,
                                        font=w.font(13), text_color=w.TEXT,
                                        placeholder_text="все  (пример: 1-3, 5, 8-)")
        self.range_entry.pack(**pad)
        self.range_entry.bind("<KeyRelease>", lambda e: self._update_page_count())

        self._label(opts, "Масштаб")
        self.fit_var = tk.StringVar(value="Вписать в лист")
        w.segmented(opts, ["Вписать в лист", "100 %"], self.fit_var).pack(**pad)
        self._label(opts, "Качество")
        self.print_dpi_var = tk.StringVar(value="600 dpi")
        w.segmented(opts, ["600 dpi", "300 dpi (быстрее)"], self.print_dpi_var).pack(**pad)

        self.duplex_var = tk.BooleanVar(value=False)
        w.switch(opts, "Двусторонняя (вручную)", self.duplex_var, self._update_page_count).pack(**pad, pady=(16, 6))
        self.reverse_var = tk.BooleanVar(value=True)
        self.rotate_back_var = tk.BooleanVar(value=self.cfg.get("rotate_back", False))
        for text, var in (("Обратный порядок 2-й стороны", self.reverse_var),
                          ("Повернуть 2-ю сторону на 180°", self.rotate_back_var)):
            ctk.CTkCheckBox(opts, text=text, variable=var, font=w.font(12), text_color=w.TEXT, checkbox_width=18,
                            checkbox_height=18, corner_radius=5, fg_color=w.ACCENT, hover_color=w.ACCENT_HOVER,
                            border_color=w.CARD_BORDER).pack(**pad, pady=3)

        self.pages_info = ctk.CTkLabel(opts, text="", font=w.font(12), text_color=w.MUTED, wraplength=300,
                                       justify="left", anchor="w")
        self.pages_info.pack(**pad, pady=(12, 0))
        w.primary_button(opts, "Печать", self.do_print, height=50).pack(side="bottom", fill="x", padx=16, pady=16)

        self.copies_var.trace_add("write", lambda *a: self._update_page_count())
        self._render_files()

    def _step_copies(self, d: int):
        try:
            n = int(self.copies_var.get())
        except ValueError:
            n = 1
        self.copies_var.set(str(min(99, max(1, n + d))))

    def _pick_print_files(self):
        files = filedialog.askopenfilenames(
            parent=self, title="Выберите файлы для печати",
            filetypes=[("Поддерживаемые файлы", "*.pdf *.jpg *.jpeg *.png *.bmp *.gif *.tif *.tiff *.webp "
                                                "*.doc *.docx *.xls *.xlsx *.txt *.rtf *.odt"),
                       ("PDF", "*.pdf"), ("Изображения", "*.jpg *.jpeg *.png *.bmp *.tif *.tiff"),
                       ("Все файлы", "*.*")])
        self.add_print_files(list(files))

    def add_print_files(self, files: list[str]):
        self.print_files += [f for f in files if os.path.isfile(f)]
        self._render_files()

    def _clear_print_files(self):
        self.print_files.clear()
        self._render_files()

    def _remove_print_file(self, i: int):
        del self.print_files[i]
        self._render_files()

    def _render_files(self):
        for wdg in self.files_frame.winfo_children():
            wdg.destroy()
        if not self.print_files:
            ctk.CTkLabel(self.files_frame, text="▤", font=w.font(44), text_color=w.CARD_BORDER).pack(pady=(70, 4))
            ctk.CTkLabel(self.files_frame, text="Нажмите «Добавить файлы», чтобы выбрать\nPDF, фотографии или "
                                                "документы Word и Excel", font=w.font(13), text_color=w.MUTED,
                         justify="center").pack()
        for i, path in enumerate(self.print_files):
            row = ctk.CTkFrame(self.files_frame, fg_color=w.CARD, corner_radius=10)
            row.pack(fill="x", pady=3, padx=2)
            ext = os.path.splitext(path)[1].lower().lstrip(".")[:4].upper() or "?"
            ctk.CTkLabel(row, text=ext, width=46, height=32, corner_radius=8, fg_color=w.ACCENT_SOFT,
                         text_color=w.ACCENT, font=w.font(11, "bold")).pack(side="left", padx=10, pady=8)
            try:
                n = pb.count_pages(path)
                pages = f"{n} стр." if n else "печать через связанную программу"
            except Exception:  # noqa: BLE001
                pages = "не удалось прочитать файл"
            text = ctk.CTkFrame(row, fg_color="transparent")
            text.pack(side="left", fill="x", expand=True)
            ctk.CTkLabel(text, text=os.path.basename(path), font=w.font(13, "bold"), text_color=w.TEXT,
                         anchor="w").pack(fill="x")
            ctk.CTkLabel(text, text=f"{pages}  ·  {os.path.dirname(path)}", font=w.font(11), text_color=w.MUTED,
                         anchor="w").pack(fill="x")
            w.tool_button(row, "✕", lambda i=i: self._remove_print_file(i)).pack(side="right", padx=8)
        self._update_page_count()

    def _page_range(self) -> str:
        text = self.range_entry.get()
        return "" if text == self.range_entry.cget("placeholder_text") else text

    def _print_options(self) -> pb.PrintOptions:
        try:
            copies = max(1, int(self.copies_var.get()))
        except ValueError:
            copies = 1
        return pb.PrintOptions(copies=copies, page_range=self._page_range(),
                               fit=self.fit_var.get() != "100 %",
                               dpi=600 if self.print_dpi_var.get().startswith("600") else 300,
                               manual_duplex=self.duplex_var.get(), reverse_second_side=self.reverse_var.get(),
                               rotate_second_side=self.rotate_back_var.get())

    def _update_page_count(self):
        if not hasattr(self, "pages_info"):
            return
        if not self.print_files:
            self.pages_info.configure(text="")
            return
        try:
            plan = pb.build_plan(self.print_files, self._print_options())
        except Exception as e:  # noqa: BLE001
            self.pages_info.configure(text=f"⚠ {e}", text_color=w.WARNING)
            return
        sides = sum(len(p) for p in plan.passes)
        sheets = len(plan.passes[0]) if plan.passes else 0
        text = f"Будет напечатано: {sides} стр. на {sheets} листах" if sides else ""
        if len(self.print_files) > 1 and self._page_range().strip():
            text += "\nДиапазон страниц работает, только когда выбран один файл."
        if plan.other_files:
            text += f"\nЕщё {len(plan.other_files)} файл(ов) напечатает связанная программа (Word, Блокнот…)."
        self.pages_info.configure(text=text.strip(), text_color=w.MUTED)

    def _ask_flip_threadsafe(self, sheets: int) -> bool:
        return self.call_in_ui(lambda: w.Dialog.ask(
            self, "Переверните стопку",
            f"Первая сторона напечатана ({sheets} л.).\n\n"
            "1. Дождитесь, пока выйдет последний лист.\n"
            "2. Возьмите всю стопку из выходного лотка, не меняя порядок листов.\n"
            "3. Положите её во входной лоток чистой стороной вверх, тем же краем вперёд.\n\n"
            "Если оборот окажется вверх ногами — в следующий раз включите «Повернуть 2-ю сторону на 180°».",
            ["Печатать вторую сторону", "Отмена"], "question") == 0)

    def do_print(self):
        printer = self.printer_var.get()
        if not self.print_files:
            w.info(self, "Нажмите «Добавить файлы» и выберите, что печатать.", "Нет файлов")
            return
        if printer not in self.printers:
            w.info(self, "Подключите МФУ и нажмите ↻ рядом со списком принтеров.", "Принтер не выбран", "warning")
            return
        opts = self._print_options()
        files = list(self.print_files)
        self.cfg["printer"] = printer
        self.cfg["rotate_back"] = opts.rotate_second_side

        def work():
            plan = pb.build_plan(files, opts)
            pb.execute(printer, plan, opts, self.set_status_threadsafe, self._ask_flip_threadsafe)

        self.run_bg("Печать…", work, lambda r: self.status.set("Задание отправлено на принтер"))

    # ================================================================ экран «Принтер»

    def _build_printer_screen(self, root):
        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(1, weight=1)

        card = w.Card(root)
        card.grid(row=0, column=0, sticky="ew")
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=20, pady=18)
        self.state_dot = ctk.CTkLabel(row, text="", width=52, height=52, corner_radius=26, fg_color=w.FIELD)
        self.state_dot.pack(side="left")
        text = ctk.CTkFrame(row, fg_color="transparent")
        text.pack(side="left", padx=16)
        self.state_name = ctk.CTkLabel(text, text="—", font=w.font(17, "bold"), text_color=w.TEXT, anchor="w")
        self.state_name.pack(fill="x")
        self.state_text = ctk.CTkLabel(text, text="", font=w.font(13), text_color=w.MUTED, anchor="w")
        self.state_text.pack(fill="x")
        w.secondary_button(row, "↻  Обновить", self.refresh_printer_state, width=120).pack(side="right")

        card = w.Card(root, "Очередь печати")
        card.grid(row=1, column=0, sticky="nsew", pady=12)
        self.jobs_frame = ctk.CTkScrollableFrame(card, fg_color=w.FIELD, corner_radius=12)
        self.jobs_frame.pack(fill="both", expand=True, padx=16, pady=(0, 16))

        actions = ctk.CTkFrame(root, fg_color="transparent")
        actions.grid(row=2, column=0, sticky="ew")
        tiles = [("Очистить очередь", "Если печать «зависла»", self._clear_queue),
                 ("Веб-интерфейс", "Тонер, сеть, настройки МФУ", self._open_ews)]
        if sys.platform == "win32":
            tiles.append(("Настройки драйвера", "EconoMode, плотность, бумага", self._driver_settings))
        actions.grid_columnconfigure(tuple(range(len(tiles))), weight=1, uniform="tile")
        for i, (title, sub, cmd) in enumerate(tiles):
            tile = w.Card(actions)
            tile.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 6, 0 if i == len(tiles) - 1 else 6))
            ctk.CTkLabel(tile, text=title, font=w.font(14, "bold"), text_color=w.TEXT, anchor="w").pack(
                fill="x", padx=16, pady=(14, 0))
            ctk.CTkLabel(tile, text=sub, font=w.font(12), text_color=w.MUTED, anchor="w").pack(fill="x", padx=16)
            w.secondary_button(tile, "Открыть", cmd, height=32).pack(anchor="w", padx=16, pady=(8, 14))

    def refresh_printer_state(self):
        printer = self.printer_var.get()
        if printer not in getattr(self, "printers", []):
            return

        def work():
            return pb.printer_status(printer)

        def done(res):
            state, jobs = res
            ok = state == "готов" or state.startswith("printer") and "idle" in state
            bad = any(s in state for s in ("ошибка", "замятие", "нет бумаги", "нет тонера", "не в сети", "дверца"))
            color = w.DANGER if bad else (w.SUCCESS if ok else w.WARNING)
            self.state_dot.configure(fg_color=color)
            self.state_name.configure(text=printer)
            self.state_text.configure(text=f"Состояние: {state}")
            self.side_dot.configure(text_color=color)
            self.side_state.configure(text=state if len(state) < 26 else state[:24] + "…")
            for wdg in self.jobs_frame.winfo_children():
                wdg.destroy()
            if not jobs:
                ctk.CTkLabel(self.jobs_frame, text="Очередь пуста — принтер ничего не печатает",
                             font=w.font(13), text_color=w.MUTED).pack(pady=30)
            for j in jobs:
                r = ctk.CTkFrame(self.jobs_frame, fg_color=w.CARD, corner_radius=10)
                r.pack(fill="x", pady=3, padx=2)
                ctk.CTkLabel(r, text=j, font=w.font(13), text_color=w.TEXT, anchor="w").pack(fill="x", padx=12,
                                                                                             pady=10)

        if not self._busy:
            self.run_bg("Запрос состояния принтера…", work, done)

    def _clear_queue(self):
        printer = self.printer_var.get()
        if printer in self.printers and w.ask_yes_no(self, "Все задания этого принтера будут отменены.",
                                                     "Очистить очередь?", yes="Очистить", kind="warning"):
            self.run_bg("Очистка очереди…", lambda: pb.clear_queue(printer),
                        lambda r: self.refresh_printer_state())

    def _open_ews(self):
        printer = self.printer_var.get()
        try:
            ip = pb.printer_ip(printer) if printer in self.printers else None
        except Exception:  # noqa: BLE001
            ip = None
        if ip:
            webbrowser.open(f"http://{ip}/")
        else:
            w.info(self, "Не удалось определить IP-адрес принтера — возможно, он подключён по USB. Адрес можно "
                         "узнать из отчёта о конфигурации: на панели МФУ кнопка «Настройка» (гаечный ключ) → "
                         "«Отчеты» → «Отчет о конфигурации».", "Адрес не найден", "warning")

    def _driver_settings(self):
        import subprocess

        subprocess.Popen(["rundll32", "printui.dll,PrintUIEntry", "/e", "/n", self.printer_var.get()])

    # ================================================================ общее

    def refresh_devices(self):
        def work():
            errors = []
            try:
                printers, default = pb.list_printers()
            except Exception as e:  # noqa: BLE001
                printers, default = [], ""
                errors.append(f"принтеры: {e}")
            try:
                scanners = sb.list_scanners()
            except Exception as e:  # noqa: BLE001
                scanners = []
                errors.append(f"сканеры: {e}")
            return printers, default, scanners, errors

        def done(res):
            printers, default, self.scanners, errors = res
            self.printers = printers
            self.printer_menu.configure(values=printers or ["Принтеры не найдены"])
            saved = self.cfg.get("printer")
            self.printer_var.set(saved if saved in printers else default if default in printers
                                 else (printers[0] if printers else "Принтеры не найдены"))
            names = [s.name for s in self.scanners]
            self.scanner_menu.configure(values=names or ["Сканер не найден"])
            self.scanner_var.set(names[sb.pick_default(self.scanners)] if names else "Сканер не найден")
            msg = f"Найдено принтеров: {len(printers)}, сканеров: {len(self.scanners)}"
            if errors:
                msg += "  ·  ошибка — " + "; ".join(errors)
            self.status.set(msg)
            if not printers:
                self.side_state.configure(text="не найден")
                self.side_dot.configure(text_color=w.DANGER)
            self.after(50, self.refresh_printer_state)

        self.run_bg("Поиск устройств…", work, done)

    def _load_cfg(self) -> dict:
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _on_close(self):
        self.cfg.update(dpi=int(self.dpi_var.get()), mode=self._mode(), quality=int(self.quality_var.get()),
                        clean=self.clean_var.get(), autocrop=self.autocrop_var.get(),
                        source=_key(SOURCE_LABELS, self.source_var.get()), page_size=self.page_size_var.get(),
                        series=self.series_var.get(), layout=self._layout())
        if self._preset_key():
            self.cfg["preset"] = self._preset_key()
        if self.printer_var.get() in self.printers:
            self.cfg["printer"] = self.printer_var.get()
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, ensure_ascii=False, indent=2)
        except OSError:
            pass
        self.destroy()


def _documents_dir() -> str:
    docs = os.path.join(os.path.expanduser("~"), "Documents")
    return docs if os.path.isdir(docs) else os.path.expanduser("~")


def _open_path(path: str):
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606
    else:
        import subprocess

        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])


LOG_PATH = os.path.join(os.path.expanduser("~"), "hp_m1212nf_error.log")


def _report_crash(exc_type, exc, tb, parent=None):
    """Пишет ошибку в журнал и показывает её — при запуске через pythonw консоли нет."""
    import traceback

    text = "".join(traceback.format_exception(exc_type, exc, tb))
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"\n=== {dt.datetime.now():%Y-%m-%d %H:%M:%S} ===\n{text}")
    except OSError:
        pass
    message = f"{exc}\n\nПодробности записаны в файл:\n{LOG_PATH}"
    try:
        if parent is not None:
            w.error(parent, message, "Непредвиденная ошибка")
        else:
            from tkinter import messagebox

            messagebox.showerror(APP_TITLE, message)
    except Exception:  # noqa: BLE001
        pass


def main():
    try:
        app = App(sys.argv[1:])
    except Exception:  # noqa: BLE001
        _report_crash(*sys.exc_info())
        raise
    app.report_callback_exception = lambda *exc: _report_crash(*exc, parent=app)
    app.mainloop()


if __name__ == "__main__":
    main()
