#!/usr/bin/env python3
"""Удаляет все файлы внутри папки (рекурсивно), сохраняя структуру папок.

Примеры:
    python delete_files_keep_folders.py /путь/к/папке --dry-run   # только показать
    python delete_files_keep_folders.py /путь/к/папке             # удалить с подтверждением
    python delete_files_keep_folders.py /путь/к/папке --yes       # удалить без вопросов
"""

import argparse
import os
import sys


def delete_files(root: str, dry_run: bool = False) -> tuple[int, int]:
    """Удаляет файлы во всех вложенных папках root. Возвращает (удалено, ошибок)."""
    deleted = errors = 0
    # followlinks=False — не заходим в папки по символическим ссылкам,
    # чтобы случайно не удалить файлы за пределами root.
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            path = os.path.join(dirpath, name)
            if dry_run:
                print(f"[dry-run] {path}")
                deleted += 1
                continue
            try:
                os.remove(path)
                print(f"Удалён: {path}")
                deleted += 1
            except OSError as e:
                print(f"Ошибка: {path}: {e}", file=sys.stderr)
                errors += 1
    return deleted, errors


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Удалить все файлы в папке и подпапках, не трогая сами папки."
    )
    parser.add_argument("folder", help="Папка, которую нужно очистить")
    parser.add_argument("--dry-run", action="store_true",
                        help="Только показать, что будет удалено")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="Не спрашивать подтверждение")
    args = parser.parse_args()

    root = os.path.abspath(args.folder)
    if not os.path.isdir(root):
        print(f"Папка не найдена: {root}", file=sys.stderr)
        return 1

    if not args.dry_run and not args.yes:
        answer = input(f"Удалить ВСЕ файлы в '{root}' и подпапках? [y/N]: ")
        if answer.strip().lower() not in ("y", "yes", "д", "да"):
            print("Отменено.")
            return 0

    deleted, errors = delete_files(root, dry_run=args.dry_run)
    verb = "Будет удалено" if args.dry_run else "Удалено"
    print(f"\n{verb} файлов: {deleted}. Ошибок: {errors}.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
