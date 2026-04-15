import os
import json
import sqlite3
import tempfile
from decimal import Decimal, InvalidOperation
from datetime import datetime

from dotenv import load_dotenv
from openai import OpenAI
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not TOKEN:
    raise ValueError("Не знайдено TELEGRAM_BOT_TOKEN у файлі .env")

if not OPENAI_API_KEY:
    raise ValueError("Не знайдено OPENAI_API_KEY у файлі .env")

client = OpenAI(api_key=OPENAI_API_KEY)

DB_NAME = "budget.db"


def get_conn():
    return sqlite3.connect(DB_NAME)


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS expenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        amount_cents INTEGER NOT NULL,
        category TEXT NOT NULL,
        description TEXT,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS budgets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        amount_cents INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS category_memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        keyword TEXT NOT NULL,
        category TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(user_id, keyword)
    )
    """)

    conn.commit()
    conn.close()


def eur_to_cents(value: str) -> int:
    value = value.replace(",", ".").strip()
    amount = Decimal(value)
    return int(amount * 100)


def cents_to_eur(cents: int) -> str:
    euros = Decimal(cents) / Decimal(100)
    return f"{euros:.2f} €"


def add_expense(user_id: int, amount_cents: int, category: str, description: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO expenses (user_id, amount_cents, category, description, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (user_id, amount_cents, category, description, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()


def update_last_expense_category(user_id: int, new_category: str) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM expenses WHERE user_id = ? ORDER BY id DESC LIMIT 1",
        (user_id,)
    )
    row = cur.fetchone()

    if not row:
        conn.close()
        return False

    expense_id = row[0]
    cur.execute(
        "UPDATE expenses SET category = ? WHERE id = ?",
        (new_category, expense_id)
    )
    conn.commit()
    conn.close()
    return True


def get_last_expense(user_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, amount_cents, category, description, created_at
        FROM expenses
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT 1
    """, (user_id,))
    row = cur.fetchone()
    conn.close()
    return row


def set_budget(user_id: int, amount_cents: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO budgets (user_id, amount_cents, created_at)
        VALUES (?, ?, ?)
        """,
        (user_id, amount_cents, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()


def get_current_budget(user_id: int) -> int:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT amount_cents FROM budgets WHERE user_id = ? ORDER BY id DESC LIMIT 1",
        (user_id,)
    )
    row = cur.fetchone()
    conn.close()
    return row[0] if row else 0


def get_total_expenses(user_id: int) -> int:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) FROM expenses WHERE user_id = ?",
        (user_id,)
    )
    row = cur.fetchone()
    conn.close()
    return row[0] if row else 0


def get_remaining_budget(user_id: int) -> int:
    return get_current_budget(user_id) - get_total_expenses(user_id)


def save_category_memory(user_id: int, keyword: str, category: str):
    keyword = keyword.strip().lower()
    category = category.strip().lower()

    if not keyword:
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO category_memory (user_id, keyword, category, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id, keyword) DO UPDATE SET
            category = excluded.category,
            updated_at = excluded.updated_at
    """, (user_id, keyword, category, datetime.now().isoformat()))
    conn.commit()
    conn.close()


def get_memory_matches(user_id: int, text: str):
    text_lower = text.lower()

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT keyword, category FROM category_memory WHERE user_id = ?",
        (user_id,)
    )
    rows = cur.fetchall()
    conn.close()

    matches = []
    for keyword, category in rows:
        if keyword in text_lower:
            matches.append((keyword, category))

    return matches


def normalize_category(raw):
    if not raw:
        return "other"

    value = raw.strip().lower()

    mapping = {
        "food": "food",
        "їжа": "food",
        "еда": "food",
        "transport": "transport",
        "транспорт": "transport",
        "shopping": "shopping",
        "покупки": "shopping",
        "шопінг": "shopping",
        "health": "health",
        "здоров'я": "health",
        "здоровье": "health",
        "entertainment": "entertainment",
        "розваги": "entertainment",
        "развлечения": "entertainment",
        "home": "home",
        "дім": "home",
        "дом": "home",
        "other": "other",
        "інше": "other",
        "другое": "other",
    }

    return mapping.get(value, value)


def analyze_message_with_ai(user_text: str, memory_hint: str) -> dict:
    schema = {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "enum": [
                    "add_expense",
                    "set_budget",
                    "check_purchase",
                    "correct_last_category",
                    "teach_category",
                    "get_budget",
                    "get_remaining",
                    "get_spent",
                    "general_chat",
                    "unknown"
                ]
            },
            "amount": {
                "type": ["number", "null"]
            },
            "category": {
                "type": ["string", "null"]
            },
            "description": {
                "type": ["string", "null"]
            },
            "keyword": {
                "type": ["string", "null"]
            },
            "reply_text": {
                "type": "string"
            }
        },
        "required": ["intent", "amount", "category", "description", "keyword", "reply_text"],
        "additionalProperties": False
    }

    prompt = f"""
Ти розумний фінансовий Telegram-асистент.

Потрібно визначити намір користувача і повернути лише JSON за схемою.

Дозволені intent:
- add_expense
- set_budget
- check_purchase
- correct_last_category
- teach_category
- get_budget
- get_remaining
- get_spent
- general_chat
- unknown

Правила:
- Розумій українську, російську, англійську, естонську.
- "який у мене бюджет" => get_budget
- "скільки лишилось", "що залишилось від бюджету" => get_remaining
- "скільки я витратив" => get_spent
- "це їжа", "ні це транспорт" => correct_last_category
- "кава це їжа", "bolt це транспорт" => teach_category
- category використовуй одну з:
  food, transport, shopping, health, entertainment, home, other
- amount = null, якщо суми немає
- keyword тільки для teach_category
- reply_text короткою природною українською

Пам'ять:
{memory_hint}

Повідомлення користувача:
{user_text}
""".strip()

    response = client.responses.create(
        model="gpt-4o-mini",
        input=prompt,
        text={
            "format": {
                "type": "json_schema",
                "name": "finance_intent_multiuser",
                "strict": True,
                "schema": schema
            }
        }
    )

    return json.loads(response.output_text)


def transcribe_audio_file(file_path: str) -> str:
    with open(file_path, "rb") as audio_file:
        transcription = client.audio.transcriptions.create(
            model="gpt-4o-mini-transcribe",
            file=audio_file
        )
    return transcription.text.strip()


async def process_finance_text(update: Update, user_text: str):
    user_id = update.effective_user.id

    memory_matches = get_memory_matches(user_id, user_text)
    if memory_matches:
        memory_hint = "Відомі відповідності: " + ", ".join(
            [f"{k} -> {v}" for k, v in memory_matches]
        )
    else:
        memory_hint = "Наразі точних збігів у пам'яті не знайдено."

    data = analyze_message_with_ai(user_text, memory_hint)

    intent = data.get("intent")
    amount = data.get("amount")
    category = normalize_category(data.get("category"))
    description = (data.get("description") or user_text).strip()
    keyword = (data.get("keyword") or "").strip().lower()
    reply_text = data.get("reply_text") or "Готово."

    if intent == "teach_category":
        if not keyword or category == "other":
            await update.message.reply_text("Я зрозумів, що ти хочеш мене навчити, але не вистачає даних.")
            return

        save_category_memory(user_id, keyword, category)
        await update.message.reply_text(
            f"{reply_text}\nЗапам'ятав: {keyword} → {category}"
        )
        return

    if intent == "correct_last_category":
        if category == "other":
            await update.message.reply_text("Я зрозумів, що ти виправляєш категорію, але не зміг визначити нову.")
            return

        ok = update_last_expense_category(user_id, category)
        if not ok:
            await update.message.reply_text("У тебе ще немає останньої витрати, яку можна виправити.")
            return

        last_expense = get_last_expense(user_id)
        if last_expense:
            _, _, _, last_description, _ = last_expense
            if last_description:
                save_category_memory(user_id, last_description, category)

        await update.message.reply_text(
            f"{reply_text}\nОстанню витрату оновлено на категорію: {category}"
        )
        return

    if intent == "add_expense":
        if amount is None:
            await update.message.reply_text("Я зрозумів, що це витрата, але не бачу суму.")
            return

        if memory_matches:
            category = memory_matches[0][1]

        amount_cents = int(Decimal(str(amount)) * 100)
        add_expense(user_id, amount_cents, category, description)

        await update.message.reply_text(
            f"{reply_text}\n"
            f"Сума: {cents_to_eur(amount_cents)}\n"
            f"Категорія: {category}\n"
            f"Опис: {description}"
        )
        return

    if intent == "set_budget":
        if amount is None:
            await update.message.reply_text("Я зрозумів, що ти хочеш встановити бюджет, але не бачу суму.")
            return

        amount_cents = int(Decimal(str(amount)) * 100)
        set_budget(user_id, amount_cents)

        await update.message.reply_text(
            f"{reply_text}\nНовий бюджет: {cents_to_eur(amount_cents)}"
        )
        return

    if intent == "check_purchase":
        if amount is None:
            await update.message.reply_text("Я зрозумів, що ти хочеш перевірити покупку, але не бачу суму.")
            return

        purchase_cents = int(Decimal(str(amount)) * 100)
        left = get_remaining_budget(user_id)
        after_purchase = left - purchase_cents

        if purchase_cents <= left:
            await update.message.reply_text(
                f"{reply_text}\n"
                f"Зараз залишок: {cents_to_eur(left)}\n"
                f"Після покупки залишиться: {cents_to_eur(after_purchase)}"
            )
        else:
            need_more = purchase_cents - left
            await update.message.reply_text(
                f"{reply_text}\n"
                f"Зараз залишок: {cents_to_eur(left)}\n"
                f"Не вистачає: {cents_to_eur(need_more)}"
            )
        return

    if intent == "get_budget":
        budget = get_current_budget(user_id)
        await update.message.reply_text(
            f"{reply_text}\nПоточний бюджет: {cents_to_eur(budget)}"
        )
        return

    if intent == "get_remaining":
        left = get_remaining_budget(user_id)
        budget = get_current_budget(user_id)
        spent = get_total_expenses(user_id)
        await update.message.reply_text(
            f"{reply_text}\n"
            f"Бюджет: {cents_to_eur(budget)}\n"
            f"Витрачено: {cents_to_eur(spent)}\n"
            f"Залишилось: {cents_to_eur(left)}"
        )
        return

    if intent == "get_spent":
        spent = get_total_expenses(user_id)
        await update.message.reply_text(
            f"{reply_text}\nУсього витрачено: {cents_to_eur(spent)}"
        )
        return

    if intent == "general_chat":
        await update.message.reply_text(reply_text)
        return

    await update.message.reply_text(reply_text)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привіт. Я багатокористувацький бот для бюджету.\n\n"
        "У кожного тут свої окремі дані.\n"
        "Можеш писати:\n"
        "• бюджет 1000\n"
        "• кава 4\n"
        "• скільки я витратив\n"
        "• скільки лишилось\n"
        "• це їжа\n"
        "• bolt це транспорт"
    )


async def budget_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not context.args:
        await update.message.reply_text("Напиши так: /budget 1200")
        return

    try:
        amount_cents = eur_to_cents(context.args[0])
        set_budget(user_id, amount_cents)
        await update.message.reply_text(f"Бюджет встановлено: {cents_to_eur(amount_cents)}")
    except (InvalidOperation, ValueError):
        await update.message.reply_text("Не зміг зрозуміти суму. Приклад: /budget 1200")


async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if len(context.args) < 2:
        await update.message.reply_text("Напиши так: /add 12.50 food кава")
        return

    try:
        amount_cents = eur_to_cents(context.args[0])
        category = normalize_category(context.args[1])
        description = " ".join(context.args[2:]) if len(context.args) > 2 else ""

        add_expense(user_id, amount_cents, category, description)

        await update.message.reply_text(
            f"Записав витрату.\n"
            f"Сума: {cents_to_eur(amount_cents)}\n"
            f"Категорія: {category}\n"
            f"Опис: {description if description else '-'}"
        )
    except (InvalidOperation, ValueError):
        await update.message.reply_text("Не зміг зрозуміти суму. Приклад: /add 12.50 food кава")


async def spent_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    total = get_total_expenses(user_id)
    await update.message.reply_text(f"Усього витрачено: {cents_to_eur(total)}")


async def left_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    budget = get_current_budget(user_id)
    spent = get_total_expenses(user_id)
    left = budget - spent

    await update.message.reply_text(
        f"Бюджет: {cents_to_eur(budget)}\n"
        f"Витрачено: {cents_to_eur(spent)}\n"
        f"Залишилось: {cents_to_eur(left)}"
    )


async def can_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not context.args:
        await update.message.reply_text("Напиши так: /can 80")
        return

    try:
        purchase_cents = eur_to_cents(context.args[0])
        budget = get_current_budget(user_id)
        spent = get_total_expenses(user_id)
        left = budget - spent
        after_purchase = left - purchase_cents

        if purchase_cents <= left:
            await update.message.reply_text(
                f"Так, влізаєш.\n"
                f"Зараз залишок: {cents_to_eur(left)}\n"
                f"Після покупки залишиться: {cents_to_eur(after_purchase)}"
            )
        else:
            over = purchase_cents - left
            await update.message.reply_text(
                f"Ні, не влізаєш у бюджет.\n"
                f"Зараз залишок: {cents_to_eur(left)}\n"
                f"Не вистачає: {cents_to_eur(over)}"
            )
    except (InvalidOperation, ValueError):
        await update.message.reply_text("Не зміг зрозуміти суму. Приклад: /can 80")


async def smart_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_text = update.message.text.strip()
        await process_finance_text(update, user_text)
    except Exception as e:
        print("TEXT AI error:", e)
        await update.message.reply_text(
            "Сталася помилка при обробці тексту."
        )


async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    temp_path = None

    try:
        await update.message.reply_text("Обробляю голосове...")

        voice = update.message.voice or update.message.audio
        if not voice:
            await update.message.reply_text("Не бачу аудіо в повідомленні.")
            return

        telegram_file = await voice.get_file()

        suffix = ".ogg"
        mime_type = getattr(voice, "mime_type", None)
        if mime_type:
            mime = mime_type.lower()
            if "mpeg" in mime or "mp3" in mime:
                suffix = ".mp3"
            elif "wav" in mime:
                suffix = ".wav"
            elif "m4a" in mime or "mp4" in mime:
                suffix = ".m4a"
            elif "webm" in mime:
                suffix = ".webm"
            elif "ogg" in mime:
                suffix = ".ogg"

        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            temp_path = tmp.name

        await telegram_file.download_to_drive(temp_path)

        transcript = transcribe_audio_file(temp_path)

        if not transcript:
            await update.message.reply_text("Не вдалося розпізнати голосове повідомлення.")
            return

        await update.message.reply_text(f"Розпізнав так:\n{transcript}")
        await process_finance_text(update, transcript)

    except Exception as e:
        print("VOICE error:", e)
        await update.message.reply_text(
            "Сталася помилка при обробці голосового."
        )
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass


def main():
    init_db()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("budget", budget_command))
    app.add_handler(CommandHandler("add", add_command))
    app.add_handler(CommandHandler("spent", spent_command))
    app.add_handler(CommandHandler("left", left_command))
    app.add_handler(CommandHandler("can", can_command))

    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, voice_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, smart_text_handler))

    print("AI multi-user бот запущений...")
    app.run_polling()


if __name__ == "__main__":
    main()