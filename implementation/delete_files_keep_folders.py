#!/usr/bin/env python3
"""Удаляет все файлы внутри выбранных папок (рекурсивно), сохраняя структуру папок.

При запуске без аргументов открывается окно для выбора одной или нескольких папок.

Примеры:
    python delete_files_keep_folders.py                          # окно выбора папок
    python delete_files_keep_folders.py папка1 папка2 --dry-run  # только показать
    python delete_files_keep_folders.py папка1 папка2            # удалить с подтверждением
    python delete_files_keep_folders.py папка1 --yes             # удалить без вопросов
"""

import argparse
import os
import queue
import sys
import threading


def delete_files(root: str, dry_run: bool = False, log=print) -> tuple[int, int]:
    """Удаляет файлы во всех вложенных папках root. Возвращает (удалено, ошибок)."""
    deleted = errors = 0
    # followlinks=False — не заходим в папки по символическим ссылкам,
    # чтобы случайно не удалить файлы за пределами root.
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            path = os.path.join(dirpath, name)
            if dry_run:
                log(f"[dry-run] {path}")
                deleted += 1
                continue
            try:
                os.remove(path)
                log(f"Удалён: {path}")
                deleted += 1
            except OSError as e:
                log(f"Ошибка: {path}: {e}")
                errors += 1
    return deleted, errors


def run_gui() -> int:
    import tkinter as tk
    from tkinter import filedialog, messagebox
    from tkinter.scrolledtext import ScrolledText

    window = tk.Tk()
    window.title("Удаление файлов (папки сохраняются)")
    window.geometry("700x500")

    messages: queue.Queue = queue.Queue()

    tk.Label(window, text="Папки, в которых будут удалены все файлы:").pack(
        anchor="w", padx=10, pady=(10, 0))

    folders = tk.Listbox(window, selectmode=tk.EXTENDED, height=8)
    folders.pack(fill=tk.X, padx=10, pady=5)

    def add_folder():
        path = filedialog.askdirectory(title="Выберите папку", mustexist=True)
        if path:
            path = os.path.abspath(path)
            if path not in folders.get(0, tk.END):
                folders.insert(tk.END, path)

    def remove_selected():
        for index in reversed(folders.curselection()):
            folders.delete(index)

    buttons = tk.Frame(window)
    buttons.pack(fill=tk.X, padx=10)
    tk.Button(buttons, text="Добавить папку…", command=add_folder).pack(side=tk.LEFT)
    tk.Button(buttons, text="Убрать выбранные", command=remove_selected).pack(
        side=tk.LEFT, padx=5)
    dry_run = tk.BooleanVar(value=False)
    tk.Checkbutton(buttons, text="Только показать (не удалять)",
                   variable=dry_run).pack(side=tk.LEFT, padx=10)
    start_button = tk.Button(buttons, text="Удалить файлы", fg="white", bg="#c0392b")
    start_button.pack(side=tk.RIGHT)

    log_box = ScrolledText(window, state=tk.DISABLED)
    log_box.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

    def worker(roots: list[str], is_dry: bool):
        total_deleted = total_errors = 0
        for root in roots:
            if not os.path.isdir(root):
                messages.put(f"Папка не найдена: {root}")
                total_errors += 1
                continue
            deleted, errors = delete_files(root, dry_run=is_dry, log=messages.put)
            total_deleted += deleted
            total_errors += errors
        verb = "Будет удалено" if is_dry else "Удалено"
        messages.put(f"\n{verb} файлов: {total_deleted}. Ошибок: {total_errors}.")
        messages.put(None)  # сигнал окончания работы

    def poll_messages():
        # Окно обновляется только из главного потока — забираем сообщения из очереди.
        try:
            while True:
                msg = messages.get_nowait()
                if msg is None:
                    start_button.config(state=tk.NORMAL)
                    continue
                log_box.config(state=tk.NORMAL)
                log_box.insert(tk.END, msg + "\n")
                log_box.see(tk.END)
                log_box.config(state=tk.DISABLED)
        except queue.Empty:
            pass
        window.after(100, poll_messages)

    def start():
        roots = list(folders.get(0, tk.END))
        if not roots:
            messagebox.showwarning("Нет папок", "Сначала добавьте хотя бы одну папку.")
            return
        is_dry = dry_run.get()
        if not is_dry and not messagebox.askyesno(
                "Подтверждение",
                "Удалить ВСЕ файлы в этих папках и их подпапках?\n\n"
                + "\n".join(roots) + "\n\nЭто действие нельзя отменить.",
                icon=messagebox.WARNING):
            return
        log_box.config(state=tk.NORMAL)
        log_box.delete("1.0", tk.END)
        log_box.config(state=tk.DISABLED)
        start_button.config(state=tk.DISABLED)
        threading.Thread(target=worker, args=(roots, is_dry), daemon=True).start()

    start_button.config(command=start)
    poll_messages()
    window.mainloop()
    return 0


def run_cli(args: argparse.Namespace) -> int:
    roots = [os.path.abspath(f) for f in args.folders]
    missing = [r for r in roots if not os.path.isdir(r)]
    if missing:
        for r in missing:
            print(f"Папка не найдена: {r}", file=sys.stderr)
        return 1

    if not args.dry_run and not args.yes:
        print("Будут удалены ВСЕ файлы в папках и подпапках:")
        for r in roots:
            print(f"  {r}")
        answer = input("Продолжить? [y/N]: ")
        if answer.strip().lower() not in ("y", "yes", "д", "да"):
            print("Отменено.")
            return 0

    total_deleted = total_errors = 0
    for root in roots:
        deleted, errors = delete_files(root, dry_run=args.dry_run)
        total_deleted += deleted
        total_errors += errors
    verb = "Будет удалено" if args.dry_run else "Удалено"
    print(f"\n{verb} файлов: {total_deleted}. Ошибок: {total_errors}.")
    return 1 if total_errors else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Удалить все файлы в папках и подпапках, не трогая сами папки. "
                    "Без аргументов открывается окно выбора папок."
    )
    parser.add_argument("folders", nargs="*", help="Папки, которые нужно очистить")
    parser.add_argument("--dry-run", action="store_true",
                        help="Только показать, что будет удалено")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="Не спрашивать подтверждение")
    args = parser.parse_args()

    if not args.folders:
        return run_gui()
    return run_cli(args)


if __name__ == "__main__":
    sys.exit(main())
