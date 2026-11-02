import os
import logging
import io
import requests
from PIL import Image
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, CallbackQueryHandler, filters
from google import genai
from google.genai import types
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive

class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot de Vilo Aromas con Imagen 3 activo!")

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), DummyHandler)
    server.serve_forever()

server_thread = threading.Thread(target=run_web_server, daemon=True)
server_thread.start()

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

# Inicializar cliente oficial de Google GenAI para Gemini e Imagen 3
client = genai.Client(api_key=GEMINI_API_KEY)

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

def generar_ideas_personalizadas(product_name):
    try:
        prompt = (
            f"Actúa como director de arte publicitario para 'Vilo Aromas'. "
            f"Para este producto: '{product_name}', genera exactamente 3 ideas cortas y creativas "
            f"de escenarios fotográficos comerciales estilo lifestyle (ej: entorno botánico con elementos naturales, superficie de mármol elegante, estudio moderno luminoso). "
            f"Devuelve únicamente las 3 ideas separadas por líneas, sin numeración ni textos extras."
        )
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt
        )
        lineas = [l.strip() for l in response.text.strip().split('\n') if l.strip()]
        ideas = []
        for l in lineas:
            for prefix in ["1.", "2.", "3.", "1-", "2-", "3-", "•"]:
                if l.startswith(prefix):
                    l = l[len(prefix):].strip()
            if l:
                ideas.append(l)
        
        if len(ideas) < 3:
            ideas = [
                f"Entorno botánico natural exclusivo para {product_name} con luz suave de ventana",
                f"Superficie de mármol blanco elegante con iluminación cálida de estudio",
                f"Espacio moderno y luminoso con detalles minimalistas y vegetación sutil"
            ]
        return ideas[:3]
    except Exception:
        return [
            f"Entorno botánico natural exclusivo para {product_name} con luz suave de ventana",
            f"Superficie de mármol blanco elegante con iluminación cálida de estudio",
            f"Espacio moderno y luminoso con detalles minimalistas y vegetación sutil"
        ]

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name
    menu_teclado = [
        ["📸 Enviar Foto de Producto"],
        ["📝 Redactar Copy (Story / Reel / Post)"],
        ["❓ Ayuda / Estado"]
    ]
    reply_markup = ReplyKeyboardMarkup(menu_teclado, resize_keyboard=True)
    await update.message.reply_text(f"¡Hola {user_name}! 👋 Bienvenido al asistente de Vilo Aromas. Selecciona 📸 Enviar Foto de Producto para comenzar.", reply_markup=reply_markup)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    chat_id = update.message.chat_id

    if "Enviar Foto" in user_message:
        await context.bot.send_message(chat_id=chat_id, text="✅ Envía ahora la foto de tu producto.", reply_markup=ReplyKeyboardRemove())
        return
    elif "Ayuda" in user_message:
        await context.bot.send_message(chat_id=chat_id, text="ℹ️ Envía tu foto para que la inteligencia artificial analice el producto y genere escenarios fotorrealistas con Imagen 3.")
        return

    await context.bot.send_message(chat_id=chat_id, text="✨ Redactando propuesta...")
    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=f"Actúa como community manager de 'Vilo Aromas'. Solicitud: '{user_message}'"
        )
        await context.bot.send_message(chat_id=chat_id, text=response.text.strip())
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"Error: {str(e)}")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    try:
        if update.message.photo:
            photo_file = await update.message.photo[-1].get_file()
        elif update.message.document:
            photo_file = await update.message.document.get_file()
        else:
            await context.bot.send_message(chat_id=chat_id, text="⚠️ Imagen no válida.")
            return

        photo_bytes = await photo_file.download_as_bytearray()
        image = Image.open(io.BytesIO(photo_bytes))
        
        # Análisis del producto con Gemini usando bytes
        img_byte_arr = io.BytesIO()
        image.save(img_byte_arr, format='JPEG')
        img_byte_arr = img_byte_arr.getvalue()

        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[
                types.Part.from_bytes(data=img_byte_arr, mime_type='image/jpeg'),
                "Identifica la marca exacta, línea y tipo de este producto (ej: Aromatizante textil Vilo Aromas o similar). Escribe únicamente el nombre exacto sin explicaciones."
            ]
        )
        product_name = response.text.strip()
        context.user_data['product_name'] = product_name

        await context.bot.send_message(chat_id=chat_id, text=f"🔎 Producto detectado: *{product_name}*\n🧠 Creando ideas de diseño con Inteligencia Artificial...", parse_mode="Markdown")
        
        ideas = generar_ideas_personalizadas(product_name)
        context.user_data['current_ideas'] = ideas

        await mostrar_menu_ideas(context, chat_id, product_name, ideas)

    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"Error procesando la foto: {str(e)}")

async def mostrar_menu_ideas(context, chat_id, product_name, ideas):
    keyboard = []
    for idx, idea in enumerate(ideas):
        texto_boton = f"💡 {idea[:42]}..." if len(idea) > 45 else f"💡 {idea}"
        keyboard.append([InlineKeyboardButton(texto_boton, callback_data=f"idea_{idx}")])
    
    keyboard.append([InlineKeyboardButton("🔄 ¡Más ideas diferentes!", callback_data="mas_ideas")])
    keyboard.append([InlineKeyboardButton("❌ Cancelar", callback_data="cancelar")])

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"✨ **Conceptos creativos para {product_name}:**\nSelecciona una idea para renderizar la escena con Imagen 3:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    data = query.data

    if data == "mas_ideas":
        await query.edit_message_text(text="🔄 Generando nuevas opciones de escenarios...")
        product_name = context.user_data.get('product_name', 'Producto')
        try:
            ideas = generar_ideas_personalizadas(product_name)
            context.user_data['current_ideas'] = ideas
            await mostrar_menu_ideas(context, chat_id, product_name, ideas)
        except Exception as e:
            await context.bot.send_message(chat_id=chat_id, text=f"Error generando ideas: {str(e)}")

    elif data.startswith("idea_"):
        idx = int(data.replace("idea_", ""))
        ideas = context.user_data.get('current_ideas', [])
        idea_elegida = ideas[idx] if idx < len(ideas) else "Entorno comercial profesional"
        product_name = context.user_data.get('product_name', 'Aromatizante de alta gama')

        await query.edit_message_text(text=f"🎨 Renderizando escena fotorrealista con Imagen 3 basada en:\n_{idea_elegida}_", parse_mode="Markdown")
        
        try:
            # Prompt en inglés optimizado para que Imagen 3 devuelva calidad publicitaria fotográfica
            prompt_imagen = (
                f"Professional high-end commercial product photography of a premium bottle of '{product_name}' placed centrally on a clean luxury surface. "
                f"Environment details: {idea_elegida}. "
                f"Studio lighting, soft shadows, photorealistic, 8k resolution, advertising quality, depth of field."
            )

            result = client.models.generate_images(
                model='imagen-3.0-generate-002',
                prompt=prompt_imagen,
                config=types.GenerateImagesConfig(
                    number_of_images=1,
                    output_mime_type="image/jpeg",
                    aspect_ratio="1:1",
                    person_generation="DONT_ALLOW",
                    safety_filter_level="block_medium_and_above",
                )
            )

            final_path = "temp_imagen3_scene.jpg"
            for generated_image in result.generated_images:
                with open(final_path, "wb") as f:
                    f.write(generated_image.image.image_bytes)
                break

            context.user_data['pending_photo'] = final_path

            kb_aprob = [
                [InlineKeyboardButton("✅ Aprobar y Guardar en Drive", callback_data="save_drive_escena")],
                [InlineKeyboardButton("🔄 Ver otras ideas", callback_data="mas_ideas")],
                [InlineKeyboardButton("❌ Cancelar", callback_data="cancelar")]
            ]
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=open(final_path, 'rb'),
                caption=f"✨ **¡Escena generada con Imagen 3!**\n*Concepto:* _{idea_elegida}_\n\n¿Qué deseas hacer?",
                reply_markup=InlineKeyboardMarkup(kb_aprob),
                parse_mode="Markdown"
            )
        except Exception as e:
            await context.bot.send_message(chat_id=chat_id, text=f"Error generando la imagen con IA: {str(e)}")

    elif data == "save_drive_escena":
        await query.edit_message_caption(caption="☁️ Subiendo escena a Google Drive...")
        try:
            drive = init_drive()
            path = context.user_data.get('pending_photo', 'temp_imagen3_scene.jpg')
            if drive and os.path.exists(path):
                file_d = drive.CreateFile({'title': 'vilo_imagen3_pro.jpg'})
                file_d.SetContentFile(path)
                file_d.Upload()
                await context.bot.send_message(chat_id=chat_id, text="¡Escena guardada con éxito en Google Drive! 🚀")
            else:
                await context.bot.send_message(chat_id=chat_id, text="Error conectando con Google Drive.")
        except Exception as e:
            await context.bot.send_message(chat_id=chat_id, text=f"Error: {str(e)}")

    elif data == "cancelar":
        await query.edit_message_text(text="Operación cancelada.")
        context.user_data.clear()

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_photo))
    app.add_handler(CallbackQueryHandler(button_callback))
    
    print("Bot de Vilo Aromas con Imagen 3 operativo!")
    app.run_polling(drop_pending_updates=True)
