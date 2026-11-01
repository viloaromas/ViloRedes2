import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CallbackQueryHandler, filters
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
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# Inicializar Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.5-flash")

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# Configuración de Google Drive mediante PyDrive2 y Service Account
# Nota: Asegúrate de guardar el contenido de tu JSON en una variable de entorno llamada GOOGLE_CREDENTIALS_JSON 
# o subir el archivo al repositorio con un nombre fijo (ej. credentials.json)
def init_drive():
    try:
        # Configuración automática usando un archivo local 'credentials.json' o variables
        gauth = GoogleAuth()
        # Si prefieres usar archivo local en Render, guárdalo como credentials.json
        gauth.settings['client_config_backend'] = 'service'
        gauth.settings['service_config'] = {
            'client_email': os.environ.get('GOOGLE_CLIENT_EMAIL', 'viloaromas@viloredes.iam.gserviceaccount.com'),
            'private_key': os.environ.get('GOOGLE_PRIVATE_KEY', '').replace('\\n', '\n'),
        }
        gauth.ServiceAuth()
        return GoogleDrive(gauth)
    except Exception as e:
        logging.error(f"Error al inicializar Google Drive: {e}")
        return None

# Manejador de mensajes de texto (Generación de Copywriting)
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text.lower()
    chat_id = update.message.chat_id

    await context.bot.send_message(chat_id=chat_id, text="✨ Analizando formato y redactando propuesta para Vilo Aromas...")

    try:
        if "story" in user_message or "historia" in user_message:
            formato_instruccion = "El usuario quiere una STORY. Redacta un texto ultra breve, directo y visual (máximo 250 caracteres)."
        elif "reel" in user_message or "tiktok" in user_message or "video" in user_message:
            formato_instruccion = "El usuario quiere un REEL / TIKTOK. Incluye gancho inicial, guion corto de 15 segundos y llamada a la acción (máximo 600 caracteres)."
        elif "post" in user_message or "feed" in user_message or "publicacion" in user_message:
            formato_instruccion = "El usuario quiere un POST DE FEED. Escribe un copy persuasivo completo con emojis y hashtags (máximo 1500 caracteres)."
        else:
            formato_instruccion = "Ofrece una estructura versátil de redes sociales con ideas de copy y hashtags (máximo 1000 caracteres)."

        prompt_completo = (
            f"Actúa como un experto en marketing visual y community manager para la tienda 'Vilo Aromas' "
            f"(productos de limpieza y aromatización como Saphirus, difusores Cony y perfumes Cromax).\n\n"
            f"Solicitud del usuario: '{user_message}'\n\n"
            f"Instrucción de formato: {formato_instruccion}\n\n"
            f"IMPORTANTE: Concluye siempre la idea por completo. No dejes frases a la mitad."
        )

        response = model.generate_content(prompt_completo)
        texto_respuesta = response.text.strip()
        
        if len(texto_respuesta) > 4000:
            corte = texto_respuesta[:4000].rfind('.')
            if corte != -1:
                texto_respuesta = texto_respuesta[:corte + 1]
            else:
                texto_respuesta = texto_respuesta[:4000]

        await context.bot.send_message(chat_id=chat_id, text=texto_respuesta)

    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"Ocurrió un error: {str(e)}")

# Manejador de imágenes (Procesamiento visual y propuestas de fondo)
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    await context.bot.send_message(chat_id=chat_id, text="📸 Imagen recibida. Analizando producto y generando opciones de fondo con IA...")

    try:
        # Descargar la foto enviada por Telegram
        photo_file = await update.message.photo[-1].get_file()
        photo_path = "temp_product.jpg"
        await photo_file.download_to_drive(photo_path)

        # Subir y analizar con Gemini Multimodal
        sample_file = genai.upload_file(photo_path, mime_type="image/jpeg")
        
        prompt_vision = (
            "Analiza este producto de Vilo Aromas (aromatización o limpieza). "
            "Proporciona 3 opciones de escenarios o fondos profesionales diferentes para destacar este producto "
            "(por ejemplo: fondo de mármol elegante con elementos naturales, ambiente minimalista limpio, etc.). "
            "Dale a cada opción un prompt descriptivo listo para generar la escena y redimensionarla según sea Story (1080x1920) o Post (1080x1080)."
        )

        response = model.generate_content([sample_file, prompt_vision])
        
        # Guardar temporalmente la ruta en context para la confirmación
        context.user_data['pending_photo'] = photo_path

        keyboard = [
            [InlineKeyboardButton("✅ Aprobar y Guardar en Drive", callback_data="save_drive")],
            [InlineKeyboardButton("❌ Cancelar", callback_data="cancel")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await context.bot.send_message(
            chat_id=chat_id, 
            text=f"✨ **Opciones de ambientación generadas:**\n\n{response.text}\n\n¿Deseas guardar la imagen original procesada en tu Google Drive?",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )

    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"Error procesando la imagen: {str(e)}")

# Callback para los botones de aprobación
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id

    if query.data == "save_drive":
        await query.edit_message_text(text="☁️ Subiendo imagen a tu Google Drive de Vilo Aromas...")
        try:
            drive = init_drive()
            photo_path = context.user_data.get('pending_photo', 'temp_product.jpg')
            
            if drive and os.path.exists(photo_path):
                file_drive = drive.CreateFile({'title': 'vilo_producto_procesado.jpg'})
                file_drive.SetContentFile(photo_path)
                file_drive.Upload()
                await context.bot.send_message(chat_id=chat_id, text="¡Guardado con éxito en Google Drive! 🚀")
            else:
                await context.bot.send_message(chat_id=chat_id, text="Error: No se pudo conectar con Google Drive o el archivo temporal expiró.")
        except Exception as e:
            await context.bot.send_message(chat_id=chat_id, text=f"Error al subir a Drive: {str(e)}")
    else:
        await query.edit_message_text(text="Operación cancelada.")

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(button_callback))
    
    print("Bot avanzado de Vilo Aromas en marcha en Render!")
    app.run_polling()
