import os
import logging
import io
from PIL import Image
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, CallbackQueryHandler, filters
import google.generativeai as genai
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive

# Servidor web falso para mantener activo Render en el plan gratuito
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot de Vilo Aromas activo y funcionando!")

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), DummyHandler)
    server.serve_forever()

server_thread = threading.Thread(target=run_web_server, daemon=True)
server_thread.start()

# Credenciales de entorno
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

# Inicializar Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.5-flash")

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

def init_drive():
    try:
        gauth = GoogleAuth()
        gauth.settings['client_config_backend'] = 'service'
        gauth.settings['service_config'] = {
            'client_email': os.environ.get('GOOGLE_CLIENT_EMAIL', '').strip(),
            'private_key': os.environ.get('GOOGLE_PRIVATE_KEY', '').strip().replace('\\n', '\n'),
        }
        gauth.ServiceAuth()
        return GoogleDrive(gauth)
    except Exception as e:
        logging.error(f"Error al inicializar Google Drive: {e}")
        return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name
    menu_teclado = [
        ["📸 Enviar Foto de Producto (Fondo Blanco)"],
        ["📝 Redactar Copy (Story / Reel / Post)"],
        ["❓ Ayuda / Estado"]
    ]
    reply_markup = ReplyKeyboardMarkup(menu_teclado, resize_keyboard=True)
    
    mensaje_bienvenida = (
        f"¡Hola {user_name}! 👋 Bienvenido al asistente oficial de **Vilo Aromas**.\n\n"
        "Elige una opción del menú inferior o simplemente:\n"
        "• **Envía una foto** de tu producto con fondo blanco para procesarla y guardarla en Drive.\n"
        "• **Escribe un texto o título** para que te genere el copy de marketing adaptado."
    )
    await update.message.reply_text(mensaje_bienvenida, reply_markup=reply_markup, parse_mode="Markdown")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    chat_id = update.message.chat_id

    if "Enviar Foto" in user_message:
        await context.bot.send_message(
            chat_id=chat_id, 
            text="📷 ¡Perfecto! Envía ahora la foto de tu producto con fondo blanco para que la analice y preparemos las opciones."
        )
        return
    elif "Ayuda" in user_message:
        await context.bot.send_message(
            chat_id=chat_id, 
            text="ℹ️ **Cómo usar tu bot de Vilo Aromas:**\n1. Envía una foto con fondo blanco -> La analiza la IA y te da opciones para guardar en Drive.\n2. Envía un título/texto -> Te genera el texto persuasivo para redes sociales."
        )
        return

    await context.bot.send_message(chat_id=chat_id, text="✨ Analizando solicitud y redactando propuesta para Vilo Aromas...")

    try:
        user_lower = user_message.lower()
        if "story" in user_lower or "historia" in user_lower:
            formato_instruccion = "El usuario quiere una STORY. Redacta un texto ultra breve, directo y visual (máximo 250 caracteres)."
