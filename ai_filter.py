import os
import json
import re
import requests
from dotenv import load_dotenv

load_dotenv()

CF_ACCOUNT_ID = os.getenv("CF_ACCOUNT_ID")
CF_API_TOKEN = os.getenv("CF_API_TOKEN")
CF_MODEL = os.getenv("CF_MODEL", "@cf/meta/llama-3.1-8b-instruct")

SYSTEM_PROMPT = """Ты — интеллектуальный анализатор дорожной обстановки, блокпостов и проверок документов в городе Днепр (Украина).

Люди в комментариях и каналах используют специфический сленг и сокращения Днепра:
- Патрули / ТЦК / полиция: "тцк", "бп", "оливки", "зеленые", "зелень", "синие", "хмары", "тучи", "дождь", "снег", "баклажаны", "пиксели", "бусик", "бус", "роздача", "пишут", "выписывают", "тормозят", "проверяют", "стоят", "мусор", "мусора", "копы", "легавые", "дастер", "дастеры", "приус", "шкода", "фантом", "листовка", "листовки", "вышли к парню".
- Локации и сокращения Днепра: "дамба", "малашка" / "малина" (ул. Малиновского), "скала", "юм" (Южный мост), "усть-самарский" (Усть-Самарский мост), "опс", "гаванская", "брсм", "атб", "клочко", "правда", "каверина", "рабочая", "калиновая", "победа", "парус".
- Чистая / свободная обстановка: "чисто", "свободно", "проезд свободный", "норм", "хо ро шо", "хорошо", "уехали", "сьебались", "сухо", "солнечно".

ПРАВИЛА АНАЛИЗА:
1. ВОПРОСЫ НЕ ЯВЛЯЮТСЯ ОТЧЕТОМ. Если человек просто спрашивает: "На дамбе уехали?", "Что по дамбе?", "Где на дамбе стоят?" — relevant: false.
2. Флуд, стикеры, мемы, споры — relevant: false.
3. ТОЛЬКО сообщения с фактом (например: "Остановка ОПС стоят тцк", "Поворот с малины на усть-самарский БП", "Двое из дастера вышли к парню возле складов Евы", "БСМ гаванское АТБ Юм Победа свободна", "С дамбы сьебались") — relevant: true.

Отвечай СТРОГО в формате валидного JSON (без лишнего текста и без markdown блоков):
{
  "relevant": true,
  "location": "Локация или ориентир (на русском, например: Дамба, ул. Малиновского, Усть-Самарский мост, Остановка ОПС)",
  "status": "Суть (например: Блокпост / ТЦК / Патруль на Дастере / Чисто / Уехали)",
  "summary": "Краткое понятное описание обстановки в одно предложение"
}

Если это вопрос или флуд:
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

def analyze_text_with_cf_ai(text: str) -> dict:
    clean_text = text.strip()
    if len(clean_text) < 3:
        return {"relevant": False}

    # Если это вопрос без утверждений
    if clean_text.endswith("?") and not any(kw in clean_text.lower() for kw in ["стоят", "пишут", "тормозят", "чисто", "бп", "тцк"]):
        return {"relevant": False}

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
        response = requests.post(url, headers=headers, json=payload, timeout=12)
        if response.status_code == 200:
            result_data = response.json()
            raw_response = extract_cf_text(result_data).strip()
            json_match = re.search(r"\{.*\}", raw_response, re.DOTALL)
            if json_match:
                parsed = json.loads(json_match.group(0))
                return parsed
        else:
            print(f"[Cloudflare AI Error] HTTP {response.status_code}: {response.text}")
    except Exception as e:
        print(f"[Cloudflare AI Exception] {e}")

    return {"relevant": False}

def answer_user_question_with_cf_ai(user_question: str, recent_events: list) -> str:
    if not recent_events:
        return (
            "ℹ️ На данный момент активных сообщений о проверках по вашему запросу нет. "
            "На дорогах спокойно."
        )

    lines = []
    for ev in recent_events[-25:]:
        t = ev.get("time", "")
        loc = ev.get("location", "")
        stat = ev.get("status", "")
        summ = ev.get("summary", "")
        raw = ev.get("raw_text", "")
        lines.append(f"• [{t}] {loc} — {stat}: {summ} (Цитата: \"{raw}\")")
    events_context = "\n".join(lines)

    system_q = (
        "Ты — виртуальный дорожный помощник по Днепру.\n"
        "Отвечай кратко, актуально и по сути на основе сводки последних сообщений.\n"
        "Если по запрошенному месту информации нет, напиши: по этому району за последнее время сообщений не было."
    )

    user_prompt = (
        f"Сводка актуальных событий:\n{events_context}\n\n"
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
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        if response.status_code == 200:
            result_data = response.json()
            answer = extract_cf_text(result_data).strip()
            if answer:
                return answer
    except Exception as e:
        print(f"[Cloudflare AI Q&A Error] {e}")

    return "Информация по этой локации не поступала, обстановка спокойная."
