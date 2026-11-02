import os
import logging
import io
import requests
from PIL import Image
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, CallbackQueryHandler, filters
import google.generativeai as genai
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive
from duckduckgo_search import DDGS

# Servidor web para Render
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

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

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

# Función para buscar imágenes en la web del producto exacto
def buscar_imagenes_web(nombre_producto, max_resultados=3):
    urls_imagenes = []
    try:
        with DDGS() as ddgs:
            results = ddgs.images(query=f"{nombre_producto} comercial lifestyle foto", max_results=max_resultados)
            for r in results:
                if 'image' in r:
                    urls_imagenes.append(r['image'])
    except Exception as e:
        logging.error(f"Error buscando imágenes web: {e}")
    return urls_imagenes

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name
    menu_teclado = [
        ["📸 Enviar Foto de Producto"],
        ["📝 Redactar Copy (Story / Reel / Post)"],
        ["❓ Ayuda / Estado"]
    ]
    reply_markup = ReplyKeyboardMarkup(menu_teclado, resize_keyboard=True)
    
    mensaje_bienvenida = (
        f"¡Hola {user_name}! 👋 Bienvenido al asistente de Vilo Aromas.\n\n"
        "Envía una foto de tu producto para buscar opciones reales publicadas en la web."
    )
    await update.message.reply_text(mensaje_bienvenida, reply_markup=reply_markup)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    chat_id = update.message.chat_id

    if "Enviar Foto" in user_message:
        await context.bot.send_message(
            chat_id=chat_id, 
            text="✅ Envía la foto (o archivo) de tu producto para identificarlo y buscar fotos asociadas en la web.",
            reply_markup=ReplyKeyboardRemove()
        )
        return

    elif "Ayuda" in user_message:
        await context.bot.send_message(
            chat_id=chat_id, 
            text="ℹ️ Funcionamiento:\n1. Envías la foto de tu producto.\n2. La IA reconoce la marca y variante exacta.\n3. Busca imágenes existentes en la web para que selecciones la que más te gusta y la guardes en Drive."
        )
        return

    await context.bot.send_message(chat_id=chat_id, text="✨ Redactando propuesta de copy...")
    try:
        response = model.generate_content(f"Actúa como community manager de 'Vilo Aromas'. Solicitud: '{user_message}'")
        await context.bot.send_message(chat_id=chat_id, text=response.text.strip())
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"Ocurrió un error: {str(e)}")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id

    await context.bot.send_message(chat_id=chat_id, text="🔍 Identificando producto y rastreando imágenes en la web...")

    try:
        if update.message.photo:
            photo_file = await update.message.photo[-1].get_file()
        elif update.message.document:
            photo_file = await update.message.document.get_file()
        else:
            await context.bot.send_message(chat_id=chat_id, text="⚠️ No se detectó ninguna imagen válida.")
            return

        photo_bytes = await photo_file.download_as_bytearray()
        image = Image.open(io.BytesIO(photo_bytes))

        # Gemini identifica exactamente la marca, tipo y variante del producto en la foto
        prompt_identificacion = (
            "Identifica de forma precisa este producto de aromatización (Marca, tipo de producto y fragancia exacta). "
            "Responde SOLAMENTE con el nombre de búsqueda corto y preciso, por ejemplo: 'Saphirus difusor Linah' o 'Iluminarte Prestige Ropa Limpia'."
        )

        response = model.generate_content([image, prompt_identificacion])
        busqueda_term = response.text.strip()
        context.user_data['busqueda_term'] = busqueda_term

        await context.bot.send_message(chat_id=chat_id, text=f"🔎 Producto detectado: **{busqueda_term}**\nBuscando imágenes...")

        # Buscar opciones en la web
        urls_encontradas = buscar_imagenes_web(busqueda_term, max_resultados=3)

        if not urls_encontradas:
            await context.bot.send_message(chat_id=chat_id, text="No se encontraron imágenes en la web para este producto específico.")
            return

        context.user_data['web_urls'] = urls_encontradas

        # Enviar cada imagen encontrada para que el usuario pueda elegir
        for idx, url in enumerate(urls_encontradas):
            try:
                keyboard = [[InlineKeyboardButton(f"✅ Aprobar Opción {idx+1} y Guardar en Drive", callback_data=f"save_web_{idx}")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=url,
                    caption=f"🖼️ **Opción {idx+1} encontrada en la web**",
                    reply_markup=reply_markup
                )
            except Exception:
                continue

    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"Error en el proceso: {str(e)}")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id

    if query.data.startswith("save_web_"):
        idx = int(query.data.replace("save_web_", ""))
        urls = context.user_data.get('web_urls', [])

        if idx < len(urls):
            target_url = urls[idx]
            await query.edit_message_caption(caption="☁️ Guardando imagen seleccionada en tu Google Drive...")
            try:
                # Descargar la imagen elegida
                img_data = requests.get(target_url).content
                local_file = f"temp_web_approved_{idx}.jpg"
                with open(local_file, 'wb') as f:
                    f.write(img_data)

                drive = init_drive()
                if drive:
                    file_drive = drive.CreateFile({'title': f'vilo_{context.user_data.get("busqueda_term", "producto")}.jpg'})
                    file_drive.SetContentFile(local_file)
                    file_drive.Upload()
                    await context.bot.send_message(chat_id=chat_id, text="¡Foto aprobada guardada con éxito en tu Google Drive! 🚀")
                else:
                    await context.bot.send_message(chat_id=chat_id, text="Error al conectar con Google Drive.")
            except Exception as e:
                await context.bot.send_message(chat_id=chat_id, text=f"Error guardando la foto: {str(e)}")

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_photo))
    app.add_handler(CallbackQueryHandler(button_callback))
    
    print("Bot de Vilo Aromas con búsqueda de imágenes web en marcha!")
    app.run_polling()
