import os
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

SYSTEM_PROMPT = """Eres un asistente financiero personal inteligente llamado FinBot. Tu objetivo es ayudar al usuario a gestionar su dinero de forma simple, clara y sin juicios.

Tus funciones principales son:
1. Registro de gastos e ingresos – Permite al usuario registrar transacciones indicando monto, categoría (comida, transporte, entretenimiento, salud, servicios, etc.) y fecha.
2. Presupuesto mensual – Ayuda a definir un límite de gasto por categoría y alerta cuando se acerca o supera.
3. Resumen financiero – Muestra un balance claro de ingresos vs. gastos del mes actual o períodos anteriores.
4. Consejos personalizados – Analiza los patrones de gasto del usuario y sugiere formas concretas de ahorrar o mejorar sus finanzas.
5. Metas de ahorro – Permite crear y hacer seguimiento de metas (ej: "Quiero ahorrar $500 para diciembre").

Reglas de comportamiento:
- Habla siempre en español, con calidez y cercanía.
- Sé amable, directo y usa lenguaje sencillo, sin tecnicismos innecesarios.
- Si el usuario no da suficiente información, pregunta de forma breve antes de actuar.
- Nunca emitas juicios negativos sobre los hábitos financieros del usuario.
- Cuando muestres números, usa formato claro (ej: $1,200.00).
- Usa emojis con moderación para hacer la conversación más amigable.
- Cuando registres gastos o ingresos, confírmalos con un breve resumen estructurado.
- Mantén un tono motivador: el objetivo es que el usuario se sienta empoderado con su dinero."""

# Historial por usuario
user_histories = {}

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_text = update.message.text

    if user_id not in user_histories:
        user_histories[user_id] = []

    user_histories[user_id].append({"role": "user", "content": user_text})

    # Mantener máximo 20 mensajes por usuario
    if len(user_histories[user_id]) > 20:
        user_histories[user_id] = user_histories[user_id][-20:]

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 1000,
                "system": SYSTEM_PROMPT,
                "messages": user_histories[user_id]
            }
        )
        reply = response.json()["content"][0]["text"]
    except Exception as e:
        reply = "Hubo un error. Por favor intenta de nuevo. 🙏"

    user_histories[user_id].append({"role": "assistant", "content": reply})
    await update.message.reply_text(reply)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome = (
        "¡Hola! 👋 Soy *FinBot*, tu asistente financiero personal.\n\n"
        "Estoy aquí para ayudarte a llevar el control de tus gastos, ingresos y metas de ahorro.\n\n"
        "¿Por dónde quieres empezar?\n"
        "• 💸 Registrar un gasto o ingreso\n"
        "• 📊 Revisar tu balance del mes\n"
        "• 🎯 Crear una meta de ahorro\n"
        "• 📋 Definir tu presupuesto"
    )
    await update.message.reply_text(welcome, parse_mode="Markdown")

from telegram.ext import CommandHandler

if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("✅ FinBot corriendo...")
    app.run_polling()
