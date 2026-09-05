import os
import sys
import asyncio
import json
import datetime
import requests
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.functions.channels import GetFullChannelRequest
from ai_filter import analyze_text_with_cf_ai, answer_user_question_with_cf_ai

load_dotenv()

API_ID = int(os.getenv("TELEGRAM_API_ID"))
API_HASH = os.getenv("TELEGRAM_API_HASH")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
STRING_SESSION = os.getenv("TELEGRAM_STRING_SESSION")

SETTINGS_FILE = "user_settings.json"
EVENTS_FILE = "events_history.json"
CHANNELS_TO_MONITOR = ["pridybai", "dnepr_bez_tck", "agendaDnepr"]

recent_alerts = []

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
            "mode": "only_keywords",
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

def get_main_keyboard(mode: str = "only_keywords"):
    mode_text = "🎯 Режим: Только мои слова" if mode == "only_keywords" else "🔔 Режим: Все события"
    return {
        "keyboard": [
            [{"text": "📋 Последняя сводка"}, {"text": "📍 Мои локации"}],
            [{"text": "➕ Добавить слово"}, {"text": "❌ Удалить слово"}],
            [{"text": mode_text}, {"text": "❓ Как пользоваться"}]
        ],
        "resize_keyboard": True
    }

# ==========================================
# Легковесный Web сервер для облачных платформ (Health Check)
# ==========================================
async def start_health_check_server():
    port = int(os.getenv("PORT", 8080))
    async def handle_client(reader, writer):
        try:
            await reader.read(512)
            content = "OK: Dnepr Road Bot is actively monitoring"
            response = (
                f"HTTP/1.1 200 OK\r\n"
                f"Content-Type: text/plain; charset=utf-8\r\n"
                f"Content-Length: {len(content.encode('utf-8'))}\r\n"
                f"Connection: close\r\n\r\n"
                f"{content}"
            )
            writer.write(response.encode("utf-8"))
            await writer.drain()
            writer.close()
        except Exception:
            pass

    server = await asyncio.start_server(handle_client, "0.0.0.0", port)
    print(f"[Cloud Health-Check] Сервер активен на порту {port}")
    await server.serve_forever()

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
                        mode = user.get("mode", "only_keywords")

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
                                    f"Теперь бот будет мгновенно отслеживать любые проверки и блокпосты с этим словом."
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
                                "💡 <b>Как настроить бот под себя:</b>\n"
                                "• Нажмите <b>«➕ Добавить слово»</b>, чтобы вписать свои улицы (например: <i>Калиновая</i>, <i>Правда</i>, <i>Новый мост</i>).\n"
                                "• Бот моментально присылает предупреждения по вашим локациям!\n"
                                "• Вы также можете спросить: <i>«Что на Победе?»</i>."
                            )
                            send_telegram_bot_message(user_id, welcome, reply_markup=get_main_keyboard(mode))

                        elif raw_text == "➕ Добавить слово":
                            update_user(user_id, {"state": "wait_add"})
                            cancel_kb = {"keyboard": [[{"text": "Отмена"}]], "resize_keyboard": True}
                            send_telegram_bot_message(
                                user_id,
                                "✍️ <b>Напишите название улицы, моста или района:</b>\n"
                                "<i>(Например: Калиновая, Победа, Новый мост, Рабочая, Левый берег)</i>",
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
                            mode_desc = "🎯 <i>Только мои слова (лишний шум отсекается)</i>" if mode == "only_keywords" else "🔔 <i>Все события по городу</i>"
                            if not keywords:
                                kw_list_text = "<i>(Список пуст. Добавьте улицы кнопкой «➕ Добавить слово»)</i>"
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
                                desc = "🎯 Включен режим <b>«Только мои слова»</b>. Бот присылает сообщения только если упомянуты ваши локации!"
                            else:
                                desc = "🔔 Включен режим <b>«Все события»</b>. Присылаются все подтвержденные отчеты со всего города."
                            send_telegram_bot_message(user_id, desc, reply_markup=get_main_keyboard(new_mode))

                        elif raw_text == "📋 Последняя сводка":
                            events_list = load_events()
                            if not events_list:
                                reply = "ℹ️ За последнее время активных предупреждений не зафиксировано."
                            else:
                                lines = ["📋 <b>Последние зафиксированные события в Днепре:</b>\n"]
                                for ev in reversed(events_list[-8:]):
                                    lines.append(
                                        f"🕒 <b>{ev['time']}</b> — 📍 <b>{ev['location']}</b>\n"
                                        f"⚠️ {ev['status']}: {ev['summary']}\n"
                                    )
                                reply = "\n".join(lines)
                            send_telegram_bot_message(user_id, reply, reply_markup=get_main_keyboard(mode))

                        elif raw_text == "❓ Как пользоваться":
                            help_msg = (
                                "📖 <b>Инструкция:</b>\n\n"
                                "1. <b>Ключевые слова:</b> Нажмите <i>«➕ Добавить слово»</i> и введите улицы своего маршрута.\n"
                                "2. <b>Режимы:</b>\n"
                                "   • <i>Только мои слова</i> — оповещения только при совпадении с вашим маршрутом.\n"
                                "   • <i>Все события</i> — уведомления по всему Днепру.\n"
                                "3. <b>Вопросы ИИ:</b> Напишите в чате: <i>«Что на Победе?»</i> — нейросеть Cloudflare ответит вам!"
                            )
                            send_telegram_bot_message(user_id, help_msg, reply_markup=get_main_keyboard(mode))

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
            await asyncio.sleep(4)
        await asyncio.sleep(1)

# ==========================================
# Главная функция запуска
# ==========================================
async def main():
    print("=" * 60)
    print("🚀 Запуск системы мониторинга (Мгновенные оповещения)")
    print("=" * 60)

    # Инициализация сессии: либо из переменной окружения (облако), либо из файла (ПК)
    if STRING_SESSION:
        print("☁️ Обнаружена сессия облака (TELEGRAM_STRING_SESSION)!")
        client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
    else:
        client = TelegramClient("user_session", API_ID, API_HASH)

    await client.start()

    me = await client.get_me()
    print(f"✅ Вход в Telegram выполнен успешно: {me.first_name} (@{me.username})")

    monitored_entities = {}
    
    for username in CHANNELS_TO_MONITOR:
        try:
            entity = await client.get_entity(username)
            monitored_entities[entity.id] = {"name": f"@{username}", "type": "Канал"}
            print(f"📡 Подключен канал: @{username} (ID: {entity.id})")

            if username == "agendaDnepr":
                try:
                    full_ch = await client(GetFullChannelRequest(entity))
                    linked_id = full_ch.full_chat.linked_chat_id
                    if linked_id:
                        linked_entity = await client.get_entity(linked_id)
                        monitored_entities[linked_entity.id] = {
                            "name": f"@{username} (Комментарии)",
                            "type": "Комментарий"
                        }
                        print(f"💬 Подключен чат комментариев для @{username} (ID: {linked_entity.id})")
                except Exception as e:
                    print(f"⚠️ Ошибка подключения комментариев для @{username}: {e}")
        except Exception as e:
            print(f"❌ Ошибка подключения к @{username}: {e}")

    # Первоначальное наполнение базы последними событиями
    print("🔄 Наполнение актуальной базы из каналов...")
    for username in CHANNELS_TO_MONITOR:
        try:
            async for m in client.iter_messages(username, limit=10):
                if m.text and len(m.text) > 6:
                    res = analyze_text_with_cf_ai(m.text)
                    if res.get("relevant") is True:
                        t_str = m.date.strftime("%H:%M") if m.date else datetime.datetime.now().strftime("%H:%M")
                        save_event({
                            "time": t_str,
                            "location": res.get("location", "Днепр"),
                            "status": res.get("status", "Внимание"),
                            "summary": res.get("summary", m.text[:100]),
                            "raw_text": m.text.strip(),
                            "source": f"@{username}"
                        })
        except Exception:
            pass

    target_chat_ids = list(monitored_entities.keys())
    print(f"🎯 Всего активных источников: {len(target_chat_ids)}")
    print("=" * 60)

    # Запускаем задачи: опрос диалогов и веб-сервер проверки здоровья для облака
    asyncio.create_task(bot_polling_loop())
    asyncio.create_task(start_health_check_server())

    # Обработчик новых сообщений: срабатывает МГНОВЕННО (в пределах 1 секунды)
    @client.on(events.NewMessage(chats=target_chat_ids))
    async def incoming_handler(event):
        text = event.raw_text
        if not text:
            return

        chat_id = event.chat_id
        source_info = monitored_entities.get(chat_id, {"name": "Чат", "type": "Сообщение"})
        
        print(f"\n⚡ [Мгновенный перехват] {source_info['name']}: {text[:70]}...")

        # Анализ нейросетью Cloudflare AI
        analysis = analyze_text_with_cf_ai(text)
        
        if analysis.get("relevant") is True:
            location = analysis.get("location", "Днепр")
            status = analysis.get("status", "Внимание")
            summary = analysis.get("summary", text[:100])

            alert_key = f"{location}_{status}".lower()
            now = datetime.datetime.now()
            if alert_key in recent_alerts:
                print(f"⏩ Пропуск дубликата: {alert_key}")
                return
            
            recent_alerts.append(alert_key)
            if len(recent_alerts) > 50:
                recent_alerts.pop(0)

            time_str = now.strftime("%H:%M")

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
            print(f"🔔 Мгновенная рассылка по {len(all_users)} пользователям...")

            for uid_str, udata in all_users.items():
                uid = int(uid_str)
                u_mode = udata.get("mode", "only_keywords")
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
        else:
            print("⚪ Отсеяно ИИ")

    print("\n👂 Бот активен в реальном времени! Готов к развертыванию в облаке.")
    await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nОстановка бота.")
