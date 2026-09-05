import os
import json
import re
import requests
from dotenv import load_dotenv

load_dotenv()

CF_ACCOUNT_ID = os.getenv("CF_ACCOUNT_ID")
CF_API_TOKEN = os.getenv("CF_API_TOKEN")
CF_MODEL = os.getenv("CF_MODEL", "@cf/meta/llama-3.1-8b-instruct")

SYSTEM_PROMPT = """Ты — интеллектуальный анализатор сообщений о дорожной обстановке, проверках документов, патрулях и блокпостах в городе Днепр (Украина).

Люди часто используют сленг:
- ТЦК / военные / патрули: "оливки", "зеленые", "зелень", "синие", "хмары", "тучи", "дождь", "снег", "баклажаны", "пиксели", "бусик", "бус", "роздача", "пишут", "выписывают", "тормозят", "проверяют".
- Спокойная обстановка: "чисто", "солнечно", "сухо", "проезд свободный".

ПРАВИЛА АНАЛИЗА:
1. ВОПРОСЫ НЕ ЯВЛЯЮТСЯ ОТЧЕТОМ. Если человек просто спрашивает: "Как на Правды?", "Кто знает что на Кавериной?", "Подскажите, чисто?" — это НЕ актуальное событие. Установи relevant: false.
2. Флуд, мемы, спам, ругань, реклама, ссылки, стикеры, обсуждения политики — relevant: false.
3. ТОЛЬКО реальные сообщения с указанием места и факта проверки или блокпоста (например: "На Калиновой возле Мириады стоят 3 зеленых и 2 синих, тормозят в сторону центра" или "Новый мост в центр — чисто") — relevant: true.

Отвечай СТРОГО в формате валидного JSON (без лишнего текста и без markdown блоков):
{
  "relevant": true,
  "location": "Улица, перекресток, ориентир или район (на русском)",
  "status": "Суть (например: Проверка документов / Мобильный блокпост / Чисто / Патруль)",
  "summary": "Краткое понятное описание обстановки в одно предложение"
}

Если сообщение не содержит факта или это вопрос/флуд:
{
  "relevant": false
}
"""

def extract_cf_text(result_data: dict) -> str:
    """
    Универсальное и безопасное извлечение текста ответа из Cloudflare Workers AI.
    """
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

        # Проверка формата choices (OpenAI style)
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
    """
    Отправляет входящий текст из каналов/комментариев в Cloudflare Workers AI для классификации.
    """
    clean_text = text.strip()
    if len(clean_text) < 4:
        return {"relevant": False}

    if clean_text.endswith("?") and not any(kw in clean_text.lower() for kw in ["стоят", "пишут", "тормозят", "чисто"]):
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
            
            # Извлекаем JSON из ответа
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
    """
    Отвечает на прямой вопрос пользователя в боте, используя контекст последних дорожных событий.
    """
    if not recent_events:
        events_context = "За последние часы сообщений о проверках, блокпостах или патрулях не зафиксировано."
    else:
        lines = []
        for ev in recent_events[-20:]:
            t = ev.get("time", "")
            loc = ev.get("location", "")
            stat = ev.get("status", "")
            summ = ev.get("summary", "")
            raw = ev.get("raw_text", "")
            lines.append(f"• [{t}] {loc} — {stat}: {summ} (Цитата: \"{raw}\")")
        events_context = "\n".join(lines)

    system_q = (
        "Ты — вежливый и точный виртуальный помощник по дорожной обстановке и блокпостам в городе Днепр.\n"
        "Твоя задача — отвечать на вопросы пользователя о ситуации в конкретных районах, на улицах или мостах.\n"
        "Отвечай кратко, понятно и по сути на русском языке.\n"
        "Используй ТОЛЬКО факты из предоставленной ниже сводки последних сообщений. Не выдумывай адреса.\n"
        "Если по запрошенному месту информации в сводке нет, так и скажи: за последнее время информации по этой локации не поступало, скорее всего всё спокойно."
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

    return "Не удалось получить ответ от нейросети. Попробуйте еще раз через минуту."
