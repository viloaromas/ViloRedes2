import os
import logging
import io
import requests
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

def buscar_en_web_amplia(nombre_producto):
    return [
        "https://http2.mlstatic.com/D_NQ_NP_797899-MLA110274085932_052026-O.webp",
        "https://s3i.nyc3.cdn.digitaloceanspaces.com/v1/product/127V/galeria/eWpM5DqMKrKBj3uUuLAt4Lo5T2zQ7bwqWeIi4wNN.webp",
        "https://http2.mlstatic.com/D_NQ_NP_762067-MLA104614258262_012026-O.webp"
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
        await context.bot.send_message(chat_id=chat_id, text="✅ Envía ahora la foto de tu producto con fondo blanco.", reply_markup=ReplyKeyboardRemove())
        return
    elif "Ayuda" in user_message:
        await context.bot.send_message(chat_id=chat_id, text="ℹ️ Envía tu foto para buscar opciones en la web o generar fondos profesionales con IA.")
        return

    await context.bot.send_message(chat_id=chat_id, text="✨ Redactando propuesta...")
    try:
        response = model.generate_content(f"Actúa como community manager de 'Vilo Aromas'. Solicitud: '{user_message}'")
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
        original_path = "temp_user_product.jpg"
        image.save(original_path)
        context.user_data['original_photo'] = original_path

        prompt_id = "Identifica la marca, línea y fragancia exacta de este producto (ej: Iluminarte Mango Maracuyá). Devuelve solo el texto."
        response = model.generate_content([image, prompt_id])
        product_name = response.text.strip()
        context.user_data['product_name'] = product_name

        keyboard = [
            [InlineKeyboardButton("🌐 1. Buscar fotos comerciales en la Web", callback_data="modo_web")],
            [InlineKeyboardButton("✨ 2. Generar Fondo Fotorrealista con IA", callback_data="modo_escena")],
            [InlineKeyboardButton("❌ Cancelar", callback_data="cancelar")]
        ]
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"✅ **Foto recibida con éxito**\n🔎 Producto detectado: *{product_name}*\n\n👉 **¿Qué deseas hacer?**",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"Error: {str(e)}")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    data = query.data

    if data == "modo_web":
        await query.edit_message_text(text="🌐 Buscando imágenes comerciales en catálogos web...")
        product_name = context.user_data.get('product_name', 'aromatizante')
        urls = buscar_en_web_amplia(product_name)

        context.user_data['web_urls'] = urls
        for idx, url in enumerate(urls):
            try:
                keyboard_opt = [[InlineKeyboardButton(f"✅ Guardar Opción {idx+1} en Drive", callback_data=f"save_web_{idx}")]]
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=url,
                    caption=f"🖼️ **Opción Web Comercial {idx+1}**",
                    reply_markup=InlineKeyboardMarkup(keyboard_opt)
                )
            except Exception:
                continue

    elif data == "modo_escena":
        keyboard_fondos = [
            [InlineKeyboardButton("🌿 Estilo 1: Entorno Botánico y Natural", callback_data="estilo_1")],
            [InlineKeyboardButton("🛋️ Estilo 2: Living / Sala Elegante", callback_data="estilo_2")],
            [InlineKeyboardButton("☀️ Estilo 3: Hogar / Lavadero Luminoso", callback_data="estilo_3")],
            [InlineKeyboardButton("❌ Cancelar", callback_data="cancelar")]
        ]
        await query.edit_message_text(text="✨ Selecciona el estilo de fondo profesional a generar:", reply_markup=InlineKeyboardMarkup(keyboard_fondos))

    elif data.startswith("estilo_"):
        estilo_num = data.replace("estilo_", "")
        await query.edit_message_text(text=f"🎨 Generando fondo fotorrealista (Estilo {estilo_num}) con Inteligencia Artificial...")
        
        try:
            # Definir prompts detallados para crear fondos vacíos de alta gama estilo revista
            prompts_fondos = {
                "1": "Professional empty commercial background, luxurious botanical interior design with fresh green plants, soft natural window light, polished white marble countertop surface, photorealistic, 8k resolution, empty center space for product placement.",
                "2": "Professional empty commercial background, elegant cozy living room interior with warm lighting, wooden shelves, soft blur depth of field, polished marble surface, photorealistic, 8k resolution, empty center space for product placement.",
                "3": "Professional empty commercial background, bright modern laundry room with neatly folded pastel towels on shelves, soft natural window light, clean polished countertop surface, photorealistic, 8k resolution, empty center space for product placement."
            }

            prompt_elegido = prompts_fondos.get(estilo_num, prompts_fondos["1"])

            # Generar el fondo con Imagen 3
            result = client_v2.models.generate_images(
                model='imagen-3.0-generate-002',
                prompt=prompt_elegido,
                config=types.GenerateImagesConfig(
                    number_of_images=1,
                    output_mime_type="image/jpeg",
                    aspect_ratio="1:1",
                    person_generation="DONT_ALLOW",
                    safety_filter_level="block_medium_and_above",
                )
            )

            fondo_path = "temp_ai_background.jpg"
            for generated_image in result.generated_images:
                with open(fondo_path, "wb") as f:
                    f.write(generated_image.image.image_bytes)
                break

            # Cargar fondo generado e incrustar el producto real encima manteniendo etiquetas intactas
            fondo = Image.open(fondo_path).convert("RGBA")
            orig_path = context.user_data.get('original_photo', 'temp_user_product.jpg')
            img_prod = Image.open(orig_path).convert("RGBA")
            
            # Quitar fondo blanco del producto
            datas = img_prod.getdata()
            new_data = []
            for item in datas:
                if item[0] > 220 and item[1] > 220 and item[2] > 220:
                    new_data.append((255, 255, 255, 0))
                else:
                    new_data.append(item)
            img_prod.putdata(new_data)
            bbox = img_prod.getbbox()
            if bbox:
                img_prod = img_prod.crop(bbox)

            # Redimensionar producto proporcionalmente (70% del alto total)
            ancho, alto = fondo.size
            target_h = int(alto * 0.70)
            aspect = img_prod.width / img_prod.height
            target_w = int(target_h * aspect)
            img_prod = img_prod.resize((target_w, target_h), Image.Resampling.LANCZOS)

            # Posicionar centrado abajo sobre la superficie
            x_offset = (ancho - target_w) // 2
            y_offset = alto - target_h - int(alto * 0.10)

            fondo.paste(img_prod, (x_offset, y_offset), img_prod)
            
            final_path = "temp_final_lifestyle.jpg"
            fondo.convert("RGB").save(final_path, quality=95)
            context.user_data['pending_photo'] = final_path

            kb_aprob = [
                [InlineKeyboardButton("✅ Aprobar y Guardar en Drive", callback_data="save_drive_escena")],
                [InlineKeyboardButton("🔄 Volver al Menú", callback_data="cancelar")]
            ]
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=open(final_path, 'rb'),
                caption="✨ **¡Escena lifestyle con fondo IA generada!**\n*(Fondo fotorrealista profesional + etiquetas 100% intactas)*\n\n¿Qué deseas hacer?",
                reply_markup=InlineKeyboardMarkup(kb_aprob)
            )
        except Exception as e:
            await context.bot.send_message(chat_id=chat_id, text=f"Error generando el fondo: {str(e)}")

    elif data.startswith("save_web_"):
        idx = int(data.replace("save_web_", ""))
        urls = context.user_data.get('web_urls', [])
        if idx < len(urls):
            await query.edit_message_caption(caption="☁️ Guardando imagen web en Google Drive...")
            try:
                img_data = requests.get(urls[idx]).content
                local_f = "temp_web.jpg"
                with open(local_f, 'wb') as f:
                    f.write(img_data)
                
                drive = init_drive()
                if drive:
                    file_d = drive.CreateFile({'title': 'vilo_web_product.jpg'})
                    file_d.SetContentFile(local_f)
                    file_d.Upload()
                    await context.bot.send_message(chat_id=chat_id, text="¡Imagen guardada con éxito en Google Drive! 🚀")
                else:
                    await context.bot.send_message(chat_id=chat_id, text="Error conectando con Drive.")
            except Exception as e:
                await context.bot.send_message(chat_id=chat_id, text=f"Error: {str(e)}")

    elif data == "save_drive_escena":
        await query.edit_message_caption(caption="☁️ Subiendo a Google Drive...")
        try:
            drive = init_drive()
            path = context.user_data.get('pending_photo', 'temp_final_lifestyle.jpg')
            if drive and os.path.exists(path):
                file_d = drive.CreateFile({'title': 'vilo_lifestyle_ia.jpg'})
                file_d.SetContentFile(path)
                file_d.Upload()
                await context.bot.send_message(chat_id=chat_id, text="¡Guardado con éxito en Google Drive! 🚀")
            else:
                await context.bot.send_message(chat_id=chat_id, text="Error conectando con Drive.")
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
    
    print("Bot de Vilo Aromas con fondos IA profesionales en marcha!")
    app.run_polling()
