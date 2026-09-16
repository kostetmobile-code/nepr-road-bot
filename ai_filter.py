import os
import json
import re
import requests
from dotenv import load_dotenv

load_dotenv()

CF_ACCOUNT_ID = os.getenv("CF_ACCOUNT_ID")
CF_API_TOKEN = os.getenv("CF_API_TOKEN")
CF_MODEL = os.getenv("CF_MODEL", "@cf/meta/llama-3.1-8b-instruct")

# Словарь ключевых слов для резервного анализа (если ИИ временно недоступен или ошибся)
DANGER_KEYWORDS = [
    "тцк", "бп", "блокпост", "блок-пост", "оливки", "зеленые", "зелень", "синие", 
    "хмары", "тучи", "дождь", "баклажаны", "пиксели", "бусик", "бус", "роздача", 
    "пишут", "выписывают", "тормозят", "проверяют", "стоят", "мусор", "мусора", 
    "копы", "легавые", "дастер", "приус", "шкода", "фантом", "листовка", "листовки",
    "вышли к парню", "прессуют", "патруль", "патрули", "патрулька"
]

CLEAR_KEYWORDS = [
    "чисто", "свободно", "проезд свободный", "норм", "хо ро шо", "хорошо", 
    "уехали", "сьебались", "сухо", "солнечно", "пусто", "никого"
]

COMMON_LOCATIONS = [
    "дамба", "малиновского", "малашка", "малина", "южный мост", "юм", "усть-самарский",
    "самарский", "новый мост", "старый мост", "кайдакский", "калиновая", "правда",
    "слобожанский", "рабочая", "космичка", "космическая", "победа", "парус", "покровский",
    "коммунар", "красный камень", "тополь", "сокол", "гагарина", "науки", "кирова",
    "полевая", "героев сталинграда", "богдана хмельницкого", "бх", "квартал", "шинник",
    "12 квартал", "клочко", "левобережный", "левый 3", "березинка", "караван", "вавилон",
    "мост сити", "центр", "вокзал", "автовокзал", "островского", "старомостовая", "придник",
    "приднепровск", "чапли", "рыбальск", "игрень", "самар", "подгородное", "юбилейный",
    "золотые ключи", "донецкое шоссе", "криворожская", "титова", "поля", "петрозаводская",
    "передовая", "широкая", "гаванская", "брсм", "окко", "вок", "вог", "авиас", "атб", "варус"
]

SYSTEM_PROMPT = """Ты — интеллектуальный анализатор дорожной обстановки, блокпостов и проверок документов в городе Днепр (Украина).

Люди в комментариях и каналах используют сленг и сокращения Днепра:
- Патрули / ТЦК / полиция: "тцк", "бп", "оливки", "зеленые", "зелень", "синие", "хмары", "тучи", "дождь", "баклажаны", "пиксели", "бусик", "бус", "роздача", "пишут", "выписывают", "тормозят", "проверяют", "стоят", "мусор", "мусора", "копы", "легавые", "дастер", "приус", "шкода", "фантом", "листовка", "вышли к парню".
- Локации: "дамба", "малашка" / "малина" (ул. Малиновского), "скала", "юм" (Южный мост), "усть-самарский" (Усть-Самарский мост), "опс", "гаванская", "брсм", "атб", "клочко", "правда", "каверина", "рабочая", "калиновая", "победа", "парус", "чапли", "придник", "космичка", "бх".
- Чистая обстановка: "чисто", "свободно", "проезд свободный", "норм", "хо ро шо", "хорошо", "уехали", "сьебались", "сухо", "солнечно".

ПРАВИЛА АНАЛИЗА:
1. ВОПРОСЫ НЕ ЯВЛЯЮТСЯ ОТЧЕТОМ. Если человек просто спрашивает: "На дамбе уехали?", "Что по дамбе?", "Где на дамбе стоят?", "Чисто?" — relevant: false.
2. Флуд, мемы, стикеры, новости политики, общие обсуждения — relevant: false.
3. ТОЛЬКО сообщения с фактом (например: "Остановка ОПС стоят тцк", "Поворот с малины на усть-самарский БП", "Двое из дастера вышли к парню возле складов Евы", "БСМ гаванское АТБ Юм Победа свободна", "С дамбы сьебались", "Гаванская брсм бп") — relevant: true.

Отвечай СТРОГО в формате JSON (без лишнего текста и без markdown блоков ```json):
{
  "relevant": true,
  "location": "Локация или ориентир на русском (например: ул. Малиновского, Дамба, Южный мост, Чапли, БРСМ Гаванская)",
  "status": "Суть (например: Блокпост / ТЦК / Патруль / Проверка / Чисто / Уехали)",
  "summary": "Краткое понятное описание обстановки в одно предложение"
}

Если это вопрос, спам или флуд:
{
  "relevant": false
}
"""

def extract_cf_text(result_data: dict) -> str:
    if not isinstance(result_data, dict):
        return str(result_data)

    res_obj = result_data.get("result", {})
    if isinstance(res_obj, str):
        return res_obj

    if isinstance(res_obj, dict):
        resp = res_obj.get("response")
        if isinstance(resp, str):
            return resp
        if isinstance(resp, dict):
            return resp.get("content") or resp.get("text") or json.dumps(resp, ensure_ascii=False)

        choices = res_obj.get("choices")
        if isinstance(choices, list) and len(choices) > 0:
            c = choices[0]
            if isinstance(c, dict):
                msg = c.get("message", {})
                if isinstance(msg, dict):
                    return msg.get("content", "")
                return c.get("text", "")

        if "content" in res_obj and isinstance(res_obj["content"], str):
            return res_obj["content"]

    return str(res_obj)

def fallback_rule_analysis(text: str) -> dict:
    """
    Резервный алгоритм анализа по ключевым словам (работает мгновенно, без интернета и без сбоев ИИ).
    Гарантирует, что ни одно реальное сообщение об опасности не будет пропущено.
    """
    low = text.lower().strip()
    
    # Отсекаем явные вопросы
    if low.endswith("?") and not any(kw in low for kw in ["стоят", "пишут", "тормозят", "бп", "тцк", "дастер"]):
        return {"relevant": False, "reason": "вопрос"}

    has_danger = any(kw in low for kw in DANGER_KEYWORDS)
    has_clear = any(kw in low for kw in CLEAR_KEYWORDS)

    if not has_danger and not has_clear:
        return {"relevant": False, "reason": "нет ключевых слов обстановки"}

    # Поиск локации
    found_loc = "Днепр"
    for loc in COMMON_LOCATIONS:
        if loc in low:
            found_loc = loc.title()
            break

    if has_clear and not has_danger:
        status = "Чисто"
        summary = f"Проезд свободный ({found_loc})"
    elif "тцк" in low:
        status = "ТЦК / Проверка"
        summary = f"Зафиксированы сотрудники ТЦК в районе {found_loc}"
    elif "бп" in low or "блокпост" in low:
        status = "Блокпост"
        summary = f"Установлен блокпост ({found_loc})"
    elif "дастер" in low or "патруль" in low or "мусор" in low:
        status = "Патруль"
        summary = f"Патрульный экипаж ({found_loc})"
    else:
        status = "Проверка"
        summary = f"Внимание, активность патрулей ({found_loc})"

    return {
        "relevant": True,
        "location": found_loc,
        "status": status,
        "summary": summary,
        "engine": "fallback_rules"
    }

def analyze_text_with_cf_ai(text: str) -> dict:
    """
    Анализирует текст через Cloudflare AI. 
    Если ИИ недоступен, тормозит или ошибся при наличии опасности — подключается резервный фильтр.
    """
    clean_text = text.strip()
    if len(clean_text) < 3:
        return {"relevant": False, "reason": "слишком короткое"}

    # Быстрый фильтр простых вопросов
    if clean_text.endswith("?") and not any(kw in clean_text.lower() for kw in ["стоят", "пишут", "тормозят", "бп", "тцк", "дастер"]):
        return {"relevant": False, "reason": "простой вопрос"}

    # 1. Запрос в Cloudflare Workers AI
    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai/run/{CF_MODEL}"
    headers = {
        "Authorization": f"Bearer {CF_API_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Сообщение:\n{clean_text}"}
        ],
        "max_tokens": 256,
        "temperature": 0.1
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=8)
        if response.status_code == 200:
            result_data = response.json()
            raw_response = extract_cf_text(result_data).strip()
            json_match = re.search(r"\{.*\}", raw_response, re.DOTALL)
            if json_match:
                parsed = json.loads(json_match.group(0))
                if parsed.get("relevant") is True:
                    parsed["engine"] = "cloudflare_ai"
                    return parsed
    except Exception as e:
        print(f"[Cloudflare AI Warning] {e}. Переключение на резервный анализатор...")

    # 2. РЕЗЕРВНАЯ ПРОВЕРКА (Страховка):
    # Если ИИ выдал false или сбойнул, но в тексте есть жесткие маркеры опасности — спасаем сообщение!
    fallback = fallback_rule_analysis(clean_text)
    if fallback.get("relevant") is True:
        return fallback

    return {"relevant": False, "reason": "не относится к обстановке"}

def answer_user_question_with_cf_ai(user_question: str, recent_events: list) -> str:
    if not recent_events:
        return (
            "ℹ️ На данный момент активных сообщений о проверках или блокпостах нет. "
            "На дорогах спокойно."
        )

    lines = []
    for ev in recent_events[-25:]:
        t = ev.get("time", "")
        loc = ev.get("location", "")
        stat = ev.get("status", "")
        summ = ev.get("summary", "")
        raw = ev.get("raw_text", "")
        lines.append(f"• [{t}] {loc} — {stat}: {summ} (Сообщение: \"{raw}\")")
    events_context = "\n".join(lines)

    system_q = (
        "Ты — точный виртуальный помощник по дорожной обстановке в городе Днепр.\n"
        "Отвечай кратко, актуально и по сути на русском языке на основе приведенной ниже сводки.\n"
        "Если по запрошенной улице/району информации в сводке нет, четко скажи: за последнее время сообщений по этой локации не поступало, обстановка скорее всего спокойная."
    )

    user_prompt = (
        f"Актуальная сводка событий в Днепре:\n{events_context}\n\n"
        f"Вопрос пользователя: \"{user_question}\""
    )

    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai/run/{CF_MODEL}"
    headers = {
        "Authorization": f"Bearer {CF_API_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messages": [
            {"role": "system", "content": system_q},
            {"role": "user", "content": user_prompt}
        ],
        "max_tokens": 350,
        "temperature": 0.2
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        if response.status_code == 200:
            result_data = response.json()
            answer = extract_cf_text(result_data).strip()
            if answer:
                return answer
    except Exception as e:
        print(f"[Cloudflare AI Q&A Error] {e}")

    # Резервный ответ по локациям, если ИИ недоступен
    low_q = user_question.lower()
    matches = [ev for ev in recent_events if ev.get("location", "").lower() in low_q or any(w in ev.get("raw_text", "").lower() for w in low_q.split() if len(w) > 3)]
    if matches:
        last = matches[-1]
        return f"📍 По вашему запросу зафиксировано в {last.get('time')}: {last.get('location')} ({last.get('status')}) — {last.get('summary')}."

    return "По вашему запросу за последнее время сообщений не зафиксировано, на дорогах спокойно."
