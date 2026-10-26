"""Полноэкранный предпросмотр в реальном качестве.

Показывает страницу ровно такой, какой она попадёт в файл: после обработки
и сжатия (JPEG-артефакты видны при 100 %). Чтобы окно оставалось быстрым
даже на 600 dpi (35 Мп), на экран выводится только видимый фрагмент:
он вырезается из полного изображения и масштабируется под окно.
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from typing import Callable

import customtkinter as ctk
from PIL import Image, ImageTk

import widgets as w

MIN_ZOOM, MAX_ZOOM = 0.03, 8.0
HINTS = "Колёсико — масштаб · перетаскивание — сдвиг · 0 — вписать · 1 — 100 % · ← → — страницы · Esc — выход"


class FullscreenViewer(tk.Toplevel):
    def __init__(self, master, count: int, render: Callable[[int], tuple[Image.Image, str]], start: int = 0):
        """render(i) -> (страница как в файле, строка с описанием) — вызывается в фоновом потоке."""
        super().__init__(master, background=w.VIEWER_BG)
        self.count, self.render, self.index = count, render, start
        self.image: Image.Image | None = None
        self.info = ""
        self.scale = 1.0
        self.cx = self.cy = 0.0  # точка изображения в центре экрана
        self._fit_mode = True
        self._cache: dict[int, tuple[Image.Image, str]] = {}
        self._results: queue.Queue = queue.Queue()
        self._photo = None
        self._drag = None
        self._hq_after = None

        # Сначала растягиваем окно на экран (на том мониторе, где программа), затем включаем
        # полноэкранный режим — так окно занимает весь экран даже там, где -fullscreen не сработал.
        top = master.winfo_toplevel()
        self.geometry(f"{self.winfo_screenwidth()}x{self.winfo_screenheight()}+{top.winfo_rootx()}+{top.winfo_rooty()}")
        self.attributes("-fullscreen", True)
        self.canvas = tk.Canvas(self, background=w.VIEWER_BG, highlightthickness=0, cursor="fleur")
        self.canvas.pack(fill="both", expand=True)
        self._build_bars()
        self._bind_keys()
        self.focus_force()
        self.after(60, lambda: self.show(start))
        self.after(80, self._poll)

    # ------------------------------------------------------------ интерфейс

    def _build_bars(self):
        bar = ctk.CTkFrame(self, fg_color="#24262B", corner_radius=14, bg_color=w.VIEWER_BG)
        bar.place(relx=0.5, y=16, anchor="n")

        def btn(text, cmd, width=40):
            b = ctk.CTkButton(bar, text=text, command=cmd, width=width, height=34, corner_radius=9,
                              fg_color="transparent", hover_color="#363940", text_color="#F2F4F7",
                              font=w.font(14))
            b.pack(side="left", padx=2, pady=6)
            return b

        btn("◀", lambda: self.show(self.index - 1))
        self.page_label = ctk.CTkLabel(bar, text="", text_color="#F2F4F7", font=w.font(13, "bold"), width=110)
        self.page_label.pack(side="left")
        btn("▶", lambda: self.show(self.index + 1))
        ctk.CTkFrame(bar, width=1, height=22, fg_color="#41444C").pack(side="left", padx=8)
        btn("−", lambda: self.zoom_by(1 / 1.25))
        self.zoom_label = ctk.CTkLabel(bar, text="", text_color="#F2F4F7", font=w.font(13), width=60)
        self.zoom_label.pack(side="left")
        btn("+", lambda: self.zoom_by(1.25))
        btn("Вписать", self.fit, width=80)
        btn("100 %", lambda: self.set_zoom(1.0), width=64)
        ctk.CTkFrame(bar, width=1, height=22, fg_color="#41444C").pack(side="left", padx=8)
        ctk.CTkButton(bar, text="✕  Закрыть", command=self.destroy, width=100, height=34, corner_radius=9,
                      fg_color=w.ACCENT, hover_color=w.ACCENT_HOVER, font=w.font(13, "bold")).pack(
            side="left", padx=(2, 8), pady=6)

        self.info_label = ctk.CTkLabel(self, text="", fg_color="#24262B", corner_radius=10, text_color="#D0D5DD",
                                       font=w.font(12), bg_color=w.VIEWER_BG, padx=14, pady=8, justify="left")
        self.info_label.place(x=16, rely=1.0, y=-16, anchor="sw")

    def _bind_keys(self):
        self.bind("<Escape>", lambda e: self.destroy())
        self.bind("<Left>", lambda e: self.show(self.index - 1))
        self.bind("<Right>", lambda e: self.show(self.index + 1))
        self.bind("<Prior>", lambda e: self.show(self.index - 1))
        self.bind("<Next>", lambda e: self.show(self.index + 1))
        for key in ("0", "<KP_0>"):
            self.bind(key, lambda e: self.fit())
        for key in ("1", "<KP_1>"):
            self.bind(key, lambda e: self.set_zoom(1.0))
        for key in ("+", "=", "<KP_Add>"):
            self.bind(key, lambda e: self.zoom_by(1.25))
        for key in ("-", "<KP_Subtract>"):
            self.bind(key, lambda e: self.zoom_by(1 / 1.25))
        c = self.canvas
        c.bind("<Configure>", lambda e: self.fit() if self._fit_mode else self.redraw())
        c.bind("<MouseWheel>", lambda e: self.zoom_by(1.2 if e.delta > 0 else 1 / 1.2, e.x, e.y))
        c.bind("<Button-4>", lambda e: self.zoom_by(1.2, e.x, e.y))  # Linux
        c.bind("<Button-5>", lambda e: self.zoom_by(1 / 1.2, e.x, e.y))
        c.bind("<ButtonPress-1>", self._drag_start)
        c.bind("<B1-Motion>", self._drag_move)
        c.bind("<Double-Button-1>", self._toggle_zoom)

    # ------------------------------------------------------------ загрузка страниц

    def show(self, i: int):
        if not 0 <= i < self.count:
            return
        self.index = i
        self.page_label.configure(text=f"{i + 1} из {self.count}")
        if i in self._cache:
            self._set_image(*self._cache[i])
            return
        self.image = None
        self.canvas.delete("all")
        self.canvas.create_text(self.canvas.winfo_width() // 2, self.canvas.winfo_height() // 2,
                                text="Готовлю страницу так, как она будет сохранена…",
                                fill="#98A2B3", font=w.font(15))

        def work():
            try:
                self._results.put((i, self.render(i), None))
            except Exception as e:  # noqa: BLE001
                self._results.put((i, None, e))

        threading.Thread(target=work, daemon=True).start()

    def _poll(self):
        try:
            while True:
                i, res, err = self._results.get_nowait()
                if err:
                    self.canvas.delete("all")
                    self.canvas.create_text(self.canvas.winfo_width() // 2, self.canvas.winfo_height() // 2,
                                            text=f"Ошибка: {err}", fill=w.DANGER, font=w.font(15))
                    continue
                img, info = res
                if img.mode not in ("L", "RGB"):
                    img = img.convert("L" if img.mode in ("1", "LA", "P") else "RGB")
                # Держим в памяти не больше 3 страниц: на 600 dpi каждая занимает ~100 МБ.
                self._cache[i] = (img, info)
                while len(self._cache) > 3:
                    self._cache.pop(next(k for k in self._cache if k != self.index))
                if i == self.index:
                    self._set_image(img, info)
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(80, self._poll)

    def _set_image(self, img: Image.Image, info: str):
        self.image, self.info = img, info
        self.fit()

    # ------------------------------------------------------------ масштаб и сдвиг

    def _fit_scale(self) -> float:
        cw, ch = max(self.canvas.winfo_width(), 1), max(self.canvas.winfo_height(), 1)
        return min((cw - 40) / self.image.width, (ch - 120) / self.image.height)

    def fit(self):
        if self.image is None:
            return
        self._fit_mode = True
        self.scale = self._fit_scale()
        self.cx, self.cy = self.image.width / 2, self.image.height / 2
        self.redraw()

    def set_zoom(self, scale: float, mx: float | None = None, my: float | None = None):
        if self.image is None:
            return
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        mx = cw / 2 if mx is None else mx
        my = ch / 2 if my is None else my
        # Точка под курсором остаётся на месте — как в любом просмотрщике фото.
        px = self.cx + (mx - cw / 2) / self.scale
        py = self.cy + (my - ch / 2) / self.scale
        self.scale = max(MIN_ZOOM, min(MAX_ZOOM, scale))
        self.cx = px - (mx - cw / 2) / self.scale
        self.cy = py - (my - ch / 2) / self.scale
        self._fit_mode = False
        self.redraw()

    def zoom_by(self, k: float, mx=None, my=None):
        self.set_zoom(self.scale * k, mx, my)

    def _toggle_zoom(self, e):
        if abs(self.scale - 1.0) < 1e-3:
            self.fit()
        else:
            self.set_zoom(1.0, e.x, e.y)

    def _drag_start(self, e):
        self._drag = (e.x, e.y, self.cx, self.cy)

    def _drag_move(self, e):
        if self._drag and self.image is not None:
            x0, y0, cx, cy = self._drag
            self.cx = cx - (e.x - x0) / self.scale
            self.cy = cy - (e.y - y0) / self.scale
            self._fit_mode = False
            self.redraw(fast=True)

    def redraw(self, fast: bool = False):
        """Рисует только видимую часть страницы. fast — быстрый фильтр во время перетаскивания."""
        img = self.image
        if img is None:
            return
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        s = self.scale
        # Не даём «утащить» страницу за пределы экрана.
        half_w, half_h = cw / 2 / s, ch / 2 / s
        self.cx = img.width / 2 if img.width <= 2 * half_w else min(max(self.cx, half_w), img.width - half_w)
        self.cy = img.height / 2 if img.height <= 2 * half_h else min(max(self.cy, half_h), img.height - half_h)
        x0, y0 = self.cx - half_w, self.cy - half_h
        bx0, by0 = max(0.0, x0), max(0.0, y0)
        bx1, by1 = min(img.width, x0 + cw / s), min(img.height, y0 + ch / s)
        tw, th = max(1, round((bx1 - bx0) * s)), max(1, round((by1 - by0) * s))
        if fast:
            resample = Image.BILINEAR
        elif s >= 2:
            resample = Image.NEAREST  # при сильном увеличении видны настоящие пиксели скана
        else:
            resample = Image.LANCZOS
        part = img.resize((tw, th), resample, box=(bx0, by0, bx1, by1),
                          reducing_gap=2.0 if s < 0.5 else None)
        self._photo = ImageTk.PhotoImage(part)
        self.canvas.delete("all")
        self.canvas.create_image(round((bx0 - x0) * s), round((by0 - y0) * s), anchor="nw", image=self._photo)
        self.zoom_label.configure(text=f"{s * 100:.0f} %")
        self.info_label.configure(text=f"{self.info}\n{HINTS}")
        if fast:
            if self._hq_after:
                self.after_cancel(self._hq_after)
            self._hq_after = self.after(150, self.redraw)
