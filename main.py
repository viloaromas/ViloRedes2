import os
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
import google.generativeai as genai
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

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

# Iniciar el servidor web en un hilo secundario
server_thread = threading.Thread(target=run_web_server, daemon=True)
server_thread.start()

# Credenciales de entorno
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# Inicializar Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.5-flash")

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    chat_id = update.message.chat_id

    await context.bot.send_message(chat_id=chat_id, text="✨ Creando propuesta para tus redes...")

    try:
        # Prompt optimizado para ajustar longitud por formato (Story, Reel, Post) y evitar textos cortados
        prompt_completo = (
            f"Actúa como un experto en marketing visual y community manager para la marca 'Vilo Aromas' "
            f"(tienda de aromatización y productos de limpieza).\n\n"
            f"Solicitud del usuario: '{user_message}'\n\n"
            f"Reglas de respuesta segun el tipo de contenido mencionado o solicitado:\n"
            f"- Si pide una STORY: Escribe un texto ultra breve, directo y visual (máximo 250 caracteres).\n"
            f"- Si pide un REEL / TIKTOK: Incluye gancho inicial, guion corto de 15 segundos y llamada a la acción (máximo 600 caracteres).\n"
            f"- Si pide un POST DE FEED o sugerencia general: Escribe un copy persuasivo completo con emojis y hashtags (máximo 1500 caracteres).\n"
            f"- IMPORTANTE: Concluye siempre la idea y la oración por completo. No dejes frases a la mitad ni respuestas incompletas."
        )

        response = model.generate_content(prompt_completo)
        texto_respuesta = response.text.strip()
        
        # Filtro de seguridad para el límite técnico de Telegram (4096 caracteres)
        if len(texto_respuesta) > 4000:
            # Buscar el último punto final antes del límite para no cortar oraciones a la mitad
            corte = texto_respuesta[:4000].rfind('.')
            if corte != -1:
                texto_respuesta = texto_respuesta[:corte + 1]
            else:
                texto_respuesta = texto_respuesta[:4000]

        await context.bot.send_message(chat_id=chat_id, text=texto_respuesta)

    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"Ocurrió un error: {str(e)}")

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    print("Bot en marcha en Render!")
    app.run_polling()
