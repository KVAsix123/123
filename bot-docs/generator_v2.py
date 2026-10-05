# -*- coding: utf-8 -*-
"""
Генератор договоров v2.

Что изменилось против первой версии:
  * один экран вместо двух: вставил текст клиента -> [Разобрать] -> проверил форму -> [Сформировать]
    (JSON из чата больше не нужен, но вставленный JSON тоже понимается);
  * поля подсвечиваются: красный — ошибка, жёлтый — предупреждение, зелёный — разобрано;
  * счета и БИК проверяются контрольной суммой ЦБ — опечатка в одной цифре ловится до печати;
  * замена плейсхолдеров не ломает форматирование и работает в колонтитулах и таблицах;
  * перед сохранением документ проверяется на незаполненные {{плейсхолдеры}};
  * номер договора не теряется, файлы не перезаписывают друг друга, ведётся журнал CSV;
  * PDF: docx2pdf, а если его нет — LibreOffice headless;
  * логика вынесена в dogovor_core.py — её же использует телеграм-бот;
  * генерация идёт в отдельном потоке, окно не подвисает.

Рядом должны лежать: template11.docx, dogovor_core.py, parser_klienta.py, docx_zapolnenie.py
"""

import json
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext

from dogovor_core import build_contract
from parser_klienta import FIELD_LABELS, FIELDS, parse_client, validate

COLOR_OK, COLOR_WARN, COLOR_ERR, COLOR_PLAIN = "#e8f5e9", "#fff8e1", "#ffebee", "#ffffff"


# --- UI ---------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Генератор договоров")
        self.geometry("760x780")
        self.entries, self.labels = {}, {}

        tk.Label(self, text="1. Вставьте всё, что прислал клиент",
                 font=("Arial", 12, "bold")).pack(pady=(12, 4))
        self.text = scrolledtext.ScrolledText(self, wrap=tk.WORD, height=9,
                                              font=("Consolas", 10))
        self.text.pack(fill="x", padx=14)
        self._bind_paste(self.text)

        bar = tk.Frame(self)
        bar.pack(pady=8)
        tk.Button(bar, text="Разобрать", font=("Arial", 11, "bold"), width=16,
                  bg="#1976d2", fg="white", command=self.on_parse).pack(side="left", padx=6)
        tk.Button(bar, text="Очистить", font=("Arial", 11), width=12,
                  command=self.on_clear).pack(side="left", padx=6)

        tk.Label(self, text="2. Проверьте поля и исправьте подсвеченные",
                 font=("Arial", 12, "bold")).pack(pady=(4, 4))
        form = tk.Frame(self)
        form.pack(padx=14, fill="x")
        for i, key in enumerate(FIELDS):
            lb = tk.Label(form, text=FIELD_LABELS[key] + ":", font=("Arial", 10), anchor="e", width=24)
            lb.grid(row=i, column=0, sticky="e", padx=4, pady=3)
            en = tk.Entry(form, width=58, font=("Arial", 10))
            en.grid(row=i, column=1, sticky="w", padx=4, pady=3)
            self.entries[key], self.labels[key] = en, lb

        self.status = tk.Label(self, text="", font=("Arial", 10), fg="#555",
                               wraplength=720, justify="left")
        self.status.pack(pady=6, padx=14, fill="x")

        self.btn = tk.Button(self, text="Сформировать договор", font=("Arial", 12, "bold"),
                             bg="#4CAF50", fg="white", command=self.on_generate)
        self.btn.pack(pady=10)

    def _bind_paste(self, widget):
        def paste(event=None):
            try:
                widget.insert(tk.INSERT, self.clipboard_get())
            except tk.TclError:
                pass
            return "break"
        for seq in ("<Control-v>", "<Control-V>", "<Control-м>", "<Control-М>", "<Shift-Insert>"):
            widget.bind(seq, paste)
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Вставить", command=paste)
        menu.add_command(label="Очистить", command=lambda: widget.delete("1.0", tk.END))
        widget.bind("<Button-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))

    def on_clear(self):
        self.text.delete("1.0", tk.END)
        for key in FIELDS:
            self.entries[key].delete(0, tk.END)
            self.entries[key].config(bg=COLOR_PLAIN)
        self.status.config(text="")

    def on_parse(self):
        raw = self.text.get("1.0", tk.END).strip()
        if not raw:
            messagebox.showwarning("Пусто", "Вставьте текст, который прислал клиент.")
            return
        # вставили готовый JSON — тоже принимаем
        data = None
        if raw.lstrip().startswith("{"):
            try:
                data = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
            except Exception:
                data = None
        if data is None:
            data, issues = parse_client(raw)
        else:
                issues = validate(data)

        by_field = {}
        for it in issues:
            by_field.setdefault(it.field, []).append(it)

        for key in FIELDS:
            self.entries[key].delete(0, tk.END)
            self.entries[key].insert(0, data.get(key, ""))
            problems = by_field.get(key, [])
            if any(p.level == "error" for p in problems):
                self.entries[key].config(bg=COLOR_ERR)
            elif problems:
                self.entries[key].config(bg=COLOR_WARN)
            elif data.get(key):
                self.entries[key].config(bg=COLOR_OK)
            else:
                self.entries[key].config(bg=COLOR_PLAIN)

        errs = [str(i) for i in issues if i.level == "error"]
        warns = [str(i) for i in issues if i.level == "warn"]
        msg = f"Разобрано полей: {sum(1 for k in FIELDS if data.get(k))} из {len(FIELDS)}."
        if errs:
            msg += "\nОшибки: " + "; ".join(errs)
        if warns:
            msg += "\nПроверьте: " + "; ".join(warns)
        self.status.config(text=msg, fg="#c62828" if errs else "#555")

    def on_generate(self):
        data = {k: self.entries[k].get().strip() for k in FIELDS}
        if not data["full_name"]:
            messagebox.showerror("Ошибка", "Поле «ФИО полностью» обязательно.")
            return
        if len(data["full_name"].split()) < 3 and not messagebox.askyesno(
                "Проверка ФИО", "В ФИО нет отчества. Всё равно сформировать договор?"):
            return

        errs = [str(i) for i in validate(data) if i.level == "error"]
        if errs and not messagebox.askyesno(
                "Есть ошибки", "\n".join(errs) + "\n\nВсё равно сформировать?"):
            return

        self.btn.config(state=tk.DISABLED, text="Формирую...")
        threading.Thread(target=self._worker, args=(data,), daemon=True).start()

    def _worker(self, data):
        try:
            res = build_contract(data)
            self.after(0, self._done, res, None)
        except Exception as e:
            self.after(0, self._done, None, e)

    def _done(self, res, err):
        self.btn.config(state=tk.NORMAL, text="Сформировать договор")
        if err:
            messagebox.showerror("Ошибка", str(err))
            return
        txt = f"Договор № {res['number']} готов.\n\nWord: {res['docx']}"
        if res["pdf"]:
            txt += f"\nPDF: {res['pdf']}"
        if res["warning"]:
            txt += f"\n\n{res['warning']}"
        messagebox.showinfo("Готово", txt)
        self.on_clear()


if __name__ == "__main__":
    App().mainloop()
