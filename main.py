import os
import logging
import io
from PIL import Image
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, CallbackQueryHandler, filters
import google.generativeai as genai
from google import genai as genai_v2
from google.genai import types
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

# Inicializar librerías de Google AI
genai.configure(api_key=GEMINI_API_KEY)
model_flash = genai.GenerativeModel("gemini-2.5-flash")
client_v2 = genai_v2.Client(api_key=GEMINI_API_KEY)

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
        "Selecciona 📸 Enviar Foto de Producto para elegir el formato y crear escenarios comerciales profesionales estilo revista."
    )
    await update.message.reply_text(mensaje_bienvenida, reply_markup=reply_markup)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    chat_id = update.message.chat_id

    if "Enviar Foto" in user_message:
        teclado_formatos = [
            ["📱 Story / Reel (Vertical 9:16)"],
            ["📰 Post de Feed (Cuadrado 1:1)"],
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
            text="ℹ️ Funcionamiento:\n1. Elige el formato y envía tu foto de producto con fondo blanco.\n2. La IA analizará la fragancia y el envase para ofrecerte 3 opciones de escenarios de lifestyle.\n3. Elige tu favorito y generaremos la escena comercial lista."
        )
        return

    if "Story" in user_message:
        context.user_data['tipo_formato'] = "Story"
        context.user_data['aspect_ratio'] = "9:16"
    elif "Post de Feed" in user_message:
        context.user_data['tipo_formato'] = "Post"
        context.user_data['aspect_ratio'] = "1:1"
    elif "Cancelar" in user_message:
        context.user_data.clear()
        await start(update, context)
        return
    else:
        await context.bot.send_message(chat_id=chat_id, text="✨ Redactando propuesta de copy...")
        try:
            response = model_flash.generate_content(f"Actúa como community manager de 'Vilo Aromas'. Solicitud: '{user_message}'")
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

    await context.bot.send_message(chat_id=chat_id, text="🔍 Analizando el producto y diseñando ambientaciones comerciales...")

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

        # Guardar temporalmente para análisis
        analysis_path = "temp_analysis.jpg"
        image.save(analysis_path)
        context.user_data['analysis_image'] = analysis_path

        # Gemini Flash analiza el producto para extraer detalles de marca, tipo y fragancia
        prompt_analisis = (
            "Analiza este producto de Vilo Aromas en detalle (marca como Iluminarte Prestige, tipo de envase, "
            "color del líquido y fragancia indicada en la etiqueta). "
            "Basado en esto, describe exactamente 3 opciones de escenarios de lifestyle comercial de alta calidad "
            "para generar con IA (por ejemplo: sala de estar moderna, cuarto de lavado con toallas, cocina luminosa con elementos naturales). "
            "Escribe las 3 opciones de forma clara y atractiva para que el usuario elija."
        )

        sample_file = genai.upload_file(analysis_path, mime_type="image/jpeg")
        response = model_flash.generate_content([sample_file, prompt_analisis])
        texto_opciones = response.text.strip()

        context.user_data['product_description'] = texto_opciones

        keyboard = [
            [InlineKeyboardButton("🌿 Opción 1: Entorno Natural / Botánico", callback_data="escena_1")],
            [InlineKeyboardButton("🛋️ Opción 2: Living / Sala Elegante", callback_data="escena_2")],
            [InlineKeyboardButton("🧺 Opción 3: Hogar / Lavadero Luminoso", callback_data="escena_3")],
            [InlineKeyboardButton("❌ Cancelar", callback_data="cancel")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"✨ **Producto analizado con éxito**\n\n"
                f"{texto_opciones}\n\n"
                f"👉 **Elige el escenario de lifestyle que prefieres generar:**"
            ),
            reply_markup=reply_markup
        )

    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"Error analizando la foto: {str(e)}")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id

    if query.data in ["escena_1", "escena_2", "escena_3"]:
        opcion_num = query.data.replace("escena_", "")
        await query.edit_message_text(text=f"🎨 Generando escena comercial fotorrealista (Opción {opcion_num}) con Imagen 3...")

        try:
            aspect_ratio = context.user_data.get('aspect_ratio', '1:1')
            
            # Construir un prompt hiper-detallado para Imagen 3 basado en los ejemplos del usuario
            prompt_imagen = (
                "Commercial product photography, professional lifestyle advertising shot, "
                "high-end interior design background with soft natural window light, photorealistic, 8k resolution. "
                f"Featuring a premium air freshener / cleaning spray bottle labeled 'Iluminarte Prestige' with crystal clear details, "
                f"placed naturally on a clean marble or wooden surface with professional depth of field, "
                f"ambient styling matching Option {opcion_num} (botanical elements, cozy living room, or clean laundry room setup with folded towels). "
                "Crisp typography on labels, perfect commercial lighting and reflections."
            )

            # Generar imagen con Imagen 3
            result = client_v2.models.generate_images(
                model='imagen-3.0-generate-002',
                prompt=prompt_imagen,
                config=types.GenerateImagesConfig(
                    number_of_images=1,
                    output_mime_type="image/jpeg",
                    aspect_ratio=aspect_ratio,
                    person_generation="DONT_ALLOW",
                    safety_filter_level="block_medium_and_above",
                )
            )

            for generated_image in result.generated_images:
                image_bytes = generated_image.image.image_bytes
                final_path = "temp_generated_scene.jpg"
                with open(final_path, "wb") as f:
                    f.write(image_bytes)
                
                context.user_data['pending_photo'] = final_path

                keyboard_aprobacion = [
                    [InlineKeyboardButton("✅ Aprobar y Guardar en Drive", callback_data="save_drive")],
                    [InlineKeyboardButton("🔄 Generar Otro Escenario", callback_data="reiniciar_escena")],
                    [InlineKeyboardButton("❌ Cancelar", callback_data="cancel")]
                ]
                reply_markup_aprob = InlineKeyboardMarkup(keyboard_aprobacion)

                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=open(final_path, 'rb'),
                    caption=(
                        f"✨ **¡Escena comercial generada con éxito!**\n"
                        f"*(Estilo lifestyle profesional tipo revista)*\n\n"
                        f"¿Qué deseas hacer?"
                    ),
                    reply_markup=reply_markup_aprob
                )
                break

        except Exception as e:
            await context.bot.send_message(chat_id=chat_id, text=f"Error generando la imagen con IA: {str(e)}")

    elif query.data == "save_drive":
        await query.edit_message_caption(caption="☁️ Subiendo escena aprobada a tu Google Drive...")
        try:
            drive = init_drive()
            photo_path = context.user_data.get('pending_photo', 'temp_generated_scene.jpg')
            
            if drive and os.path.exists(photo_path):
                file_drive = drive.CreateFile({'title': 'vilo_lifestyle_comercial.jpg'})
                file_drive.SetContentFile(photo_path)
                file_drive.Upload()
                await context.bot.send_message(chat_id=chat_id, text="¡Escena guardada con éxito en Google Drive! 🚀")
            else:
                await context.bot.send_message(chat_id=chat_id, text="Error al conectar con Google Drive.")
        except Exception as e:
            await context.bot.send_message(chat_id=chat_id, text=f"Error al subir a Drive: {str(e)}")

    elif query.data == "reiniciar_escena":
        await query.edit_message_caption(caption="🔄 Por favor, selecciona de nuevo el formato y envía tu foto.")
        context.user_data.clear()
    else:
        await query.edit_message_caption(caption="Operación cancelada.")

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_photo))
    app.add_handler(CallbackQueryHandler(button_callback))
    
    print("Bot de Vilo Aromas con Imagen 3 en marcha!")
    app.run_polling()
