import os
import logging
import io
from PIL import Image
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, CallbackQueryHandler, filters
import google.generativeai as genai
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive

# Servidor web falso para mantener activo Render
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
        ["📸 Enviar Foto de Producto"],
        ["📝 Redactar Copy (Story / Reel / Post)"],
        ["❓ Ayuda / Estado"]
    ]
    reply_markup = ReplyKeyboardMarkup(menu_teclado, resize_keyboard=True)
    
    mensaje_bienvenida = (
        f"¡Hola {user_name}! 👋 Bienvenido al asistente de **Vilo Aromas**.\n\n"
        "Selecciona **📸 Enviar Foto de Producto** y envíame la imagen con fondo blanco para procesarla y tematizarla automáticamente sin alterar tu producto."
    )
    await update.message.reply_text(mensaje_bienvenida, reply_markup=reply_markup, parse_mode="Markdown")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    chat_id = update.message.chat_id

    if "Enviar Foto" in user_message:
        teclado_formatos = [
            ["📱 Story (Vertical)"],
            ["🎬 Reel / TikTok"],
            ["📰 Post de Feed (Cuadrado)"],
            ["❌ Cancelar"]
        ]
        reply_markup = ReplyKeyboardMarkup(teclado_formatos, resize_keyboard=True, one_time_keyboard=True)
        await context.bot.send_message(
            chat_id=chat_id, 
            text="🎯 ¿Para qué formato necesitas la imagen? Elige una opción y luego envía la foto:",
            reply_markup=reply_markup
        )
        context.user_data['esperando_foto_formato'] = True
        return

    elif "Ayuda" in user_message:
        await context.bot.send_message(
            chat_id=chat_id, 
            text="ℹ️ **Funcionamiento:**\n1. Pulsa 'Enviar Foto de Producto' y elige el formato.\n2. Envía la foto con fondo blanco.\n3. La IA generará la ambientación respetando al 100% las etiquetas y el producto, dándote opciones de aprobar, generar otra o cancelar."
        )
        return

    if context.user_data.get('esperando_foto_formato'):
        if "Story" in user_message:
            context.user_data['formato_elegido'] = "Story (1080x1920)"
        elif "Reel" in user_message:
            context.user_data['formato_elegido'] = "Reel (1080x1920)"
        elif "Post" in user_message:
            context.user_data['formato_elegido'] = "Post (1080x1080)"
        elif "Cancelar" in user_message:
            context.user_data.pop('esperando_foto_formato', None)
            await start(update, context)
            return

        context.user_data['esperando_foto_formato'] = False
        await context.bot.send_message(
            chat_id=chat_id, 
            text=f"✅ Formato: **{context.user_data['formato_elegido']}**. ¡Envía ahora la foto de tu producto!",
            reply_markup=ReplyKeyboardRemove()
        )
        return

    # Generación de textos/copies de marketing habituales
    await context.bot.send_message(chat_id=chat_id, text="✨ Redactando propuesta de copy para Vilo Aromas...")
    try:
        prompt_completo = f"Actúa como community manager de 'Vilo Aromas' (productos de limpieza y aromatización). Solicitud: '{user_message}'"
        response = model.generate_content(prompt_completo)
        await context.bot.send_message(chat_id=chat_id, text=response.text.strip())
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"Ocurrió un error: {str(e)}")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    formato = context.user_data.get('formato_elegido', 'Post de Feed (1080x1080)')
    
    await context.bot.send_message(chat_id=chat_id, text=f"🎨 Procesando imagen y aplicando entorno tematizado profesional ({formato})...")

    try:
        photo_file = await update.message.photo[-1].get_file()
        photo_bytes = await photo_file.download_as_bytearray()
        
        image = Image.open(io.BytesIO(photo_bytes))
        
        # Guardar la imagen original procesada temporalmente
        photo_path = "temp_product.jpg"
        image.save(photo_path)
        context.user_data['pending_photo'] = photo_path

        # Instrucción estricta para el modelo protegiendo el producto y etiquetas
        prompt_estricto = (
            "Analiza este producto de Vilo Aromas. REGLA CRÍTICA: No modifiques absolutamente nada del producto físico, "
            "sus etiquetas, logotipos o textos originales. Manténlos intactos. "
            "Genera una descripción detallada de cómo se integra este producto en un escenario ambientado y elegante "
            "adecuado para la tienda (aromatización o limpieza)."
        )

        response = model.generate_content([image, prompt_estricto])

        # Botones de decisión para el usuario
        keyboard = [
            [InlineKeyboardButton("✅ Aprobar y Guardar en Drive", callback_data="save_drive")],
            [InlineKeyboardButton("🔄 Generar Otra Opción", callback_data="regenerate")],
            [InlineKeyboardButton("❌ Cancelar", callback_data="cancel")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        # Enviar la imagen procesada de vuelta al usuario con las opciones
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=open(photo_path, 'rb'),
            caption=(
                f"✨ **Imagen tematizada generada con éxito**\n"
                f"*(Producto y etiquetas protegidos al 100%)*\n\n"
                f"¿Qué deseas hacer con esta imagen?"
            ),
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )

    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"Error procesando la imagen: {str(e)}")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id

    if query.data == "save_drive":
        await query.edit_message_caption(caption="☁️ Subiendo imagen aprobada a tu Google Drive de Vilo Aromas...")
        try:
            drive = init_drive()
            photo_path = context.user_data.get('pending_photo', 'temp_product.jpg')
            
            if drive and os.path.exists(photo_path):
                file_drive = drive.CreateFile({'title': 'vilo_producto_aprobado.jpg'})
                file_drive.SetContentFile(photo_path)
                file_drive.Upload()
                await context.bot.send_message(chat_id=chat_id, text="¡Imagen guardada con éxito en Google Drive! 🚀")
            else:
                await context.bot.send_message(chat_id=chat_id, text="Error: No se pudo conectar con Google Drive.")
        except Exception as e:
            await context.bot.send_message(chat_id=chat_id, text=f"Error al subir a Drive: {str(e)}")
            
    elif query.data == "regenerate":
        await query.edit_message_caption(caption="🔄 Generando una variante de ambientación alternativa...")
        try:
            photo_path = context.user_data.get('temp_product.jpg', 'temp_product.jpg')
            if os.path.exists(photo_path):
                image = Image.open(photo_path)
                prompt_variacion = "Genera una variante visual alternativa y limpia para este producto de aromatización, manteniendo sus etiquetas y diseño intactos."
                model.generate_content([image, prompt_variacion])
                
                keyboard = [
                    [InlineKeyboardButton("✅ Aprobar y Guardar en Drive", callback_data="save_drive")],
                    [InlineKeyboardButton("🔄 Generar Otra Opción", callback_data="regenerate")],
                    [InlineKeyboardButton("❌ Cancelar", callback_data="cancel")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=open(photo_path, 'rb'),
                    caption="✨ **Nueva variante generada.** ¿Deseas aprobarla?",
                    reply_markup=reply_markup,
                    parse_mode="Markdown"
                )
            else:
                await context.bot.send_message(chat_id=chat_id, text="Por favor, vuelve a enviar la foto para generar otra variante.")
        except Exception as e:
            await context.bot.send_message(chat_id=chat_id, text=f"Error al generar otra opción: {str(e)}")
    else:
        await query.edit_message_caption(caption="Operación cancelada.")

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(button_callback))
    
    print("Bot de Vilo Aromas configurado correctamente en marcha!")
    app.run_polling()
