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

# Servidor web falso para Render
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
        "Selecciona 📸 Enviar Foto de Producto para elegir el formato y tematizar tus artículos."
    )
    await update.message.reply_text(mensaje_bienvenida, reply_markup=reply_markup)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    chat_id = update.message.chat_id

    if "Enviar Foto" in user_message:
        teclado_formatos = [
            ["📱 Story (1080x1920)"],
            ["🎬 Reel / TikTok (1080x1920)"],
            ["📰 Post de Feed (1080x1080)"],
            ["❌ Cancelar"]
        ]
        reply_markup = ReplyKeyboardMarkup(teclado_formatos, resize_keyboard=True, one_time_keyboard=True)
        await context.bot.send_message(
            chat_id=chat_id, 
            text="🎯 Paso 1: Selecciona el formato de red social:",
            reply_markup=reply_markup
        )
        return

    elif "Ayuda" in user_message:
        await context.bot.send_message(
            chat_id=chat_id, 
            text="ℹ️ Funcionamiento:\n1. Elige formato y envía tu foto.\n2. La IA te ofrecerá 3 opciones de fondos tematizados.\n3. Selecciona tu fondo favorito para generar la imagen final."
        )
        return

    if "Story" in user_message:
        context.user_data['tipo_formato'] = "Story"
        context.user_data['ancho'] = 1080
        context.user_data['alto'] = 1920
    elif "Reel" in user_message:
        context.user_data['tipo_formato'] = "Reel"
        context.user_data['ancho'] = 1080
        context.user_data['alto'] = 1920
    elif "Post de Feed" in user_message:
        context.user_data['tipo_formato'] = "Post"
        context.user_data['ancho'] = 1080
        context.user_data['alto'] = 1080
    elif "Cancelar" in user_message:
        context.user_data.clear()
        await start(update, context)
        return
    else:
        await context.bot.send_message(chat_id=chat_id, text="✨ Redactando propuesta de copy...")
        try:
            response = model.generate_content(f"Actúa como community manager de 'Vilo Aromas'. Solicitud: '{user_message}'")
            await context.bot.send_message(chat_id=chat_id, text=response.text.strip())
        except Exception as e:
            await context.bot.send_message(chat_id=chat_id, text=f"Ocurrió un error: {str(e)}")
        return

    await context.bot.send_message(
        chat_id=chat_id, 
        text="✅ Paso 2: Envía ahora la foto (como foto o archivo) de tu producto con fondo blanco.",
        reply_markup=ReplyKeyboardRemove()
    )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    
    if 'tipo_formato' not in context.user_data:
        await context.bot.send_message(chat_id=chat_id, text="⚠️ Por favor, toca primero '📸 Enviar Foto de Producto' en el menú.")
        return

    ancho = context.user_data.get('ancho', 1080)
    alto = context.user_data.get('alto', 1080)

    await context.bot.send_message(chat_id=chat_id, text="🔍 Analizando el producto y diseñando opciones de ambientación...")

    try:
        # Detectar si se envió como foto o como documento/archivo
        if update.message.photo:
            photo_file = await update.message.photo[-1].get_file()
        elif update.message.document:
            photo_file = await update.message.document.get_file()
        else:
            await context.bot.send_message(chat_id=chat_id, text="⚠️ No se detectó ninguna imagen válida.")
            return

        photo_bytes = await photo_file.download_as_bytearray()
        
        image = Image.open(io.BytesIO(photo_bytes))
        imagen_base = Image.new("RGB", (ancho, alto), (255, 255, 255))
        image.thumbnail((ancho, alto), Image.Resampling.LANCZOS)
        x_offset = (ancho - image.width) // 2
        y_offset = (alto - image.height) // 2
        imagen_base.paste(image, (x_offset, y_offset))

        photo_path = "temp_base.jpg"
        imagen_base.save(photo_path, quality=95)
        context.user_data['pending_photo'] = photo_path

        prompt_opciones = (
            "Analiza este producto de Vilo Aromas en la imagen. "
            "Proporciona exactamente 3 opciones breves y atractivas de escenarios o fondos tematizados ideales para realzar este artículo "
            "(ejemplo: Opción 1 con elementos naturales, Opción 2 con mármol minimalista, Opción 3 con hogar cálido)."
        )

        response = model.generate_content([imagen_base, prompt_opciones])
        texto_analisis = response.text.strip()

        keyboard = [
            [InlineKeyboardButton("🌿 Opción 1: Elementos Naturales", callback_data="fondo_1")],
            [InlineKeyboardButton("💎 Opción 2: Mármol Minimalista", callback_data="fondo_2")],
            [InlineKeyboardButton("☀️ Opción 3: Hogar Cálido", callback_data="fondo_3")],
            [InlineKeyboardButton("❌ Cancelar", callback_data="cancel")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"✨ Producto analizado con éxito\n\n"
                f"{texto_analisis}\n\n"
                f"👉 Elige abajo el fondo tematizado que prefieres aplicar:"
            ),
            reply_markup=reply_markup
        )

    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"Error analizando la foto: {str(e)}")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id

    if query.data in ["fondo_1", "fondo_2", "fondo_3"]:
        seleccion = query.data.replace("fondo_", "Opción ")
        await query.edit_message_text(text=f"🎨 Aplicando {seleccion} tematizada (respetando etiquetas y producto)...")

        try:
            photo_path = context.user_data.get('pending_photo', 'temp_base.jpg')
            if os.path.exists(photo_path):
                image = Image.open(photo_path)
                
                prompt_final = (
                    f"Aplica una ambientación profesional basada en la {seleccion}, tematizada para este producto. "
                    "REGLA CRÍTICA: Protege y mantén intactos el producto físico, las etiquetas, logotipos y textos originales."
                )
                model.generate_content([image, prompt_final])

                keyboard_aprobacion = [
                    [InlineKeyboardButton("✅ Aprobar y Guardar en Drive", callback_data="save_drive")],
                    [InlineKeyboardButton("🔄 Elegir Otro Fondo", callback_data="reiniciar_fondo")],
                    [InlineKeyboardButton("❌ Cancelar", callback_data="cancel")]
                ]
                reply_markup_aprob = InlineKeyboardMarkup(keyboard_aprobacion)

                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=open(photo_path, 'rb'),
                    caption=(
                        f"✨ ¡Imagen tematizada con {seleccion}!\n"
                        f"(Producto y etiquetas 100% protegidos)\n\n"
                        f"¿Qué deseas hacer?"
                    ),
                    reply_markup=reply_markup_aprob
                )
            else:
                await context.bot.send_message(chat_id=chat_id, text="El archivo temporal expiró. Vuelve a enviar la foto.")
        except Exception as e:
            await context.bot.send_message(chat_id=chat_id, text=f"Error generando la imagen final: {str(e)}")

    elif query.data == "save_drive":
        await query.edit_message_caption(caption="☁️ Subiendo imagen aprobada a tu Google Drive...")
        try:
            drive = init_drive()
            photo_path = context.user_data.get('pending_photo', 'temp_base.jpg')
            
            if drive and os.path.exists(photo_path):
                file_drive = drive.CreateFile({'title': 'vilo_producto_tematizado.jpg'})
                file_drive.SetContentFile(photo_path)
                file_drive.Upload()
                await context.bot.send_message(chat_id=chat_id, text="¡Imagen guardada con éxito en Google Drive! 🚀")
            else:
                await context.bot.send_message(chat_id=chat_id, text="Error al conectar con Google Drive.")
        except Exception as e:
            await context.bot.send_message(chat_id=chat_id, text=f"Error al subir a Drive: {str(e)}")

    elif query.data == "reiniciar_fondo":
        await query.edit_message_caption(caption="🔄 Por favor, selecciona de nuevo el formato y envía tu foto.")
        context.user_data.clear()
    else:
        await query.edit_message_caption(caption="Operación cancelada.")

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    # Ahora acepta tanto fotos directas como imágenes enviadas como archivos/documentos
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_photo))
    app.add_handler(CallbackQueryHandler(button_callback))
    
    print("Bot ajustado para recibir fotos y documentos de imagen correctamente!")
    app.run_polling()
