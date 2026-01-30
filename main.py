import os
import sqlite3
from datetime import datetime

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

from openai import OpenAI

# ---------- CONFIG ----------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN env var")
if not OPENAI_API_KEY:
    raise RuntimeError("Missing OPENAI_API_KEY env var")

client = OpenAI(api_key=OPENAI_API_KEY)

DB_PATH = "/var/data/memory.sqlite"
SHORT_HISTORY_LIMIT = 20  # последних сообщений на пользователя

SYSTEM_PROMPT = """
Ты — «Душа», тёплый, бережный, мудрый проводник и наставница.
Тон: спокойный, поддерживающий, человечный, без пафоса, без морализаторства.
Стиль:
- отвечай по делу, но с теплом;
- если вопрос простой — отвечай коротко;
- если человек переживает — сначала поддержи, потом предложи 1-2 шага;
- НЕ дави, НЕ командуй, НЕ ставь диагнозов;
- избегай "я всего лишь ИИ" — говори естественно;
- можно использовать эмодзи умеренно (1–3), если это уместно.
Память:
- учитывай «долгую память» (facts), но не вываливай её списком;
- если не уверен(а) — уточни мягко.
Безопасность:
- не проси и не повторяй секретные ключи, пароли, токены.
"""

# ---------- DB ----------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS facts (
        user_id INTEGER PRIMARY KEY,
        facts TEXT DEFAULT ''
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        role TEXT,
        content TEXT,
        ts TEXT
    )
    """)

    conn.commit()
    conn.close()

def get_facts(user_id: int) -> str:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT facts FROM facts WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else ""

def set_facts(user_id: int, facts: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO facts(user_id, facts) VALUES(?, ?)
    ON CONFLICT(user_id) DO UPDATE SET facts=excluded.facts
    """, (user_id, facts))
    conn.commit()
    conn.close()

def add_message(user_id: int, role: str, content: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO messages(user_id, role, content, ts) VALUES(?,?,?,?)",
        (user_id, role, content, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()

def get_recent_messages(user_id: int, limit: int = SHORT_HISTORY_LIMIT):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
    SELECT role, content FROM messages
    WHERE user_id=?
    ORDER BY id DESC
    LIMIT ?
    """, (user_id, limit))
    rows = cur.fetchall()
    conn.close()
    # возвращаем в правильном порядке
    return [{"role": r, "content": c} for (r, c) in reversed(rows)]

def trim_history(user_id: int, keep: int = SHORT_HISTORY_LIMIT):
    """Оставляем только последние keep сообщений, чтобы база не раздувалась."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
    DELETE FROM messages
    WHERE user_id=? AND id NOT IN (
        SELECT id FROM messages
        WHERE user_id=?
        ORDER BY id DESC
        LIMIT ?
    )
    """, (user_id, user_id, keep))
    conn.commit()
    conn.close()

# ---------- AI ----------
def build_messages(user_id: int, user_text: str):
    facts = get_facts(user_id).strip()

    memory_block = ""
    if facts:
        memory_block = f"\n\nДолгая память (facts) о пользователе:\n{facts}\n"

    msgs = [{"role": "system", "content": SYSTEM_PROMPT + memory_block}]
    msgs += get_recent_messages(user_id, SHORT_HISTORY_LIMIT)
    msgs += [{"role": "user", "content": user_text}]
    return msgs

def ask_ai(user_id: int, user_text: str) -> str:
    messages = build_messages(user_id, user_text)

    resp = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=messages,
        temperature=0.7,
    )
    return resp.choices[0].message.content.strip()

def update_facts_with_ai(user_id: int, user_text: str, assistant_text: str):
    """
    Мягкое обновление фактов: извлекаем только устойчивые предпочтения/данные.
    Запускается НЕ каждый раз (см. ниже), чтобы экономить.
    """
    current_facts = get_facts(user_id)

    extractor_prompt = f"""
Ты извлекатель фактов для долгой памяти.
Твоя задача: обновить "facts" о пользователе кратко и полезно.
Правила:
- добавляй только устойчивые вещи: имя, предпочтения, цели, важные долгосрочные проекты;
- НЕ добавляй секреты (ключи, пароли), номера карт, точные адреса;
- НЕ добавляй одноразовые мелочи.
Формат: короткие пункты, 1 строка = 1 факт.

Текущие facts:
{current_facts}

Новые сообщения:
Пользователь: {user_text}
Ассистент: {assistant_text}

Верни обновлённый список facts:
"""

    resp = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": "Ты аккуратный извлекатель фактов."},
            {"role": "user", "content": extractor_prompt},
        ],
        temperature=0.2,
    )
    new_facts = resp.choices[0].message.content.strip()
    set_facts(user_id, new_facts)

# ---------- Telegram handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    add_message(user_id, "assistant", "Привет, душа моя 🤍 Я здесь. Хочешь поговорить?")
    await update.message.reply_text("Привет, душа моя 🤍 Я здесь. Хочешь поговорить?")

async def remember(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /remember текст
    вручную добавляет факт
    """
    user_id = update.effective_user.id
    text = update.message.text.replace("/remember", "", 1).strip()
    if not text:
        await update.message.reply_text("Напиши после /remember что именно запомнить 🙏")
        return

    current = get_facts(user_id).strip()
    updated = (current + "\n" + text).strip() if current else text
    set_facts(user_id, updated)
    await update.message.reply_text("Запомнила 🤍")

async def memory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    facts = get_facts(user_id).strip()
    await update.message.reply_text(f"Вот что я о тебе помню:\n\n{facts if facts else 'Пока пусто 🤍'}")

async def clear_memory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    set_facts(user_id, "")
    await update.message.reply_text("Очистила долгую память 🤍")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_text = update.message.text.strip()

    # сохраняем сообщение пользователя
    add_message(user_id, "user", user_text)

    # получаем ответ
    assistant_text = ask_ai(user_id, user_text)

    # сохраняем ответ ассистента
    add_message(user_id, "assistant", assistant_text)

    # подчищаем историю
    trim_history(user_id, SHORT_HISTORY_LIMIT)

    # обновлять факты не каждый раз: например, 1 раз в 6 сообщений
    # (чтобы экономить и не грузить)
    count = len(get_recent_messages(user_id, SHORT_HISTORY_LIMIT))
    if count % 6 == 0:
        try:
            update_facts_with_ai(user_id, user_text, assistant_text)
        except Exception:
            pass  # не падаем, если extractor не сработал

    await update.message.reply_text(assistant_text)

def main():
    init_db()
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("remember", remember))
    app.add_handler(CommandHandler("memory", memory))
    app.add_handler(CommandHandler("clear_memory", clear_memory))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.run_polling()

if __name__ == "__main__":
    main()
