import os
import requests
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

SYSTEM_PROMPT = """Eres FinBot, asistente financiero personal. Ayudas a registrar gastos e ingresos, definir presupuestos, crear metas de ahorro y dar consejos financieros. Habla siempre en espanol, se amable, sin juicios, usa formato claro para numeros como $1,200.00"""

user_histories = {}

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        pass

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    httpd = HTTPServer(("0.0.0.0", port), HealthHandler)
    print(f"Web server on port {port}")
    httpd.serve_forever()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hola! Soy FinBot, tu asistente financiero.\n\n"
        "Puedo ayudarte a:\n"
        "- Registrar gastos e ingresos\n"
        "- Revisar tu balance\n"
        "- Crear metas de ahorro\n"
        "- Definir tu presupuesto\n\n"
        "Escribe lo que necesitas!"
    )

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
        resp = requests.post(
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
            },
            timeout=30
        )
        reply = resp.json()["content"][0]["text"]
    except Exception as e:
        print(f"Error API: {e}")
        reply = "Hubo un error, intenta de nuevo."

    user_histories[user_id].append({"role": "assistant", "content": reply})
    await update.message.reply_text(reply)

def main():
    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("FinBot iniciado!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
