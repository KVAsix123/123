# -*- coding: utf-8 -*-
"""
Телеграм-бот: присылаете данные клиента в любом виде — текстом или фото
документов — получаете готовый JSON для скрипта, а по кнопке и сам договор.

Как разбирается вход:
  текст  -> регулярки (офлайн, бесплатно) -> если чего-то не хватает и ИИ
            настроен, недостающее добирается моделью;
  фото   -> модель со зрением (DeepSeek Vision, Gemini и т.п.) -> результат
            прогоняется через те же контрольные суммы ЦБ, что и текст.

Контрольные суммы здесь ключевые: модель может перепутать 5 и 6 в счёте, но
ключ счёта тогда не сойдётся, и бот скажет об этом, а не выпустит договор.

НАСТРОЙКА
  1. @BotFather -> /newbot -> токен.
  2. bot_config.json:  {"token": "123456:AA...", "allowed_ids": []}
  3. pip install requests
  4. python bot_telegram.py , затем /id боту, номер -> в allowed_ids, перезапуск.
  5. Для фото: заполните llm_config.json (см. llm_provider.py). Текст работает
     и без этого.

КОМАНДЫ
  (текст или фото)   — разобрать данные клиента
  /show   — показать текущий JSON
  /edit   — выбрать поле из списка и вписать новое значение
  /fix поле=значение — поправить поле
  /reset  — сбросить
  /status — что настроено
  /id     — мой Telegram ID
"""

import html
import json
import os
import sys
import time
import traceback

try:
    import requests
except ImportError:
    sys.exit("Нужна библиотека requests:  pip install requests")

from parser_klienta import (FIELD_LABELS, FIELDS, apply_mrz, normalize,
                            parse_client, parse_images, validate)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "bot_config.json")

PHOTO_WAIT = 2.5          # сек: столько ждём остальные фото альбома
MAX_PHOTOS = 6

# Модуль сборки договора грузим лениво и запоминаем причину неудачи. Раньше
# ошибка импорта (чаще всего — не установлен python-docx в том Python, которым
# запускается бот) глоталась молча, и кнопка «Сформировать договор» просто
# пропадала без объяснений.
PIP_NAMES = {"docx": "python-docx", "docx2pdf": "docx2pdf", "lxml": "lxml"}
TEMPLATE = os.path.join(SCRIPT_DIR, "template11.docx")
build_contract = None
BUILD_ERR = ""


def ensure_builder():
    """Пробует подключить dogovor_core. Возвращает '' или текст причины."""
    global build_contract, TEMPLATE, BUILD_ERR
    if build_contract is None:
        try:
            from dogovor_core import TEMPLATE as _tpl, build_contract as _build
            build_contract, TEMPLATE = _build, _tpl
        except ImportError as e:
            mod = (getattr(e, "name", "") or "").split(".")[0]
            pkg = PIP_NAMES.get(mod, mod or "?")
            BUILD_ERR = (f"не установлена библиотека {mod or e} — выполните "
                         f"«{sys.executable} -m pip install {pkg}» и перезапустите бота")
            return BUILD_ERR
        except Exception as e:
            BUILD_ERR = f"не загрузился dogovor_core.py: {e}"
            return BUILD_ERR
    BUILD_ERR = "" if os.path.exists(TEMPLATE) else f"не найден шаблон {TEMPLATE}"
    return BUILD_ERR


ensure_builder()


def load_config():
    cfg = {"token": "", "allowed_ids": []}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as e:
            sys.exit(f"Не читается bot_config.json: {e}")
    cfg["token"] = os.environ.get("TELEGRAM_BOT_TOKEN") or cfg.get("token") or ""
    env_ids = os.environ.get("TELEGRAM_ALLOWED_IDS", "")
    if env_ids:
        cfg["allowed_ids"] = [int(x) for x in env_ids.split(",") if x.strip().lstrip("-").isdigit()]
    cfg["allowed_ids"] = [int(x) for x in cfg.get("allowed_ids", [])]
    return cfg


CFG = load_config()
API = f"https://api.telegram.org/bot{CFG['token']}"
SESSIONS = {}      # chat_id -> разобранный словарь
PHOTOBUF = {}      # chat_id -> {'images': [(bytes, mime)], 'caption': str, 'ts': float}
SEEN_TEXT = {}     # chat_id -> последний распознанный с фото текст (для /raw)
SOURCES = {}       # chat_id -> {поле: приоритет источника}
EDITING = {}       # chat_id -> поле, для которого ждём новое значение

# Чем надёжнее источник, тем выше приоритет. Данные из паспорта (особенно
# прописку со штампа) не перетирает текст, присланный позже; ручную правку
# не перетирает ничего.
PRIO_TEXT, PRIO_PASSPORT, PRIO_MANUAL = 1, 2, 3
PASSPORT_FIELDS = ("full_name", "passport_series", "passport_number",
                   "passport_issued_by", "registration_address")


# --- транспорт --------------------------------------------------------------

def api(method, **params):
    r = requests.post(f"{API}/{method}", json=params, timeout=60)
    return r.json()


def send(chat_id, text, keyboard=None):
    params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True}
    if keyboard:
        params["reply_markup"] = {"inline_keyboard": keyboard}
    return api("sendMessage", **params)


def edit_keyboard(chat_id, message_id, keyboard):
    """Меняет кнопки под уже отправленным сообщением, не плодя новых."""
    return api("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
               reply_markup={"inline_keyboard": keyboard})


def send_file(chat_id, path, caption=""):
    with open(path, "rb") as f:
        r = requests.post(f"{API}/sendDocument",
                          data={"chat_id": chat_id, "caption": caption[:1000]},
                          files={"document": (os.path.basename(path), f)}, timeout=180)
    return r.json()


def download(file_id):
    info = api("getFile", file_id=file_id)
    if not info.get("ok"):
        raise RuntimeError(f"Telegram не отдал файл: {info}")
    path = info["result"]["file_path"]
    r = requests.get(f"https://api.telegram.org/file/bot{CFG['token']}/{path}", timeout=120)
    r.raise_for_status()
    return r.content


# --- ответы -----------------------------------------------------------------

def render_reply(data, issues, source="", chat_id=None):
    payload = {k: data.get(k, "") for k in FIELDS}
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    filled = sum(1 for k in FIELDS if data.get(k))

    errs = [i for i in issues if i.level == "error"]
    warns = [i for i in issues if i.level == "warn"]

    head = f"Разобрано <b>{filled} из {len(FIELDS)}</b> полей"
    head += f" ({source})." if source else "."
    if errs:
        head += "\n\n❗️ <b>Ошибки:</b>\n" + "\n".join(
            f"• {html.escape(FIELD_LABELS.get(i.field, i.field))} — {html.escape(i.message)}"
            for i in errs)
    if warns:
        head += "\n\n⚠️ <b>Проверьте:</b>\n" + "\n".join(
            f"• {html.escape(FIELD_LABELS.get(i.field, i.field))} — {html.escape(i.message)}"
            for i in warns)
    if not errs and not warns:
        head += "\nЗамечаний нет."

    src = SOURCES.get(chat_id, {}) if chat_id is not None else {}
    if any(src.get(f) == PRIO_PASSPORT for f in PASSPORT_FIELDS):
        head += "\n\n🛂 Паспортные данные и прописка взяты из паспорта — текст их не перезапишет."

    return (f"{head}\n\n<pre>{html.escape(body)}</pre>\n\n"
            "<b>Всё верно?</b> Тогда «📄 Сформировать договор».\n"
            "Нужно что-то поправить — «✏️ Редактировать» и выберите поле.")


def keyboard_for(data):
    rows = []
    if data and any(data.get(f) for f in FIELDS):
        rows.append([{"text": "📄 Сформировать договор", "callback_data": "build"}])
        rows.append([{"text": "✏️ Редактировать", "callback_data": "edit"}])
    rows.append([{"text": "🗑 Сбросить", "callback_data": "reset"}])
    return rows


def _short(val, limit=30):
    val = str(val or "").strip()
    return val if len(val) <= limit else val[:limit - 1] + "…"


def fields_keyboard(data):
    """Список полей для правки: на кнопке — название и текущее значение."""
    rows = []
    for f in FIELDS:
        mark = "" if data.get(f) else "⚠️ "
        text = f"{mark}{FIELD_LABELS[f]}: {_short(data.get(f)) or '—'}"
        rows.append([{"text": text, "callback_data": "edit:" + f}])
    rows.append([{"text": "⬅️ Назад", "callback_data": "back"}])
    return rows


def merge_into_session(chat_id, new, from_passport=False):
    """
    Добавляет свежие данные в карточку клиента с учётом приоритета источника:
    поле с более надёжным источником не перезаписывается менее надёжным.
    """
    data = SESSIONS.setdefault(chat_id, {})
    src = SOURCES.setdefault(chat_id, {})
    for k, v in new.items():
        if not v:
            continue
        if k not in FIELDS:            # служебные пометки вроде _mrz_checked
            data[k] = v
            continue
        prio = PRIO_PASSPORT if (from_passport and k in PASSPORT_FIELDS) else PRIO_TEXT
        if data.get(k) and src.get(k, 0) > prio:
            continue
        data[k] = v
        src[k] = prio
    if data.get("registration_address") and src.get("registration_address", 0) >= PRIO_PASSPORT:
        data.pop("_addr_garbled", None)
    return data


def looks_like_passport(fresh):
    """Пачка данных пришла со страниц паспорта, а не, скажем, со скрина банка."""
    return bool(fresh.get("passport_number") or fresh.get("passport_series")
                or fresh.get("_mrz_checked"))


def clear_session(chat_id):
    for store in (SESSIONS, SOURCES, EDITING, PHOTOBUF, SEEN_TEXT):
        store.pop(chat_id, None)


def apply_edit(chat_id, field, value):
    """Ручная правка поля: высший приоритет, дальше её ничто не перезапишет."""
    data = SESSIONS.get(chat_id)
    if not data:
        send(chat_id, "Карточка клиента пуста — сначала пришлите данные.")
        return
    data[field] = value.strip()
    data = normalize(data)
    if field == "registration_address":
        data.pop("_addr_garbled", None)
    if field == "bank_account":
        data.pop("_card_seen", None)
    SESSIONS[chat_id] = data
    SOURCES.setdefault(chat_id, {})[field] = PRIO_MANUAL
    what = "очищено" if not data.get(field) else "обновлено"
    send(chat_id, f"✅ {html.escape(FIELD_LABELS[field])} — {what}.\n\n"
         + render_reply(data, validate(data), chat_id=chat_id), keyboard_for(data))


HELP = (
    "Пришлите данные клиента — <b>текстом, файлом или фотографиями</b>. "
    "Понимаю txt, pdf, docx и картинки. "
    "Можно вперемешку: сначала фото паспорта, потом текстом реквизиты счёта, "
    "данные накопятся в одной карточке.\n\n"
    "Я верну JSON для скрипта и подсвечу, чего не хватает. Счета и БИК "
    "проверяются контрольной суммой ЦБ — опечатка или ошибка распознавания "
    "в одной цифре отловится.\n\n"
    "<b>Фото лучше отправлять «как файл»</b>, без сжатия — мелкий шрифт "
    "в паспорте читается заметно надёжнее.\n\n"
    "<b>Команды</b>\n"
    "/show — текущий JSON\n"
    "/raw — что именно увидел OCR на фото\n"
    "/edit — выбрать поле из списка и поправить\n"
    "/fix поле=значение — поправить поле одной строкой\n"
    "/reset — сбросить\n"
    "/status — что настроено\n"
    "/id — мой Telegram ID"
)


# --- обработка фото ---------------------------------------------------------

def photo_engine():
    """
    Чем читать фото. Приоритет у локального OCR: данные не покидают компьютер.
    Возвращает ('local'|'cloud'|None, описание).
    """
    mode = "auto"
    try:
        import llm_provider
        mode = (llm_provider.load_config().get("photo_mode") or "auto").lower()
    except Exception:
        pass

    local_ok = cloud_ok = False
    local_name = cloud_name = ""
    try:
        import ocr_local
        local_ok = ocr_local.vision_ready()
        local_name = ocr_local.engine_name()
    except Exception:
        pass
    try:
        import llm_provider
        cloud_ok = llm_provider.vision_available()
        cloud_name = llm_provider.load_config().get("vision_model", "")
    except Exception:
        pass

    if mode == "local":
        return ("local", local_name) if local_ok else (None, "локальный OCR не установлен")
    if mode == "cloud":
        return ("cloud", cloud_name) if cloud_ok else (None, "облачное зрение не настроено")
    if local_ok:
        return "local", local_name
    if cloud_ok:
        return "cloud", cloud_name
    return None, "ни локальный OCR, ни облачное зрение не настроены"



def queue_photo(chat_id, blob, mime, caption):
    buf = PHOTOBUF.setdefault(chat_id, {"images": [], "caption": "", "ts": 0.0})
    if len(buf["images"]) < MAX_PHOTOS:
        buf["images"].append((blob, mime))
    if caption:
        buf["caption"] = (buf["caption"] + "\n" + caption).strip()
    buf["ts"] = time.time()


def _read_local(images, caption):
    """Локальный OCR + машиночитаемая зона. Возвращает (поля, пометка, сырой текст)."""
    import ocr_local
    res = ocr_local.read_images(images)
    text, mrz_fields = res.get("text", ""), res.get("mrz") or {}
    if not text.strip() and not mrz_fields:
        return None, "", ""
    combined = (text + "\n" + caption).strip()
    fresh, _ = parse_client(combined, use_llm=False)
    note = ""
    if mrz_fields:
        fresh = apply_mrz(fresh, mrz_fields, page_text=combined)
        note = "машиночитаемая зона прочитана"
    return fresh, note, text


def _read_cloud(images, caption):
    """Модель со зрением. Серию и номер потом сверяем с МЧЗ контрольными цифрами."""
    fresh, _ = parse_images(images, caption)
    note = ("серия и номер сверены с машиночитаемой зоной"
            if fresh.get("_mrz_checked") else "")
    return fresh, note


def flush_photos():
    """Вызывается из главного цикла: разбирает фото, когда альбом догрузился."""
    now = time.time()
    for chat_id in list(PHOTOBUF):
        buf = PHOTOBUF[chat_id]
        if not buf["images"] or now - buf["ts"] < PHOTO_WAIT:
            continue
        PHOTOBUF.pop(chat_id, None)
        n = len(buf["images"])
        kind, name = photo_engine()
        if kind is None:
            send(chat_id, "Фото прочитать нечем: " + html.escape(name)
                 + "\n\nПришлите данные текстом или см. <code>/status</code>.")
            continue

        send(chat_id, f"Читаю {n} фото ({html.escape(name)})…")
        fresh, note, raw_text, used = None, "", "", kind
        try:
            if kind == "cloud":
                fresh, note = _read_cloud(buf["images"], buf["caption"])
            else:
                fresh, note, raw_text = _read_local(buf["images"], buf["caption"])
        except Exception as e:
            problem = str(e)[:250]
            # облако не ответило — молча доигрываем локально, если есть чем
            fallback = False
            if kind == "cloud":
                try:
                    import ocr_local
                    fallback = ocr_local.vision_ready()
                except Exception:
                    fallback = False
            if fallback:
                send(chat_id, "Облако не ответило (" + html.escape(problem)
                     + "). Читаю локально…")
                try:
                    fresh, note, raw_text = _read_local(buf["images"], buf["caption"])
                    used = "local"
                except Exception as e2:
                    send(chat_id, "И локально не вышло: " + html.escape(str(e2)[:200]))
                    continue
            else:
                send(chat_id, "Не смог прочитать фото: " + html.escape(problem)
                     + "\n\nПроверьте <code>/status</code> или пришлите данные текстом.")
                continue

        if fresh is None:
            send(chat_id, "На фото не удалось разобрать текст. Снимите страницы "
                          "по отдельности, сверху и при ровном свете — или пришлите "
                          "данные текстом.")
            continue

        if raw_text:
            SEEN_TEXT[chat_id] = raw_text
        data = merge_into_session(chat_id, fresh, from_passport=looks_like_passport(fresh))
        tail = ""
        if note:
            tail += "\n\n✅ " + html.escape(note)
        if used == "local":
            tail += "\n\nСырой текст распознавания: <code>/raw</code>"
        source = f"по {n} фото" + (", облако" if used == "cloud" else "")
        send(chat_id, render_reply(data, validate(data), source, chat_id) + tail, keyboard_for(data))


# --- команды ----------------------------------------------------------------

def handle_text(chat_id, user_id, text):
    text = (text or "").strip()

    if text in ("/start", "/help"):
        send(chat_id, HELP)
        return
    if text == "/id":
        send(chat_id, f"Ваш Telegram ID: <code>{user_id}</code>")
        return
    if text == "/status":
        import llm_provider
        _, state = llm_provider.status()
        kind, name = photo_engine()
        if kind == "local":
            photo_line = f"локально, {html.escape(name)} — данные не покидают компьютер"
        elif kind == "cloud":
            photo_line = f"облако, {html.escape(name)} — фото уходят провайдеру"
        else:
            photo_line = "нет — " + html.escape(name)
        lines = [f"Чтение фото: {photo_line}",
                 f"Облачный ИИ для текста: {html.escape(state)}",
                 "Договор: " + ("готов к сборке" if not ensure_builder()
                                  else "не соберётся — " + html.escape(BUILD_ERR)),
                 "Разбор текста регулярками: всегда работает"]
        send(chat_id, "\n".join(lines))
        return
    if text == "/reset":
        clear_session(chat_id)
        send(chat_id, "Сброшено. Присылайте следующего клиента.")
        return
    if text == "/raw":
        raw = SEEN_TEXT.get(chat_id)
        if not raw:
            send(chat_id, "Фото ещё не распознавались.")
        else:
            send(chat_id, "Что увидел OCR:\n\n<pre>"
                 + html.escape(raw[:3500]) + "</pre>")
        return
    if text == "/show":
        data = SESSIONS.get(chat_id)
        if not data:
            send(chat_id, "Пока ничего не разобрано.")
        else:
            send(chat_id, render_reply(data, validate(data), chat_id=chat_id), keyboard_for(data))
        return
    if text.startswith("/fix"):
        data = SESSIONS.get(chat_id)
        if not data:
            send(chat_id, "Сначала пришлите данные клиента.")
            return
        arg = text[4:].strip()
        if "=" not in arg:
            send(chat_id, "Формат: <code>/fix bank_bik=044030653</code>\n\nПоля: "
                          + ", ".join(f"<code>{f}</code>" for f in FIELDS))
            return
        key, _, val = arg.partition("=")
        key, val = key.strip(), val.strip()
        if key not in FIELDS:
            send(chat_id, f"Нет поля <code>{html.escape(key)}</code>. Доступны: "
                          + ", ".join(f"<code>{f}</code>" for f in FIELDS))
            return
        apply_edit(chat_id, key, val)
        return
    if text == "/edit":
        data = SESSIONS.get(chat_id)
        if not data:
            send(chat_id, "Сначала пришлите данные клиента.")
        else:
            send(chat_id, "Какое поле поправить?", fields_keyboard(data))
        return

    if len(text) < 15:
        send(chat_id, "Слишком коротко. Пришлите данные клиента целиком, фото "
                      "документов или /help.")
        return

    if text.lstrip().startswith("{"):
        try:
            fresh = json.loads(text[text.find("{"):text.rfind("}") + 1])
        except Exception:
            fresh, _ = parse_client(text)
    else:
        fresh, _ = parse_client(text)

    data = merge_into_session(chat_id, fresh)
    send(chat_id, render_reply(data, validate(data), chat_id=chat_id), keyboard_for(data))


def handle_build(chat_id):
    data = SESSIONS.get(chat_id)
    if not data:
        send(chat_id, "Сначала пришлите данные клиента.")
        return
    if ensure_builder():
        send(chat_id, "Договор собрать не могу: " + html.escape(BUILD_ERR))
        return
    errs = [i for i in validate(data) if i.level == "error"]
    if errs:
        send(chat_id, "Не формирую — сначала исправьте:\n" + "\n".join(
            f"• {html.escape(FIELD_LABELS.get(i.field, i.field))} — {html.escape(i.message)}"
            for i in errs) + "\n\nИсправить: «✏️ Редактировать».", keyboard_for(data))
        return
    send(chat_id, "Формирую договор…")
    try:
        res = build_contract(data)
    except Exception as e:
        send(chat_id, f"Ошибка: {html.escape(str(e))}")
        return
    caption = f"Договор № {res['number']}"
    send_file(chat_id, res["docx"], caption)
    if res["pdf"]:
        send_file(chat_id, res["pdf"], caption)
    if res["warning"]:
        send(chat_id, html.escape(res["warning"]))
    clear_session(chat_id)


def handle_callback(chat_id, message_id, action):
    data = SESSIONS.get(chat_id) or {}

    if action == "build":
        handle_build(chat_id)
    elif action == "reset":
        clear_session(chat_id)
        send(chat_id, "Сброшено. Присылайте следующего клиента.")
    elif action == "edit":
        if not data:
            send(chat_id, "Карточка клиента пуста — сначала пришлите данные.")
            return
        # кнопки под тем же сообщением меняются на список полей
        edit_keyboard(chat_id, message_id, fields_keyboard(data))
    elif action == "back":
        EDITING.pop(chat_id, None)
        edit_keyboard(chat_id, message_id, keyboard_for(data))
    elif action.startswith("edit:"):
        field = action[5:]
        if field not in FIELDS or not data:
            return
        EDITING[chat_id] = field
        cur = data.get(field) or ""
        send(chat_id,
             f"✏️ <b>{html.escape(FIELD_LABELS[field])}</b>\n"
             "Сейчас: " + (f"<code>{html.escape(cur)}</code>" if cur else "—")
             + "\n\nНапишите новое значение следующим сообщением.",
             [[{"text": "🧹 Очистить поле", "callback_data": "clear:" + field}],
              [{"text": "↩️ Отмена", "callback_data": "cancel"}]])
    elif action.startswith("clear:"):
        field = action[6:]
        if field in FIELDS and data:
            EDITING.pop(chat_id, None)
            apply_edit(chat_id, field, "")
    elif action == "cancel":
        EDITING.pop(chat_id, None)
        if data:
            send(chat_id, "Без изменений.\n\n" + render_reply(data, validate(data), chat_id=chat_id),
                 keyboard_for(data))


def handle_update(upd):
    cb = upd.get("callback_query")
    if cb:
        chat_id = cb["message"]["chat"]["id"]
        user_id = cb["from"]["id"]
        if CFG["allowed_ids"] and user_id not in CFG["allowed_ids"]:
            return
        api("answerCallbackQuery", callback_query_id=cb["id"])
        handle_callback(chat_id, cb["message"]["message_id"], cb.get("data") or "")
        return

    msg = upd.get("message") or upd.get("edited_message")
    if not msg:
        return
    chat_id = msg["chat"]["id"]
    user_id = msg.get("from", {}).get("id", 0)

    if not CFG["allowed_ids"]:
        send(chat_id, f"Бот ещё не настроен. Ваш Telegram ID: <code>{user_id}</code>\n"
                      "Впишите его в <code>allowed_ids</code> в bot_config.json "
                      "и перезапустите бота.")
        return
    if user_id not in CFG["allowed_ids"]:
        send(chat_id, "Доступ закрыт.")
        return

    caption = msg.get("caption") or ""

    # ждём новое значение поля после «✏️ Редактировать» → выбор поля.
    # Команда или фото/файл вместо текста отменяют правку.
    field = EDITING.pop(chat_id, None)
    if field and msg.get("text") and not msg["text"].startswith("/"):
        apply_edit(chat_id, field, msg["text"])
        return

    # фото, сжатое телеграмом: берём самый крупный размер
    if msg.get("photo"):
        if photo_engine()[0] is None:
            send(chat_id, "Чтение фото не настроено — см. <code>/status</code>. "
                          "Пришлите данные текстом.")
            return
        biggest = sorted(msg["photo"], key=lambda p: p.get("file_size") or 0)[-1]
        queue_photo(chat_id, download(biggest["file_id"]), "image/jpeg", caption)
        return

    # картинка, отправленная файлом — качество выше, читается лучше
    doc = msg.get("document")
    if doc and str(doc.get("mime_type", "")).startswith("image/"):
        if photo_engine()[0] is None:
            send(chat_id, "Чтение фото не настроено — см. <code>/status</code>. "
                          "Пришлите данные текстом.")
            return
        queue_photo(chat_id, download(doc["file_id"]), doc["mime_type"], caption)
        return

    if doc:
        name = doc.get("file_name", "")
        import files_in
        if not files_in.is_supported(name, doc.get("mime_type", "")):
            send(chat_id, "Такой файл я не читаю. Подойдут: txt, pdf, docx, "
                          "картинка или просто текст сообщением.")
            return
        send(chat_id, f"Читаю файл {html.escape(name or '')}…")
        try:
            blob = download(doc["file_id"])
            res = files_in.read_rich(blob, name, doc.get("mime_type", ""))
        except Exception as e:
            send(chat_id, "Не смог открыть файл: " + html.escape(str(e)[:200]))
            return
        text, src, mrz_fields = res["text"], res["source"], res["mrz"]
        if not text.strip() and not mrz_fields:
            send(chat_id, "Из файла не удалось достать текст: " + html.escape(src))
            return
        SEEN_TEXT[chat_id] = text
        combined = (text + "\n" + caption).strip()
        fresh, _ = parse_client(combined)
        if mrz_fields:
            fresh = apply_mrz(fresh, mrz_fields, page_text=combined)
        data = merge_into_session(chat_id, fresh, from_passport=bool(mrz_fields))
        send(chat_id, render_reply(data, validate(data), html.escape(src), chat_id)
             + "\n\nЧто было в файле: <code>/raw</code>", keyboard_for(data))
        return

    handle_text(chat_id, user_id, msg.get("text") or caption)


# --- цикл опроса ------------------------------------------------------------

def main():
    if not CFG["token"]:
        sys.exit('Не задан токен. bot_config.json: {"token": "...", "allowed_ids": []}')
    me = api("getMe")
    if not me.get("ok"):
        sys.exit(f"Telegram не принял токен: {me}")
    print(f"Бот @{me['result'].get('username')} запущен. Ctrl+C — остановить.")
    if not CFG["allowed_ids"]:
        print("ВНИМАНИЕ: allowed_ids пуст — бот только сообщит вам ваш ID.")
    try:
        import llm_provider
        print("ИИ:", llm_provider.status()[1])
    except Exception as e:
        print("ИИ не настроен:", e)
    if BUILD_ERR:
        print("ВНИМАНИЕ: договор не соберётся —", BUILD_ERR)

    offset = None
    while True:
        try:
            r = api("getUpdates", offset=offset, timeout=20,
                    allowed_updates=["message", "edited_message", "callback_query"])
            if r.get("ok"):
                for upd in r.get("result", []):
                    offset = upd["update_id"] + 1
                    try:
                        handle_update(upd)
                    except Exception:
                        traceback.print_exc()
            flush_photos()
        except KeyboardInterrupt:
            print("Остановлен.")
            break
        except Exception:
            traceback.print_exc()
            time.sleep(5)


if __name__ == "__main__":
    main()
