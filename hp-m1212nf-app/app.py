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
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

import image_tools as it
import print_backend as pb
import scan_backend as sb

APP_TITLE = "HP LaserJet M1212nf MFP"
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".hp_m1212nf_app.json")
MODE_LABELS = {"bw": "Ч/Б (текст)", "gray": "Оттенки серого", "color": "Цветной"}
DPI_CHOICES = ["150", "200", "300", "400", "600", "1200"]
PREVIEW_MAX = 1400  # длинная сторона уменьшенной копии для предпросмотра


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

    def full(self) -> Image.Image:
        return it.rotate(self.raw, self.rotation)


class App(tk.Tk):
    def __init__(self, files_to_print: list[str]):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1100x720")
        self.minsize(900, 600)
        self.cfg = self._load_cfg()
        self._jobs: queue.Queue = queue.Queue()
        self._busy = False
        self.pages: list[ScannedPage] = []
        self.scanners: list[sb.ScannerInfo] = []
        self.selected = -1
        self._thumb_refs: list[ImageTk.PhotoImage] = []
        self._preview_ref = None
        self._estimate_after = None

        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Big.TButton", font=("Segoe UI", 11, "bold"), padding=8)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=(8, 0))
        self.scan_tab = ttk.Frame(nb, padding=8)
        self.print_tab = ttk.Frame(nb, padding=8)
        self.printer_tab = ttk.Frame(nb, padding=8)
        nb.add(self.scan_tab, text="  Сканирование  ")
        nb.add(self.print_tab, text="  Печать  ")
        nb.add(self.printer_tab, text="  Принтер  ")

        self.status = tk.StringVar(value="Готово")
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=4)
        self.progress = ttk.Progressbar(bar, mode="indeterminate", length=160)
        self.progress.pack(side="right")
        ttk.Label(bar, textvariable=self.status).pack(side="left")

        self._build_scan_tab()
        self._build_print_tab()
        self._build_printer_tab()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll_jobs)
        self.after(200, self.refresh_devices)

        if files_to_print:
            self.add_print_files(files_to_print)
            nb.select(self.print_tab)

    # ------------------------------------------------------------ фоновые задачи

    def run_bg(self, title: str, func, on_done=None):
        """Выполняет func() в фоновом потоке, чтобы окно не «зависало»."""
        if self._busy:
            messagebox.showinfo(APP_TITLE, "Подождите, предыдущая операция ещё выполняется.")
            return
        self._busy = True
        self.status.set(title)
        self.progress.start(12)

        def worker():
            try:
                result = func()
                self._jobs.put((on_done, result, None))
            except Exception as e:  # noqa: BLE001 — показываем пользователю любую ошибку
                self._jobs.put((None, None, e))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_jobs(self):
        try:
            while True:
                item = self._jobs.get_nowait()
                if item[0] == "ui":  # обновление интерфейса из рабочего потока
                    item[1](*item[2])
                    continue
                if item[0] == "call":  # вызов в главном потоке по просьбе рабочего
                    _, fn, box = item
                    box["result"] = fn()
                    box["event"].set()
                    continue
                on_done, result, err = item
                self._busy = False
                self.progress.stop()
                if err:
                    self.status.set("Ошибка")
                    messagebox.showerror(APP_TITLE, str(err) or err.__class__.__name__)
                else:
                    self.status.set("Готово")
                    if on_done:
                        on_done(result)
        except queue.Empty:
            pass
        self.after(100, self._poll_jobs)

    def call_in_ui(self, fn):
        """Из рабочего потока: выполнить fn в главном потоке и дождаться результата."""
        box = {"event": threading.Event()}
        self._jobs.put(("call", fn, box))
        box["event"].wait()
        return box["result"]

    def post(self, fn, *args):
        """Из рабочего потока: выполнить fn(*args) в главном потоке (Tk не потокобезопасен)."""
        self._jobs.put(("ui", fn, args))

    def set_status_threadsafe(self, text: str):
        self.post(self.status.set, text)

    # ------------------------------------------------------------ вкладка «Сканирование»

    def _build_scan_tab(self):
        left = ttk.Frame(self.scan_tab)
        left.pack(side="left", fill="y", padx=(0, 10))
        right = ttk.Frame(self.scan_tab)
        right.pack(side="left", fill="both", expand=True)

        grp = ttk.LabelFrame(left, text="Сканер", padding=8)
        grp.pack(fill="x")
        self.scanner_var = tk.StringVar()
        self.scanner_box = ttk.Combobox(grp, textvariable=self.scanner_var, state="readonly", width=34)
        self.scanner_box.pack(fill="x")
        ttk.Button(grp, text="Обновить список", command=self.refresh_devices).pack(anchor="e", pady=(4, 0))

        grp = ttk.LabelFrame(left, text="Откуда сканировать", padding=8)
        grp.pack(fill="x", pady=6)
        self.source_var = tk.StringVar(value=self.cfg.get("source", "flatbed"))
        ttk.Radiobutton(grp, text="Стекло (одна страница)", value="flatbed", variable=self.source_var).pack(anchor="w")
        ttk.Radiobutton(grp, text="Автоподатчик (все листы)", value="feeder", variable=self.source_var).pack(anchor="w")

        grp = ttk.LabelFrame(left, text="Качество", padding=8)
        grp.pack(fill="x")
        ttk.Label(grp, text="Профиль:").grid(row=0, column=0, sticky="w")
        self.preset_var = tk.StringVar()
        presets = ttk.Combobox(grp, textvariable=self.preset_var, state="readonly", width=40,
                               values=[p.title for p in it.PRESETS.values()])
        presets.grid(row=1, column=0, columnspan=2, sticky="we", pady=(0, 6))
        presets.bind("<<ComboboxSelected>>", lambda e: self._apply_preset())

        ttk.Label(grp, text="Разрешение, dpi:").grid(row=2, column=0, sticky="w")
        self.dpi_var = tk.StringVar(value=str(self.cfg.get("dpi", 300)))
        ttk.Combobox(grp, textvariable=self.dpi_var, values=DPI_CHOICES, width=8, state="readonly").grid(
            row=2, column=1, sticky="e")

        ttk.Label(grp, text="Цвет:").grid(row=3, column=0, sticky="w", pady=4)
        self.mode_var = tk.StringVar(value=MODE_LABELS[self.cfg.get("mode", "gray")])
        mode_box = ttk.Combobox(grp, textvariable=self.mode_var, values=list(MODE_LABELS.values()),
                                width=16, state="readonly")
        mode_box.grid(row=3, column=1, sticky="e")
        mode_box.bind("<<ComboboxSelected>>", lambda e: self._settings_changed())

        ttk.Label(grp, text="Сжатие JPEG:").grid(row=4, column=0, sticky="w")
        self.quality_var = tk.IntVar(value=self.cfg.get("quality", 75))
        self.quality_label = ttk.Label(grp, width=4)
        self.quality_label.grid(row=4, column=1, sticky="e")
        ttk.Scale(grp, from_=40, to=95, variable=self.quality_var,
                  command=lambda v: self._settings_changed()).grid(row=5, column=0, columnspan=2, sticky="we")
        ttk.Label(grp, text="← меньше файл        лучше качество →", foreground="gray").grid(
            row=6, column=0, columnspan=2)

        self.clean_var = tk.BooleanVar(value=self.cfg.get("clean", True))
        ttk.Checkbutton(grp, text="Белый фон (убрать серость бумаги)", variable=self.clean_var,
                        command=self._settings_changed).grid(row=7, column=0, columnspan=2, sticky="w", pady=(6, 0))

        ttk.Label(grp, text="Формат листа:").grid(row=8, column=0, sticky="w", pady=(6, 0))
        self.page_size_var = tk.StringVar(value=self.cfg.get("page_size", "A4"))
        ttk.Combobox(grp, textvariable=self.page_size_var, values=list(sb.PAGE_SIZES), width=8,
                     state="readonly").grid(row=8, column=1, sticky="e", pady=(6, 0))
        grp.columnconfigure(0, weight=1)

        ttk.Button(left, text="Сканировать", style="Big.TButton", command=self.do_scan).pack(fill="x", pady=(10, 4))
        ttk.Button(left, text="Сохранить…", command=self.do_save).pack(fill="x", pady=2)
        ttk.Button(left, text="Напечатать копию", command=self.do_copy).pack(fill="x", pady=2)
        self.estimate_var = tk.StringVar(value="Страниц: 0")
        ttk.Label(left, textvariable=self.estimate_var, wraplength=260, justify="left").pack(anchor="w", pady=6)

        # Правая часть: большой предпросмотр + лента миниатюр.
        tools = ttk.Frame(right)
        tools.pack(fill="x")
        ttk.Button(tools, text="⟲ Повернуть", command=lambda: self._rotate(-90)).pack(side="left")
        ttk.Button(tools, text="⟳ Повернуть", command=lambda: self._rotate(90)).pack(side="left", padx=4)
        ttk.Button(tools, text="◀ Раньше", command=lambda: self._move(-1)).pack(side="left")
        ttk.Button(tools, text="Позже ▶", command=lambda: self._move(1)).pack(side="left", padx=4)
        ttk.Button(tools, text="Обрезать поля", command=self._autocrop).pack(side="left")
        ttk.Button(tools, text="Удалить страницу", command=self._delete_page).pack(side="left", padx=4)
        ttk.Button(tools, text="Очистить всё", command=self._clear_pages).pack(side="right")

        self.preview = tk.Canvas(right, background="#5a5a5a", highlightthickness=0)
        self.preview.pack(fill="both", expand=True, pady=6)
        self.preview.bind("<Configure>", lambda e: self._draw_preview())

        strip_frame = ttk.Frame(right)
        strip_frame.pack(fill="x")
        self.strip = tk.Canvas(strip_frame, height=150, background="#3c3c3c", highlightthickness=0)
        sbar = ttk.Scrollbar(strip_frame, orient="horizontal", command=self.strip.xview)
        self.strip.configure(xscrollcommand=sbar.set)
        self.strip.pack(fill="x")
        sbar.pack(fill="x")

        preset_key = self.cfg.get("preset", it.DEFAULT_PRESET)
        self.preset_var.set(it.PRESETS.get(preset_key, it.PRESETS[it.DEFAULT_PRESET]).title)
        self._settings_changed()

    def _preset_key(self) -> str:
        return next((k for k, p in it.PRESETS.items() if p.title == self.preset_var.get()), it.DEFAULT_PRESET)

    def _mode(self) -> str:
        return next(k for k, v in MODE_LABELS.items() if v == self.mode_var.get())

    def _apply_preset(self):
        p = it.PRESETS[self._preset_key()]
        self.dpi_var.set(str(p.dpi))
        self.mode_var.set(MODE_LABELS[p.mode])
        if p.quality:
            self.quality_var.set(p.quality)
        self.clean_var.set(p.clean_background)
        self._settings_changed()

    def _settings_changed(self):
        self.quality_label.configure(text=str(int(self.quality_var.get())))
        self._draw_preview()
        # Пересчёт оценки размера — с задержкой, чтобы не считать на каждое движение ползунка.
        if self._estimate_after:
            self.after_cancel(self._estimate_after)
        self._estimate_after = self.after(500, self._update_estimate)

    def _update_estimate(self):
        self._estimate_after = None
        n = len(self.pages)
        if not n:
            self.estimate_var.set("Страниц: 0")
            return
        page, mode, clean, q = self.pages[0], self._mode(), self.clean_var.get(), int(self.quality_var.get())

        def work():
            img = it.process(page.full(), mode, clean)
            return it.estimate_size([img], q, page.dpi) * n

        def done(size):
            raw = sum(p.raw.width * p.raw.height * len(p.raw.getbands()) for p in self.pages)
            self.estimate_var.set(f"Страниц: {n}\nПримерный размер PDF: {it.human_size(size)}\n"
                                  f"(без сжатия было бы {it.human_size(raw)})")

        threading.Thread(target=lambda: self.post(done, work()), daemon=True).start()

    def _scan_settings(self) -> sb.ScanSettings:
        return sb.ScanSettings(dpi=int(self.dpi_var.get()), color=self._mode() == "color",
                               source=self.source_var.get(), page_size=self.page_size_var.get())

    def do_scan(self):
        idx = self.scanner_box.current()
        if idx < 0 or not self.scanners:
            messagebox.showwarning(APP_TITLE, "Сканер не найден.\n\nПроверьте, что МФУ включено, подключено "
                                              "(USB или сеть) и установлен драйвер HP. Затем нажмите "
                                              "«Обновить список».")
            return
        settings = self._scan_settings()
        device = self.scanners[idx].id

        def done(images):
            for img in images:
                self.pages.append(ScannedPage(img, settings.dpi))
            self.selected = len(self.pages) - 1
            self._refresh_pages()
            self.status.set(f"Отсканировано страниц: {len(images)}")

        self.run_bg("Сканирование… (не открывайте крышку)", lambda: sb.scan(device, settings), done)

    def do_save(self):
        if not self.pages:
            messagebox.showinfo(APP_TITLE, "Сначала отсканируйте документ.")
            return
        fmt_by_ext = {v: k for k, v in it.SAVE_FORMATS.items()}
        last_ext = self.cfg.get("save_ext", ".pdf")
        if last_ext not in fmt_by_ext:
            last_ext = ".pdf"
        path = filedialog.asksaveasfilename(
            title="Сохранить скан",
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
        q = int(self.quality_var.get())
        mode, clean, pages = self._mode(), self.clean_var.get(), list(self.pages)

        def work():
            imgs = [it.process(p.full(), mode, clean) for p in pages]
            return it.save_pages(imgs, path, q, [p.dpi for p in pages])

        def done(files):
            total = sum(os.path.getsize(f) for f in files)
            msg = f"Сохранено: {os.path.basename(files[0])}" + (f" и ещё {len(files) - 1}" if len(files) > 1 else "")
            if messagebox.askyesno(APP_TITLE, f"{msg}\nРазмер: {it.human_size(total)}\n\nОткрыть папку?"):
                _open_path(os.path.dirname(files[0]))

        self.run_bg("Сохранение…", work, done)

    def do_copy(self):
        """Режим копира: печать отсканированных страниц."""
        if not self.pages:
            messagebox.showinfo(APP_TITLE, "Сначала отсканируйте документ.")
            return
        printer = self.printer_var.get()
        if not printer:
            messagebox.showwarning(APP_TITLE, "Не выбран принтер (вкладка «Печать»).")
            return
        opts = self._print_options()
        opts.page_range = ""
        mode = "bw" if self._mode() == "bw" else "gray"  # принтер монохромный
        clean, pages = self.clean_var.get(), list(self.pages)

        def work():
            imgs = [it.process(p.full(), mode, clean) for p in pages]
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "copy.pdf")
                it.save_pages(imgs, path, 90, [p.dpi for p in pages])
                plan = pb.build_plan([path], opts)
                pb.execute(printer, plan, opts, self.set_status_threadsafe, self._ask_flip_threadsafe)

        self.run_bg("Печать копии…", work)

    # --- миниатюры и предпросмотр

    def _refresh_pages(self):
        self.strip.delete("all")
        self._thumb_refs.clear()
        x = 8
        for i, page in enumerate(self.pages):
            th = page.small().copy()
            th.thumbnail((100, 130))
            ref = ImageTk.PhotoImage(th)
            self._thumb_refs.append(ref)
            if i == self.selected:
                self.strip.create_rectangle(x - 4, 4, x + th.width + 4, 10 + th.height + 4,
                                            outline="#4aa3ff", width=3)
            item = self.strip.create_image(x, 10, anchor="nw", image=ref)
            self.strip.create_text(x + th.width // 2, 140, text=str(i + 1), fill="white")
            self.strip.tag_bind(item, "<Button-1>", lambda e, i=i: self._select(i))
            x += th.width + 16
        self.strip.configure(scrollregion=(0, 0, x, 150))
        self._draw_preview()
        self._settings_changed()

    def _select(self, i: int):
        self.selected = i
        self._refresh_pages()

    def _draw_preview(self):
        c = self.preview
        c.delete("all")
        if not (0 <= self.selected < len(self.pages)):
            c.create_text(c.winfo_width() // 2, c.winfo_height() // 2, fill="white", font=("Segoe UI", 12),
                          text="Положите документ на стекло (лицом вниз)\nили в автоподатчик (лицом вверх)\n"
                               "и нажмите «Сканировать»", justify="center")
            return
        img = it.process(self.pages[self.selected].small(), self._mode(), self.clean_var.get())
        w, h = max(c.winfo_width() - 20, 50), max(c.winfo_height() - 20, 50)
        img = img.convert("L" if img.mode == "1" else img.mode)
        img.thumbnail((w, h))
        self._preview_ref = ImageTk.PhotoImage(img)
        c.create_image(c.winfo_width() // 2, c.winfo_height() // 2, image=self._preview_ref)

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
            p.raw = it.autocrop_to_content(p.raw, margin_px=p.dpi // 10)
            p._small = None
            self._refresh_pages()

    def _delete_page(self):
        if 0 <= self.selected < len(self.pages):
            del self.pages[self.selected]
            self.selected = min(self.selected, len(self.pages) - 1)
            self._refresh_pages()

    def _clear_pages(self):
        if self.pages and messagebox.askyesno(APP_TITLE, "Удалить все отсканированные страницы?"):
            self.pages.clear()
            self.selected = -1
            self._refresh_pages()

    # ------------------------------------------------------------ вкладка «Печать»

    def _build_print_tab(self):
        top = ttk.Frame(self.print_tab)
        top.pack(fill="x")
        ttk.Label(top, text="Принтер:").pack(side="left")
        self.printer_var = tk.StringVar()
        self.printer_box = ttk.Combobox(top, textvariable=self.printer_var, state="readonly", width=50)
        self.printer_box.pack(side="left", padx=6)
        ttk.Button(top, text="Обновить", command=self.refresh_devices).pack(side="left")

        body = ttk.Frame(self.print_tab)
        body.pack(fill="both", expand=True, pady=8)
        files = ttk.LabelFrame(body, text="Файлы для печати (PDF, изображения, документы)", padding=8)
        files.pack(side="left", fill="both", expand=True)
        self.files_list = tk.Listbox(files, selectmode="extended", font=("Segoe UI", 10))
        self.files_list.pack(fill="both", expand=True)
        btns = ttk.Frame(files)
        btns.pack(fill="x", pady=(6, 0))
        ttk.Button(btns, text="Добавить файлы…", command=self._pick_print_files).pack(side="left")
        ttk.Button(btns, text="Убрать выбранные", command=self._remove_print_files).pack(side="left", padx=4)
        ttk.Button(btns, text="Очистить", command=lambda: (self.files_list.delete(0, "end"),
                                                          self._update_page_count())).pack(side="left")

        opts = ttk.LabelFrame(body, text="Параметры", padding=8)
        opts.pack(side="left", fill="y", padx=(10, 0))
        ttk.Label(opts, text="Копий:").grid(row=0, column=0, sticky="w")
        self.copies_var = tk.IntVar(value=1)
        ttk.Spinbox(opts, from_=1, to=99, textvariable=self.copies_var, width=6,
                    command=self._update_page_count).grid(row=0, column=1, sticky="e")
        ttk.Label(opts, text="Страницы:").grid(row=1, column=0, sticky="w", pady=6)
        self.range_var = tk.StringVar()
        ttk.Entry(opts, textvariable=self.range_var, width=14).grid(row=1, column=1, sticky="e")
        ttk.Label(opts, text="пусто — все; пример: 1-3, 5, 8-", foreground="gray").grid(
            row=2, column=0, columnspan=2, sticky="w")

        self.fit_var = tk.BooleanVar(value=True)
        ttk.Radiobutton(opts, text="Вписать в лист", value=True, variable=self.fit_var).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Radiobutton(opts, text="Реальный размер (100 %)", value=False, variable=self.fit_var).grid(
            row=4, column=0, columnspan=2, sticky="w")

        ttk.Label(opts, text="Качество:").grid(row=5, column=0, sticky="w", pady=(8, 0))
        self.print_dpi_var = tk.StringVar(value="600 dpi")
        ttk.Combobox(opts, textvariable=self.print_dpi_var, values=["600 dpi", "300 dpi (быстрее)"],
                     state="readonly", width=16).grid(row=5, column=1, sticky="e", pady=(8, 0))

        self.duplex_var = tk.BooleanVar(value=False)
        self.reverse_var = tk.BooleanVar(value=True)
        self.rotate_back_var = tk.BooleanVar(value=self.cfg.get("rotate_back", False))
        ttk.Checkbutton(opts, text="Двусторонняя (вручную)", variable=self.duplex_var,
                        command=self._update_page_count).grid(row=6, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Checkbutton(opts, text="  обратный порядок 2-й стороны", variable=self.reverse_var).grid(
            row=7, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(opts, text="  повернуть 2-ю сторону на 180°", variable=self.rotate_back_var).grid(
            row=8, column=0, columnspan=2, sticky="w")

        self.pages_info = tk.StringVar()
        ttk.Label(opts, textvariable=self.pages_info, wraplength=220).grid(row=9, column=0, columnspan=2,
                                                                           sticky="w", pady=10)
        ttk.Button(opts, text="Печать", style="Big.TButton", command=self.do_print).grid(
            row=10, column=0, columnspan=2, sticky="we", pady=(10, 0))
        self.range_var.trace_add("write", lambda *a: self._update_page_count())

    def _pick_print_files(self):
        files = filedialog.askopenfilenames(
            title="Выберите файлы для печати",
            filetypes=[("Поддерживаемые файлы", "*.pdf *.jpg *.jpeg *.png *.bmp *.gif *.tif *.tiff *.webp "
                                                "*.doc *.docx *.xls *.xlsx *.txt *.rtf *.odt"),
                       ("PDF", "*.pdf"), ("Изображения", "*.jpg *.jpeg *.png *.bmp *.tif *.tiff"),
                       ("Все файлы", "*.*")])
        self.add_print_files(list(files))

    def add_print_files(self, files: list[str]):
        for f in files:
            if os.path.isfile(f):
                self.files_list.insert("end", f)
        self._update_page_count()

    def _remove_print_files(self):
        for i in reversed(self.files_list.curselection()):
            self.files_list.delete(i)
        self._update_page_count()

    def _print_options(self) -> pb.PrintOptions:
        try:
            copies = max(1, int(self.copies_var.get()))
        except (tk.TclError, ValueError):
            copies = 1
        return pb.PrintOptions(copies=copies, page_range=self.range_var.get(), fit=self.fit_var.get(),
                               dpi=600 if self.print_dpi_var.get().startswith("600") else 300,
                               manual_duplex=self.duplex_var.get(), reverse_second_side=self.reverse_var.get(),
                               rotate_second_side=self.rotate_back_var.get())

    def _update_page_count(self):
        files = list(self.files_list.get(0, "end"))
        if not files:
            self.pages_info.set("")
            return
        try:
            plan = pb.build_plan(files, self._print_options())
        except Exception as e:  # noqa: BLE001
            self.pages_info.set(f"⚠ {e}")
            return
        sides = sum(len(p) for p in plan.passes)
        sheets = len(plan.passes[0]) if plan.passes else 0
        text = f"Будет напечатано сторон: {sides}, листов: {sheets}"
        if len(files) > 1 and self.range_var.get().strip():
            text += "\n(диапазон страниц применяется только к одному файлу)"
        if plan.other_files:
            text += f"\nЕщё {len(plan.other_files)} файл(ов) напечатает связанная программа (Word, Блокнот…)"
        self.pages_info.set(text)

    def _ask_flip_threadsafe(self, sheets: int) -> bool:
        return self.call_in_ui(lambda: messagebox.askokcancel(
            APP_TITLE,
            f"Первая сторона напечатана ({sheets} л.).\n\n"
            "1. Дождитесь, пока выйдет последний лист.\n"
            "2. Возьмите всю стопку из выходного лотка, НЕ переворачивая её и не меняя порядок листов.\n"
            "3. Положите её во входной лоток чистой стороной вверх, тем же краем вперёд, "
            "как она вышла из принтера.\n\n"
            "Нажмите OK, чтобы напечатать вторую сторону.\n\n"
            "Совет: если оборот получился вверх ногами — включите «повернуть 2-ю сторону на 180°»."))

    def do_print(self):
        files = list(self.files_list.get(0, "end"))
        printer = self.printer_var.get()
        if not files:
            messagebox.showinfo(APP_TITLE, "Добавьте файлы для печати.")
            return
        if not printer:
            messagebox.showwarning(APP_TITLE, "Не выбран принтер.")
            return
        opts = self._print_options()
        self.cfg["printer"] = printer
        self.cfg["rotate_back"] = opts.rotate_second_side

        def work():
            plan = pb.build_plan(files, opts)
            pb.execute(printer, plan, opts, self.set_status_threadsafe, self._ask_flip_threadsafe)

        self.run_bg("Печать…", work, lambda r: self.status.set("Задание отправлено на принтер"))

    # ------------------------------------------------------------ вкладка «Принтер»

    def _build_printer_tab(self):
        grp = ttk.LabelFrame(self.printer_tab, text="Состояние", padding=10)
        grp.pack(fill="x")
        self.printer_state = tk.StringVar(value="—")
        ttk.Label(grp, textvariable=self.printer_state, font=("Segoe UI", 11)).pack(anchor="w")

        grp = ttk.LabelFrame(self.printer_tab, text="Очередь печати", padding=10)
        grp.pack(fill="both", expand=True, pady=8)
        self.jobs_list = tk.Listbox(grp, height=8)
        self.jobs_list.pack(fill="both", expand=True)

        btns = ttk.Frame(self.printer_tab)
        btns.pack(fill="x")
        ttk.Button(btns, text="Обновить", command=self.refresh_printer_state).pack(side="left")
        ttk.Button(btns, text="Очистить очередь (если печать «зависла»)",
                   command=self._clear_queue).pack(side="left", padx=6)
        ttk.Button(btns, text="Веб-интерфейс принтера", command=self._open_ews).pack(side="left")
        if sys.platform == "win32":
            ttk.Button(btns, text="Настройки драйвера…", command=self._driver_settings).pack(side="left", padx=6)
        ttk.Label(self.printer_tab, foreground="gray", wraplength=900, justify="left", text=(
            "Веб-интерфейс (только при сетевом подключении) позволяет посмотреть остаток тонера, "
            "настроить сеть, режим экономии и плотность печати. В настройках драйвера можно включить "
            "EconoMode (экономия тонера) и выбрать тип бумаги.")).pack(anchor="w", pady=10)

    def refresh_printer_state(self):
        printer = self.printer_var.get()
        if not printer:
            return

        def done(res):
            state, jobs = res
            self.printer_state.set(f"{printer}: {state}")
            self.jobs_list.delete(0, "end")
            for j in jobs or ["(очередь пуста)"]:
                self.jobs_list.insert("end", j)

        self.run_bg("Запрос состояния принтера…", lambda: pb.printer_status(printer), done)

    def _clear_queue(self):
        printer = self.printer_var.get()
        if printer and messagebox.askyesno(APP_TITLE, "Отменить все задания в очереди этого принтера?"):
            self.run_bg("Очистка очереди…", lambda: pb.clear_queue(printer),
                        lambda r: self.refresh_printer_state())

    def _open_ews(self):
        printer = self.printer_var.get()
        ip = pb.printer_ip(printer) if printer else None
        if ip:
            webbrowser.open(f"http://{ip}/")
        else:
            messagebox.showinfo(APP_TITLE, "Не удалось определить IP-адрес принтера (возможно, он подключён "
                                           "по USB). Адрес можно узнать, напечатав страницу конфигурации: "
                                           "меню на панели МФУ → Отчеты → Отчет о конфигурации.")

    def _driver_settings(self):
        import subprocess

        subprocess.Popen(["rundll32", "printui.dll,PrintUIEntry", "/e", "/n", self.printer_var.get()])

    # ------------------------------------------------------------ общее

    def refresh_devices(self):
        def work():
            errors = []
            try:
                printers, default = pb.list_printers()
            except Exception as e:  # noqa: BLE001
                printers, default = [], ""
                errors.append(f"Принтеры: {e}")
            try:
                scanners = sb.list_scanners()
            except Exception as e:  # noqa: BLE001
                scanners = []
                errors.append(f"Сканеры: {e}")
            return printers, default, scanners, errors

        def done(res):
            printers, default, self.scanners, errors = res
            self.printer_box["values"] = printers
            saved = self.cfg.get("printer")
            self.printer_var.set(saved if saved in printers else default if default in printers
                                 else (printers[0] if printers else ""))
            self.scanner_box["values"] = [s.name for s in self.scanners]
            if self.scanners:
                self.scanner_box.current(sb.pick_default(self.scanners))
            else:
                self.scanner_var.set("")
            msg = f"Принтеров: {len(printers)}, сканеров: {len(self.scanners)}"
            if errors:
                msg += "  |  " + "; ".join(errors)
            self.status.set(msg)
            self.refresh_printer_state()

        self.scanners = []
        self.run_bg("Поиск устройств…", work, done)

    def _load_cfg(self) -> dict:
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _on_close(self):
        self.cfg.update(preset=self._preset_key(), dpi=int(self.dpi_var.get()), mode=self._mode(),
                        quality=int(self.quality_var.get()), clean=self.clean_var.get(),
                        source=self.source_var.get(), page_size=self.page_size_var.get())
        if self.printer_var.get():
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


def main():
    if sys.platform == "win32":
        try:  # чёткий интерфейс на мониторах с масштабированием 125–200 %
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            pass
    App(sys.argv[1:]).mainloop()


if __name__ == "__main__":
    main()
