import os
import requests
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

SYSTEM_PROMPT = """Eres un asistente financiero personal inteligente llamado FinBot. Tu objetivo es ayudar al usuario a gestionar su dinero de forma simple, clara y sin juicios.

Tus funciones principales son:
1. Registro de gastos e ingresos
2. Presupuesto mensual
3. Resumen financiero
4. Consejos personalizados
5. Metas de ahorro

Reglas: Habla siempre en español, sé amable y directo, sin juicios negativos, usa formato claro para números (ej: $1,200.00), usa emojis con moderación."""

user_histories = {}

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"FinBot activo!")
    def log_message(self, format, *args):
        pass

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome = (
        "¡Hola! 👋 Soy *FinBot*, tu asistente financiero personal.\n\n"
        "¿Por dónde quieres empezar?\n"
        "• 💸 Registrar un gasto o ingreso\n"
        "• 📊 Revisar tu balance del mes\n"
        "• 🎯 Crear una meta de ahorro\n"
        "• 📋 Definir tu presupuesto"
    )
    await update.message.reply_text(welcome, parse_mode="Markdown")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_text = update.message.text

    if user_id not in user_histories:
        user_histories[user_id] = []

    user_histories[user_id].append({"role": "user", "content": user_text})

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

if __name__ == "__main__":
    t = threading.Thread(target=run_web_server, daemon=True)
    t.start()
    print("✅ Servidor web activo")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("✅ FinBot corriendo...")
    app.run_polling()
