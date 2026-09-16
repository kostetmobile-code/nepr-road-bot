import os
import sys
import threading

# === Исправление кодировки Windows (cp1251 -> UTF-8) для эмодзи в print() ===
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
from http.server import HTTPServer, BaseHTTPRequestHandler
import asyncio
import json
import datetime
import requests
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.functions.channels import GetFullChannelRequest, JoinChannelRequest
from ai_filter import analyze_text_with_cf_ai, answer_user_question_with_cf_ai

# =========================================================================
# 1. ЗАГРУЗКА КЛЮЧЕЙ
# =========================================================================
if os.path.exists("/etc/secrets/.env"):
    print("[Config] Загрузка ключей из /etc/secrets/.env (Render Docker)")
    load_dotenv("/etc/secrets/.env")
else:
    load_dotenv()

API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
STRING_SESSION = os.getenv("TELEGRAM_STRING_SESSION", "")
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "https://nepr-road-bot.onrender.com")

# =========================================================================
# 2. ВРЕМЕННОЙ ПОЯС КИЕВ / ДНЕПР (UTC+3)
# =========================================================================
KYIV_TZ = datetime.timezone(datetime.timedelta(hours=3))

def to_kyiv_time(dt=None) -> datetime.datetime:
    """Возвращает точное местное время Днепра (UTC+3)"""
    if dt is None:
        return datetime.datetime.now(KYIV_TZ)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(KYIV_TZ)

# =========================================================================
# 3. МГНОВЕННЫЙ ВЕБ-СЕРВЕР ДЛЯ RENDER (Порт 10000)
# =========================================================================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        now_str = to_kyiv_time().strftime("%Y-%m-%d %H:%M:%S")
        self.wfile.write(f"OK: Dnepr Road Bot is actively running. Kyiv time: {now_str}".encode("utf-8"))

    def log_message(self, format, *args):
        pass

def run_immediate_http():
    port = int(os.getenv("PORT", 10000))
    try:
        httpd = HTTPServer(("0.0.0.0", port), HealthHandler)
        print(f"🚀 [Render Web Port] Веб-сервер мгновенно открыл порт {port} на 0.0.0.0!")
        httpd.serve_forever()
    except Exception as e:
        print(f"[Render Port Error] {e}")

web_thread = threading.Thread(target=run_immediate_http, daemon=True)
web_thread.start()

SETTINGS_FILE = "user_settings.json"
EVENTS_FILE = "events_history.json"
CHANNELS_TO_MONITOR = ["pridybai", "dnepr_bez_tck", "agendaDnepr"]

# Журнал самоотладки (хранит последние 20 перехваченных сообщений и причину решения)
audit_log = []

# Дедупликация с ограничением по времени (15 минут)
recent_alerts_map = {}

def is_duplicate_alert(key: str, ttl_minutes: int = 15) -> bool:
    now_ts = datetime.datetime.now().timestamp()
    expired = [k for k, ts in recent_alerts_map.items() if now_ts - ts > ttl_minutes * 60]
    for k in expired:
        del recent_alerts_map[k]
    if key in recent_alerts_map:
        return True
    recent_alerts_map[key] = now_ts
    return False

# ==========================================
# Управление пользователями и ключевыми словами
# ==========================================
def load_all_users() -> dict:
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_all_users(data: dict):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_user(chat_id: int, user_name: str = "Пользователь") -> dict:
    users = load_all_users()
    uid = str(chat_id)
    if uid not in users:
        users[uid] = {
            "name": user_name,
            "keywords": [],
            "mode": "all",  # По умолчанию ставим "all" (все события), чтобы новичок не пропускал опасность!
            "state": None
        }
        save_all_users(users)
    return users[uid]

def update_user(chat_id: int, updates: dict):
    users = load_all_users()
    uid = str(chat_id)
    if uid in users:
        users[uid].update(updates)
    else:
        users[uid] = updates
    save_all_users(users)

# ==========================================
# История событий
# ==========================================
def load_events() -> list:
    if os.path.exists(EVENTS_FILE):
        try:
            with open(EVENTS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_event(event_dict: dict):
    events_list = load_events()
    for existing in events_list:
        if existing.get("raw_text") == event_dict.get("raw_text"):
            return
    events_list.append(event_dict)
    if len(events_list) > 100:
        events_list = events_list[-100:]
    with open(EVENTS_FILE, "w", encoding="utf-8") as f:
        json.dump(events_list, f, ensure_ascii=False, indent=2)

# ==========================================
# Отправка сообщений и Меню
# ==========================================
def send_telegram_bot_message(chat_id: int, text: str, reply_markup=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(url, json=payload, timeout=8)
    except Exception as e:
        print(f"[Bot Send Error] {e}")

def get_main_keyboard(mode: str = "all"):
    mode_text = "🎯 Режим: Только мои слова" if mode == "only_keywords" else "🔔 Режим: Все события города"
    return {
        "keyboard": [
            [{"text": "📋 Последняя сводка"}, {"text": "📍 Мои локации"}],
            [{"text": "➕ Добавить слово"}, {"text": "❌ Удалить слово"}],
            [{"text": mode_text}, {"text": "🛠 Диагностика"}]
        ],
        "resize_keyboard": True
    }

# ==========================================
# Автоматический антисон (Keep-Alive Self Ping)
# ==========================================
async def keep_alive_self_ping():
    print(f"[Anti-Sleep] Фоновый пинг запущен для {RENDER_URL}")
    while True:
        await asyncio.sleep(600)
        try:
            resp = await asyncio.to_thread(requests.get, RENDER_URL, timeout=15)
            print(f"[Anti-Sleep] Пинг отправлен: HTTP {resp.status_code}")
        except Exception as e:
            print(f"[Anti-Sleep Ошибка] {e}")

# ==========================================
# Формирование отчета самодиагностики
# ==========================================
def build_diagnostic_report(user_id: int) -> str:
    kyiv_now = to_kyiv_time().strftime("%H:%M:%S")
    user = get_user(user_id)
    u_mode = user.get("mode", "all")
    u_keywords = user.get("keywords", [])
    
    mode_str = "🎯 Только мои слова" if u_mode == "only_keywords" else "🔔 Все события города"
    kw_str = ", ".join(u_keywords) if u_keywords else "(список пуст)"

    lines = [
        "🛠 <b>САМОДИАГНОСТИКА И СТАТУС БОТА:</b>\n",
        f"🕒 <b>Точное время (Днепр):</b> <code>{kyiv_now}</code> (UTC+3)",
        f"⚙️ <b>Ваш режим:</b> <b>{mode_str}</b>",
        f"📍 <b>Ваши ключевые слова:</b> <code>{kw_str}</code>\n"
    ]

    if u_mode == "only_keywords" and u_keywords:
        lines.append(
            "⚠️ <b>ВНИМАНИЕ:</b> У вас включен режим фильтрации!\n"
            f"Бот присылает сообщения <b>ТОЛЬКО</b> если в тексте есть: <i>{kw_str}</i>.\n"
            "Все остальные блокпосты города отсекаются вашим личным фильтром!\n"
            "<i>(Чтобы получать ВСЕ события города, нажмите кнопку «Режим» внизу).</i>\n"
        )

    lines.append("📜 <b>Последние перехваченные сообщения из эфира:</b>")
    if not audit_log:
        lines.append("<i>Пока сообщений в журнале нет (ожидаем новые публикации).</i>")
    else:
        for item in reversed(audit_log[-6:]):
            lines.append(
                f"• [{item['time']}] <b>{item['source']}</b>: «{item['text']}»\n"
                f"  └ <b>Вердикт:</b> {item['decision']}"
            )

    return "\n".join(lines)

# ==========================================
# Фоновый диалог бота
# ==========================================
async def bot_polling_loop():
    offset = 0
    print("[Bot] Интерактивное меню бота запущено...")
    while True:
        try:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?offset={offset}&timeout=20"
            resp = await asyncio.to_thread(requests.get, url, timeout=25)
            if resp.status_code == 200:
                data = resp.json()
                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    msg = update.get("message")
                    if msg and "from" in msg:
                        user_id = msg["from"]["id"]
                        user_name = msg["from"].get("first_name", "Пользователь")
                        raw_text = msg.get("text", "").strip()

                        user = get_user(user_id, user_name)
                        state = user.get("state")
                        keywords = user.get("keywords", [])
                        mode = user.get("mode", "all")

                        if state == "wait_add":
                            if raw_text in ["/cancel", "отмена", "Отмена"]:
                                update_user(user_id, {"state": None})
                                send_telegram_bot_message(user_id, "❌ Добавление отменено.", reply_markup=get_main_keyboard(mode))
                            else:
                                new_kw = raw_text.strip().title()
                                if new_kw.lower() not in [k.lower() for k in keywords]:
                                    keywords.append(new_kw)
                                update_user(user_id, {"keywords": keywords, "state": None})
                                success_msg = (
                                    f"✅ Локация <b>«{new_kw}»</b> успешно добавлена!\n\n"
                                    f"Теперь бот будет отслеживать любые проверки и блокпосты с этим словом."
                                )
                                send_telegram_bot_message(user_id, success_msg, reply_markup=get_main_keyboard(mode))
                            continue

                        if state == "wait_del":
                            if raw_text in ["/cancel", "отмена", "Отмена"]:
                                update_user(user_id, {"state": None})
                                send_telegram_bot_message(user_id, "❌ Удаление отменено.", reply_markup=get_main_keyboard(mode))
                            else:
                                kw_to_del = raw_text.strip().lower()
                                new_keywords = [k for k in keywords if k.lower() != kw_to_del]
                                if len(new_keywords) < len(keywords):
                                    update_user(user_id, {"keywords": new_keywords, "state": None})
                                    send_telegram_bot_message(user_id, f"🗑 Локация <b>«{raw_text}»</b> удалена.", reply_markup=get_main_keyboard(mode))
                                else:
                                    update_user(user_id, {"state": None})
                                    send_telegram_bot_message(user_id, f"Слово <b>«{raw_text}»</b> не найдено в списке.", reply_markup=get_main_keyboard(mode))
                            continue

                        if raw_text == "/start":
                            welcome = (
                                f"👋 Здравствуйте, <b>{user_name}</b>!\n\n"
                                "✅ <b>Бот мгновенного мониторинга дорожной обстановки в Днепре готов к работе!</b>\n\n"
                                "💡 <b>Как это работает:</b>\n"
                                "• Бот непрерывно слушает каналы и комментарии Днепра.\n"
                                "• Если где-то стоит патруль или проверка — вы сразу получаете тревожное сообщение.\n"
                                "• Нажмите <b>«🛠 Диагностика»</b>, чтобы в любой момент проверить статус бота и последние перехваченные фразы!"
                            )
                            send_telegram_bot_message(user_id, welcome, reply_markup=get_main_keyboard(mode))

                        elif raw_text in ["🛠 Диагностика", "/debug", "/diag"]:
                            report = build_diagnostic_report(user_id)
                            send_telegram_bot_message(user_id, report, reply_markup=get_main_keyboard(mode))

                        elif raw_text == "➕ Добавить слово":
                            update_user(user_id, {"state": "wait_add"})
                            cancel_kb = {"keyboard": [[{"text": "Отмена"}]], "resize_keyboard": True}
                            send_telegram_bot_message(
                                user_id,
                                "✍️ <b>Напишите название улицы, моста или района:</b>\n"
                                "<i>(Например: Калиновая, Дамба, Малиновского, Южный мост, Победа, Рабочая)</i>",
                                reply_markup=cancel_kb
                            )

                        elif raw_text == "❌ Удалить слово":
                            if not keywords:
                                send_telegram_bot_message(user_id, "ℹ️ Ваш список локаций пока пуст.", reply_markup=get_main_keyboard(mode))
                            else:
                                update_user(user_id, {"state": "wait_del"})
                                kw_buttons = [[{"text": k}] for k in keywords]
                                kw_buttons.append([{"text": "Отмена"}])
                                del_kb = {"keyboard": kw_buttons, "resize_keyboard": True}
                                send_telegram_bot_message(
                                    user_id,
                                    "🗑 Выберите на клавиатуре слово для удаления:",
                                    reply_markup=del_kb
                                )

                        elif raw_text == "📍 Мои локации":
                            mode_desc = "🎯 <i>Только мои слова</i>" if mode == "only_keywords" else "🔔 <i>Все события города</i>"
                            if not keywords:
                                kw_list_text = "<i>(Список пуст. Вы получаете все важные события города)</i>"
                            else:
                                kw_list_text = "\n".join([f"• <b>{k}</b>" for k in keywords])

                            status_text = (
                                f"📍 <b>Ваши отслеживаемые локации:</b>\n\n"
                                f"{kw_list_text}\n\n"
                                f"⚙️ <b>Текущий режим:</b>\n{mode_desc}"
                            )
                            send_telegram_bot_message(user_id, status_text, reply_markup=get_main_keyboard(mode))

                        elif "Режим:" in raw_text:
                            new_mode = "all" if mode == "only_keywords" else "only_keywords"
                            update_user(user_id, {"mode": new_mode})
                            if new_mode == "only_keywords":
                                desc = "🎯 Включен режим <b>«Только мои слова»</b>. Вы будете получать тревоги ТОЛЬКО по вашим ключевым словам!"
                            else:
                                desc = "🔔 Включен режим <b>«Все события города»</b>. Теперь вы не пропустите ни один блокпост в Днепре!"
                            send_telegram_bot_message(user_id, desc, reply_markup=get_main_keyboard(new_mode))

                        elif raw_text == "📋 Последняя сводка":
                            events_list = load_events()
                            if not events_list:
                                reply = (
                                    "ℹ️ <b>Сейчас на дорогах Днепра спокойно!</b>\n\n"
                                    "За последнее время активных предупреждений не зафиксировано.\n"
                                    "Как только появится новая информация — бот сразу же пришлет вам уведомление!"
                                )
                            else:
                                lines = ["📋 <b>Актуальная сводка событий по Днепру (от свежих к старым):</b>\n"]
                                for ev in reversed(events_list[-10:]):
                                    lines.append(
                                        f"🕒 <b>{ev.get('time', '')}</b> — 📍 <b>{ev.get('location', '')}</b>\n"
                                        f"⚠️ <b>{ev.get('status', '')}:</b> {ev.get('summary', '')}\n"
                                        f"💬 <i>«{ev.get('raw_text', '')[:120]}»</i>\n"
                                    )
                                reply = "\n".join(lines)
                            send_telegram_bot_message(user_id, reply, reply_markup=get_main_keyboard(mode))

                        else:
                            print(f"[Q&A] Вопрос от {user_name}: {raw_text}")
                            events_list = load_events()
                            try:
                                requests.post(
                                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendChatAction",
                                    json={"chat_id": user_id, "action": "typing"},
                                    timeout=3
                                )
                            except Exception:
                                pass

                            ai_answer = await asyncio.to_thread(answer_user_question_with_cf_ai, raw_text, events_list)
                            send_telegram_bot_message(user_id, ai_answer, reply_markup=get_main_keyboard(mode))

        except Exception as e:
            await asyncio.sleep(3)
        await asyncio.sleep(1)

# ==========================================
# Главная функция запуска
# ==========================================
async def main():
    print("=" * 60)
    print("🚀 Запуск системы мониторинга Днепра")
    print(f"API_ID: {'Найден' if API_ID else 'ОШИБКА: 0'}, BOT_TOKEN: {'Найден' if BOT_TOKEN else 'ОШИБКА'}")
    print(f"🕒 Время сервера: {to_kyiv_time().strftime('%H:%M:%S')} (Kyiv UTC+3)")
    print("=" * 60)

    asyncio.create_task(keep_alive_self_ping())

    while True:
        try:
            if STRING_SESSION:
                print("☁️ Подключение через TELEGRAM_STRING_SESSION...")
                client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
            else:
                client = TelegramClient("user_session", API_ID, API_HASH)

            await client.start()
            break
        except Exception as e:
            print(f"[Telegram Start Error] {e}. Повтор через 5с...")
            await asyncio.sleep(5)

    me = await client.get_me()
    print(f"✅ Вход в Telegram выполнен успешно: {me.first_name} (@{me.username})")

    monitored_entities = {}
    sources_to_preload = []

    for username in CHANNELS_TO_MONITOR:
        try:
            entity = await client.get_entity(username)
            monitored_entities[entity.id] = {"name": f"@{username}", "type": "Канал"}
            sources_to_preload.append(entity)
            print(f"📡 Подключен канал: @{username} (ID: {entity.id})")

            # Вступаем и подключаем чаты комментариев
            try:
                full_ch = await client(GetFullChannelRequest(entity))
                linked_id = full_ch.full_chat.linked_chat_id
                if linked_id:
                    linked_entity = await client.get_entity(linked_id)
                    monitored_entities[linked_entity.id] = {
                        "name": f"@{username} (Комментарии)",
                        "type": "Комментарий"
                    }
                    sources_to_preload.append(linked_entity)
                    
                    # ОБЯЗАТЕЛЬНО ВСТУПАЕМ В ЧАТ КОММЕНТАРИЕВ ДЛЯ ПРИЕМА ЖИВЫХ СООБЩЕНИЙ!
                    try:
                        await client(JoinChannelRequest(linked_entity))
                        print(f"✅ Вступили в чат комментариев для @{username} (ID: {linked_entity.id})")
                    except Exception as je:
                        print(f"ℹ️ Статус участия в комментариях @{username}: {je}")
            except Exception as e:
                print(f"⚠️ Комментарии для @{username} не найдены: {e}")
        except Exception as e:
            print(f"❌ Ошибка подключения к @{username}: {e}")

    # Загрузка актуальной базы
    print("🔄 Наполнение базы из каналов и комментариев...")
    for src in sources_to_preload:
        try:
            async for m in client.iter_messages(src, limit=35):
                if m.text and len(m.text) > 4:
                    res = analyze_text_with_cf_ai(m.text)
                    if res.get("relevant") is True:
                        t_str = to_kyiv_time(m.date).strftime("%H:%M")
                        save_event({
                            "time": t_str,
                            "location": res.get("location", "Днепр"),
                            "status": res.get("status", "Внимание"),
                            "summary": res.get("summary", m.text[:100]),
                            "raw_text": m.text.strip(),
                            "source": getattr(src, "username", "Чат")
                        })
        except Exception:
            pass

    target_chat_ids = list(monitored_entities.keys())
    print(f"🎯 Всего активных источников (каналы + комментарии): {len(target_chat_ids)}")
    print("=" * 60)

    asyncio.create_task(bot_polling_loop())

    @client.on(events.NewMessage(chats=target_chat_ids))
    async def incoming_handler(event):
        text = event.raw_text
        if not text:
            return

        chat_id = event.chat_id
        source_info = monitored_entities.get(chat_id, {"name": "Чат", "type": "Сообщение"})
        
        # Точное время отправки сообщения по Киеву/Днепру
        msg_time = to_kyiv_time(event.message.date if event.message and event.message.date else None)
        time_str = msg_time.strftime("%H:%M")

        print(f"\n⚡ [{time_str}] [Перехват] {source_info['name']}: {text[:70]}...")

        # Анализ (ИИ + резервные правила)
        analysis = analyze_text_with_cf_ai(text)
        
        if analysis.get("relevant") is True:
            location = analysis.get("location", "Днепр")
            status = analysis.get("status", "Внимание")
            summary = analysis.get("summary", text[:100])

            alert_key = f"{location}_{status}".lower()
            if is_duplicate_alert(alert_key, ttl_minutes=15):
                print(f"⏩ Пропуск дубликата (в пределах 15 мин): {alert_key}")
                audit_log.append({
                    "time": time_str,
                    "source": source_info['name'],
                    "text": text[:60],
                    "decision": f"⏩ Дубликат (уже было за последние 15 мин: {location})"
                })
                return

            save_event({
                "time": time_str,
                "location": location,
                "status": status,
                "summary": summary,
                "raw_text": text.strip(),
                "source": source_info['name']
            })

            searchable_text = f"{location} {summary} {text}".lower()
            all_users = load_all_users()
            print(f"🔔 Рассылка по {len(all_users)} пользователям...")

            sent_count = 0
            filtered_count = 0

            for uid_str, udata in all_users.items():
                uid = int(uid_str)
                u_mode = udata.get("mode", "all")
                u_keywords = udata.get("keywords", [])

                matched_kw = None
                for kw in u_keywords:
                    if kw.lower() in searchable_text:
                        matched_kw = kw
                        break

                should_send = False
                if u_mode == "all":
                    should_send = True
                elif u_mode == "only_keywords":
                    if not u_keywords:
                        should_send = True
                    elif matched_kw:
                        should_send = True

                if should_send:
                    match_header = f"🎯 <b>По вашему фильтру: «{matched_kw}»</b>\n\n" if matched_kw else ""
                    alert_message = (
                        f"🚨 <b>Внимание: дорожная обстановка</b>\n"
                        f"{match_header}"
                        f"📍 <b>Локация:</b> {location}\n"
                        f"⚠️ <b>Статус:</b> {status}\n"
                        f"📝 <b>Суть:</b> {summary}\n\n"
                        f"💬 <i>«{text.strip()}»</i>\n\n"
                        f"🔗 <b>Источник:</b> {source_info['name']}\n"
                        f"🕒 <b>Время:</b> {time_str}"
                    )
                    send_telegram_bot_message(uid, alert_message, reply_markup=get_main_keyboard(u_mode))
                    sent_count += 1
                else:
                    filtered_count += 1

            # Запись в аудит-лог
            if sent_count > 0:
                audit_log.append({
                    "time": time_str,
                    "source": source_info['name'],
                    "text": text[:60],
                    "decision": f"✅ Опасность отправлена! ({location} - {status})"
                })
            else:
                audit_log.append({
                    "time": time_str,
                    "source": source_info['name'],
                    "text": text[:60],
                    "decision": f"⚠️ Зафиксировано ({location}), но отфильтровано вашим списком ключевых слов"
                })

            if len(audit_log) > 25:
                audit_log.pop(0)

        else:
            audit_log.append({
                "time": time_str,
                "source": source_info['name'],
                "text": text[:60],
                "decision": f"⚪ Отсеяно ({analysis.get('reason', 'неактуально/вопрос')})"
            })
            if len(audit_log) > 25:
                audit_log.pop(0)

    print("\n👂 Бот активен в реальном времени с защитой от засыпания!")
    
    while True:
        try:
            await client.run_until_disconnected()
        except Exception as e:
            print(f"[Telethon Socket Drop] {e}. Переподключение через 5с...")
            await asyncio.sleep(5)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nОстановка бота.")
