"""Оформление: палитра, шрифты, карточки и модальные диалоги в едином стиле.

Цвета задаются парами (светлая тема, тёмная тема) — CustomTkinter сам
переключает их при смене темы.
"""
from __future__ import annotations

import sys

import customtkinter as ctk

ACCENT = "#0096D6"  # фирменный синий HP
ACCENT_HOVER = "#007CB4"
ACCENT_SOFT = ("#E3F4FB", "#11313F")
BG = ("#F3F5F8", "#111215")
SIDEBAR = ("#FFFFFF", "#17181C")
CARD = ("#FFFFFF", "#1C1D22")
CARD_BORDER = ("#E4E7EC", "#2A2C33")
FIELD = ("#F3F5F8", "#26282E")
TEXT = ("#101828", "#F2F4F7")
MUTED = ("#667085", "#98A2B3")
DANGER = "#E5484D"
SUCCESS = "#30A46C"
WARNING = "#F5A524"
VIEWER_BG = "#16171A"  # фон области предпросмотра — тёмный в обеих темах, как в фотоальбомах

FAMILY = "Segoe UI" if sys.platform == "win32" else None


def font(size: int = 13, weight: str = "normal") -> ctk.CTkFont:
    return ctk.CTkFont(family=FAMILY, size=size, weight=weight)


class Card(ctk.CTkFrame):
    """Белая (в тёмной теме — графитовая) карточка со скруглёнными углами и заголовком."""

    def __init__(self, master, title: str | None = None, **kw):
        super().__init__(master, fg_color=CARD, corner_radius=14, border_width=1,
                         border_color=CARD_BORDER, **kw)
        self.body = self
        if title:
            ctk.CTkLabel(self, text=title.upper(), font=font(11, "bold"), text_color=MUTED,
                         anchor="w").pack(fill="x", padx=16, pady=(12, 4))


def primary_button(master, text: str, command, height: int = 40, **kw) -> ctk.CTkButton:
    return ctk.CTkButton(master, text=text, command=command, height=height, corner_radius=10,
                         fg_color=ACCENT, hover_color=ACCENT_HOVER, font=font(14, "bold"), **kw)


def secondary_button(master, text: str, command, height: int = 36, **kw) -> ctk.CTkButton:
    return ctk.CTkButton(master, text=text, command=command, height=height, corner_radius=10,
                         fg_color=CARD, hover_color=FIELD, text_color=TEXT, border_width=1,
                         border_color=CARD_BORDER, font=font(13), **kw)


def tool_button(master, text: str, command, width: int = 36, **kw) -> ctk.CTkButton:
    """Компактная кнопка панели инструментов."""
    return ctk.CTkButton(master, text=text, command=command, width=width, height=32, corner_radius=8,
                         fg_color="transparent", hover_color=FIELD, text_color=TEXT, font=font(13), **kw)


def segmented(master, values: list[str], variable, command=None) -> ctk.CTkSegmentedButton:
    return ctk.CTkSegmentedButton(master, values=values, variable=variable, command=command, height=32,
                                  corner_radius=8, font=font(12), selected_color=ACCENT,
                                  selected_hover_color=ACCENT_HOVER, unselected_color=FIELD,
                                  unselected_hover_color=CARD_BORDER, fg_color=FIELD, text_color=TEXT)


def switch(master, text: str, variable, command=None) -> ctk.CTkSwitch:
    return ctk.CTkSwitch(master, text=text, variable=variable, command=command, font=font(13),
                         progress_color=ACCENT, text_color=TEXT)


def option_menu(master, values: list[str], variable, command=None, **kw) -> ctk.CTkOptionMenu:
    return ctk.CTkOptionMenu(master, values=values or [""], variable=variable, command=command, height=34,
                             corner_radius=8, fg_color=FIELD, button_color=FIELD,
                             button_hover_color=CARD_BORDER, text_color=TEXT, font=font(13),
                             dropdown_font=font(13), dynamic_resizing=False, **kw)


_ICONS = {"info": ("i", ACCENT), "success": ("✓", SUCCESS), "warning": ("!", WARNING),
          "error": ("✕", DANGER), "question": ("?", ACCENT)}


class Dialog(ctk.CTkToplevel):
    """Модальное окно с иконкой, текстом и набором кнопок. Первая кнопка — основная."""

    def __init__(self, master, title: str, message: str, buttons: list[str], kind: str = "info"):
        super().__init__(master)
        self.result: int | None = None
        self.title(title)
        self.resizable(False, False)
        self.configure(fg_color=CARD)
        self.transient(master.winfo_toplevel())

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=24, pady=(22, 10))
        glyph, color = _ICONS.get(kind, _ICONS["info"])
        ctk.CTkLabel(body, text=glyph, width=44, height=44, corner_radius=22, fg_color=color,
                     text_color="white", font=font(20, "bold")).pack(side="left", anchor="n")
        text = ctk.CTkFrame(body, fg_color="transparent")
        text.pack(side="left", fill="both", expand=True, padx=(16, 0))
        ctk.CTkLabel(text, text=title, font=font(16, "bold"), text_color=TEXT, anchor="w").pack(fill="x")
        ctk.CTkLabel(text, text=message, font=font(13), text_color=MUTED, justify="left", anchor="w",
                     wraplength=420).pack(fill="x", pady=(6, 0))

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=24, pady=(10, 20))
        for i, label in reversed(list(enumerate(buttons))):
            maker = primary_button if i == 0 else secondary_button
            btn = maker(row, label, lambda i=i: self._close(i), height=36)
            btn.pack(side="right", padx=(8, 0))
        self.bind("<Return>", lambda e: self._close(0))
        self.bind("<Escape>", lambda e: self._close(None))
        self.protocol("WM_DELETE_WINDOW", lambda: self._close(None))

        self.update_idletasks()
        top = master.winfo_toplevel()
        x = top.winfo_rootx() + (top.winfo_width() - self.winfo_width()) // 2
        y = top.winfo_rooty() + (top.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        self.after(50, self._grab)

    def _grab(self):
        try:
            self.grab_set()
            self.focus_force()
        except Exception:  # noqa: BLE001 — окно могло уже закрыться
            pass

    def _close(self, result):
        self.result = result
        self.grab_release()
        self.destroy()

    @classmethod
    def ask(cls, master, title: str, message: str, buttons: list[str], kind: str = "info") -> int | None:
        dlg = cls(master, title, message, buttons, kind)
        master.wait_window(dlg)
        return dlg.result


def info(master, message: str, title: str = "Готово", kind: str = "info") -> None:
    Dialog.ask(master, title, message, ["OK"], kind)


def error(master, message: str, title: str = "Что-то пошло не так") -> None:
    Dialog.ask(master, title, message, ["Понятно"], "error")


def ask_yes_no(master, message: str, title: str, yes: str = "Да", no: str = "Отмена",
               kind: str = "question") -> bool:
    return Dialog.ask(master, title, message, [yes, no], kind) == 0
