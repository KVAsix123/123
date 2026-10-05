# -*- coding: utf-8 -*-
"""
Слой облачных ИИ-провайдеров: текст и картинки.

Два транспорта:
  * gemini — родной Gemini API. Нужен именно он: ключи нового формата «AQ.»
    не проходят через OpenAI-совместимый путь с Bearer-авторизацией, зато
    нормально работают с заголовком x-goog-api-key на родном эндпоинте.
  * openai — общий формат chat/completions: DeepSeek, OpenRouter, Groq, Ollama
    и любой другой совместимый сервис.

Настройка в llm_config.json рядом со скриптами:
    {
      "provider": "gemini",
      "api_key": "AQ....",
      "vision_model": "",       // пусто = подобрать автоматически
      "text_model": "",
      "photo_mode": "cloud"     // cloud | local | auto
    }

Имена моделей у Google меняются часто, поэтому при ошибке «модель не найдена»
модуль сам запрашивает список доступных и берёт подходящую flash-модель.
"""

import base64
import json
import os
import time

try:
    import requests
except ImportError:
    requests = None

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "llm_config.json")
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

PRESETS = {
    "gemini": {
        "transport": "gemini", "base_url": GEMINI_BASE,
        "text_model": "gemini-3.1-flash-lite-preview", "vision_model": "gemini-3.1-flash-lite-preview",
        "key_env": "GEMINI_API_KEY",
    },
    "deepseek": {
        "transport": "openai", "base_url": "https://api.deepseek.com",
        "text_model": "deepseek-flash", "vision_model": "deepseek-v4-flash-vision-exp",
        "key_env": "DEEPSEEK_API_KEY",
    },
    "openrouter": {
        "transport": "openai", "base_url": "https://openrouter.ai/api/v1",
        "text_model": "", "vision_model": "", "key_env": "OPENROUTER_API_KEY",
    },
    "groq": {
        "transport": "openai", "base_url": "https://api.groq.com/openai/v1",
        "text_model": "", "vision_model": "", "key_env": "GROQ_API_KEY",
    },
    "ollama": {
        "transport": "openai", "base_url": "http://localhost:11434/v1",
        "text_model": "qwen2.5", "vision_model": "", "key_env": "",
    },
    "none": {"transport": "", "base_url": "", "text_model": "", "vision_model": "", "key_env": ""},
}

_MODEL_CACHE = {"ts": 0, "names": []}


def load_config():
    cfg = {"provider": "none", "api_key": "", "text_model": "", "vision_model": "",
           "base_url": "", "photo_mode": "auto"}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8-sig") as f:
                cfg.update(json.load(f) or {})
        except Exception:
            pass
    preset = PRESETS.get(str(cfg.get("provider", "none")).lower(), PRESETS["none"])
    for field in ("base_url", "text_model", "vision_model"):
        if not cfg.get(field):
            cfg[field] = preset[field]
    cfg["transport"] = preset["transport"]
    cfg["key_env"] = preset.get("key_env", "")
    if not cfg.get("api_key") and cfg["key_env"]:
        cfg["api_key"] = os.environ.get(cfg["key_env"], "")
    return cfg


def _ready(cfg):
    if requests is None:
        return False, "не установлена библиотека requests"
    if not cfg["transport"]:
        return False, "облачный ИИ выключен"
    if not cfg["api_key"] and cfg["provider"] != "ollama":
        return False, f"нет ключа (впишите в llm_config.json или в {cfg['key_env']})"
    return True, ""


def status():
    cfg = load_config()
    ok, why = _ready(cfg)
    if not ok:
        return cfg, why
    return cfg, (f"{cfg['provider']}, фото: {cfg['vision_model'] or 'подберу сам'}, "
                 f"текст: {cfg['text_model'] or '—'}")


def text_available():
    cfg = load_config()
    return _ready(cfg)[0] and bool(cfg["text_model"] or cfg["transport"] == "gemini")


def vision_available():
    cfg = load_config()
    return _ready(cfg)[0] and bool(cfg["vision_model"] or cfg["transport"] == "gemini")


# --- родной Gemini ----------------------------------------------------------

def gemini_models(api_key, force=False):
    """Список доступных моделей. Кэшируется на час."""
    if not force and _MODEL_CACHE["names"] and time.time() - _MODEL_CACHE["ts"] < 3600:
        return _MODEL_CACHE["names"]
    r = requests.get(f"{GEMINI_BASE}/models", headers={"x-goog-api-key": api_key}, timeout=40)
    if r.status_code >= 400:
        raise RuntimeError(f"Gemini {r.status_code}: {r.text[:300]}")
    names = []
    for m in r.json().get("models", []):
        if "generateContent" in (m.get("supportedGenerationMethods") or []):
            names.append(m.get("name", "").split("/")[-1])
    _MODEL_CACHE.update({"ts": time.time(), "names": names})
    return names


def _pick_gemini_model(api_key, want_vision=True):
    """Подбирает flash-модель, если прописанная не нашлась."""
    names = gemini_models(api_key)
    # Prefer the stable Gemini 2.5 Flash model for text and image input.
    for preferred in ("gemini-3.1-flash-lite-preview", "gemini-flash-lite-latest",
                      "gemini-3-flash-preview", "gemini-2.5-flash-lite",
                      "gemini-2.5-flash"):
        if preferred in names:
            return preferred
    for key in ("flash-latest", "flash"):
        for n in names:
            low = n.lower()
            if key in low and "thinking" not in low and "image" not in low and "tts" not in low:
                return n
    return names[0] if names else ""


def _gemini_candidates(cfg, vision=False):
    """Модели по приоритету: текущая, затем доступные резервные Flash."""
    current = cfg["vision_model"] if vision else cfg["text_model"]
    result = []
    for model in (current, "gemini-flash-lite-latest", "gemini-3.1-flash-lite-preview",
                  "gemini-3-flash-preview", "gemini-2.5-flash-lite", "gemini-2.5-flash"):
        if model and model not in result:
            result.append(model)
    return result


def _is_retryable_gemini_error(exc):
    text = str(exc).lower()
    return any(mark in text for mark in (" 429:", " 500:", " 502:", " 503:",
                                        " 504:", "temporarily", "unavailable",
                                        "timed out", "timeout"))


def _gemini_call(model, parts, api_key, timeout=180, max_tokens=4000):
    body = {"contents": [{"parts": parts}],
            "generationConfig": {"temperature": 0, "maxOutputTokens": max_tokens,
                                 "response_mime_type": "application/json"}}
    url = f"{GEMINI_BASE}/models/{model}:generateContent"
    r = requests.post(url, headers={"x-goog-api-key": api_key,
                                    "Content-Type": "application/json"},
                      json=body, timeout=timeout)
    if r.status_code == 404:
        raise LookupError(f"модель {model} не найдена")
    if r.status_code >= 400:
        raise RuntimeError(f"Gemini {r.status_code}: {r.text[:400]}")
    data = r.json()
    cands = data.get("candidates") or []
    if not cands:
        fb = (data.get("promptFeedback") or {}).get("blockReason")
        raise RuntimeError(f"пустой ответ Gemini{' (' + str(fb) + ')' if fb else ''}")
    out = []
    for part in (cands[0].get("content") or {}).get("parts") or []:
        if "text" in part:
            out.append(part["text"])
    return "".join(out)


# --- общий OpenAI-совместимый ----------------------------------------------

def _openai_call(model, content, cfg, timeout=180, max_tokens=4000):
    headers = {"Content-Type": "application/json"}
    if cfg["api_key"]:
        headers["Authorization"] = "Bearer " + cfg["api_key"]
    r = requests.post(cfg["base_url"].rstrip("/") + "/chat/completions", headers=headers,
                      json={"model": model, "max_tokens": max_tokens, "temperature": 0,
                            "messages": [{"role": "user", "content": content}]},
                      timeout=timeout)
    if r.status_code >= 400:
        raise RuntimeError(f"{r.status_code}: {r.text[:400]}")
    return r.json()["choices"][0]["message"]["content"]


# --- общее ------------------------------------------------------------------

def _extract_json(raw):
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.lstrip().lower().startswith("json"):
            raw = raw.lstrip()[4:]
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("модель вернула не JSON: " + raw[:200])
    return json.loads(raw[start:end + 1])


def ask_json_text(prompt, timeout=120):
    cfg = load_config()
    ok, why = _ready(cfg)
    if not ok:
        raise RuntimeError(why)
    if cfg["transport"] == "gemini":
        last = None
        for model in _gemini_candidates(cfg):
            try:
                return _extract_json(_gemini_call(model, [{"text": prompt}], cfg["api_key"], timeout))
            except (LookupError, RuntimeError) as exc:
                last = exc
                if isinstance(exc, RuntimeError) and not _is_retryable_gemini_error(exc):
                    raise
        raise last
    return _extract_json(_openai_call(cfg["text_model"], prompt, cfg, timeout))


def ask_json_images(prompt, images, timeout=240):
    """images — список (bytes, mime). Возвращает разобранный JSON."""
    cfg = load_config()
    ok, why = _ready(cfg)
    if not ok:
        raise RuntimeError(why)

    if cfg["transport"] == "gemini":
        parts = [{"text": prompt}]
        for blob, mime in images:
            parts.append({"inline_data": {"mime_type": mime or "image/jpeg",
                                          "data": base64.b64encode(blob).decode("ascii")}})
        last = None
        for model in _gemini_candidates(cfg, vision=True):
            try:
                return _extract_json(_gemini_call(model, parts, cfg["api_key"], timeout))
            except (LookupError, RuntimeError) as exc:
                last = exc
                if isinstance(exc, RuntimeError) and not _is_retryable_gemini_error(exc):
                    raise
        raise last or RuntimeError("не нашлось ни одной доступной модели Gemini")

    content = [{"type": "text", "text": prompt}]
    for blob, mime in images:
        b64 = base64.b64encode(blob).decode("ascii")
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:{mime or 'image/jpeg'};base64,{b64}"}})
    return _extract_json(_openai_call(cfg["vision_model"], content, cfg, timeout))


def selftest():
    """Проверка ключа и моделей: python llm_provider.py"""
    cfg, state = status()
    print("Провайдер:", cfg["provider"], "| транспорт:", cfg["transport"] or "—")
    print("Состояние:", state)
    ok, why = _ready(cfg)
    if not ok:
        return
    if cfg["transport"] == "gemini":
        try:
            names = gemini_models(cfg["api_key"], force=True)
        except Exception as e:
            print("Ключ не принят:", e)
            return
        print(f"Ключ рабочий. Доступно моделей: {len(names)}")
        flash = [n for n in names if "flash" in n.lower()][:8]
        print("Подходящие:", ", ".join(flash) or "—")
        if cfg["vision_model"] not in names:
            print(f"ВНИМАНИЕ: модель «{cfg['vision_model']}» в списке не значится.")
            print("Подберу автоматически:", _pick_gemini_model(cfg["api_key"]))
        try:
            r = _gemini_call(cfg["vision_model"] if cfg["vision_model"] in names
                             else _pick_gemini_model(cfg["api_key"]),
                             [{"text": 'Верни ровно такой JSON: {"ok": true}'}], cfg["api_key"], 60)
            print("Пробный запрос:", r.strip()[:120])
        except Exception as e:
            print("Пробный запрос не прошёл:", e)


if __name__ == "__main__":
    selftest()
