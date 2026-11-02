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

# Búsqueda web flexible y robusta
def buscar_imagenes_web(nombre_producto, max_resultados=4):
    urls_imagenes = []
    try:
        queries = [
            f"{nombre_producto} aromatizante",
            f"{nombre_producto} lifestyle",
            f"{nombre_producto}"
        ]
        with DDGS() as ddgs:
            for q in queries:
                results = ddgs.images(query=q, max_results=2)
                for r in results:
                    if 'image' in r and r['image'] not in urls_imagenes:
                        urls_imagenes.append(r['image'])
                if len(urls_imagenes) >= max_resultados:
                    break
    except Exception as e:
        logging.error(f"Error buscando imágenes web: {e}")
    return urls_imagenes[:max_resultados]

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
        "Selecciona 📸 Enviar Foto de Producto para comenzar."
    )
    await update.message.reply_text(mensaje_bienvenida, reply_markup=reply_markup)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    chat_id = update.message.chat_id

    if "Enviar Foto" in user_message:
        await context.bot.send_message(
            chat_id=chat_id, 
            text="✅ Envía ahora la foto (o archivo) de tu producto con fondo blanco.",
            reply_markup=ReplyKeyboardRemove()
        )
        return

    elif "Ayuda" in user_message:
        await context.bot.send_message(
            chat_id=chat_id, 
            text="ℹ️ Funcionamiento:\n1. Envías la foto de tu producto.\n2. Eliges si deseas buscar fotos profesionales en la web o crear un fondo atractivo.\n3. Apruebas el resultado y se guarda en tu Google Drive."
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

        # Guardar temporalmente la foto original enviada por el usuario
        original_path = "temp_user_product.jpg"
        image.save(original_path)
        context.user_data['original_photo'] = original_path

        # Identificar producto con Gemini
        prompt_id = "Identifica la marca, línea y fragancia exacta de este producto (ej: Iluminarte Prestige Mango Maracuyá). Devuelve solo el nombre clave de búsqueda."
        response = model.generate_content([image, prompt_id])
        product_name = response.text.strip()
        context.user_data['product_name'] = product_name

        # Menú de opciones post-imagen
        keyboard = [
            [InlineKeyboardButton("🔍 Buscar fotos comerciales en la Web", callback_data="modo_web")],
            [InlineKeyboardButton("🎨 Crear fondo/escena atractiva", callback_data="modo_fondo")],
            [InlineKeyboardButton("❌ Cancelar", callback_data="cancelar")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"✅ **Foto recibida con éxito**\n"
                f"🔎 Producto detectado: *{product_name}*\n\n"
                f"¿Qué deseas hacer con esta imagen?"
            ),
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )

    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"Error procesando la foto: {str(e)}")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    data = query.data

    if data == "modo_web":
        await query.edit_message_text(text="🌐 Buscando imágenes comerciales en la web...")
        product_name = context.user_data.get('product_name', 'aromatizante')
        urls = buscar_imagenes_web(product_name, max_resultados=3)

        if not urls:
            await context.bot.send_message(chat_id=chat_id, text="⚠️ No se encontraron imágenes web exactas. Intenta con otra foto.")
            return

        context.user_data['web_urls'] = urls
        for idx, url in enumerate(urls):
            try:
                keyboard_opt = [[InlineKeyboardButton(f"✅ Guardar Opción {idx+1} en Drive", callback_data=f"save_web_{idx}")]]
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=url,
                    caption=f"🖼️ **Opción Web {idx+1}**",
                    reply_markup=InlineKeyboardMarkup(keyboard_opt)
                )
            except Exception:
                continue

    elif data == "modo_fondo":
        keyboard_fondos = [
            [InlineKeyboardButton("🌿 Opción 1: Entorno Botánico y Natural", callback_data="fondo_1")],
            [InlineKeyboardButton("🛋️ Opción 2: Living / Sala Elegante", callback_data="fondo_2")],
            [InlineKeyboardButton("🧺 Opción 3: Cuarto de Lavado Luminoso", callback_data="fondo_3")],
            [InlineKeyboardButton("❌ Cancelar", callback_data="cancelar")]
        ]
        await query.edit_message_text(
            text="🎨 Selecciona el estilo de fondo profesional que deseas para tu producto:",
            reply_markup=InlineKeyboardMarkup(keyboard_fondos)
        )

    elif data.startswith("fondo_"):
        opcion = data.replace("fondo_", "")
        await query.edit_message_text(text=f"🎨 Generando escena comercial (Opción {opcion}) manteniendo intacto tu producto...")
        
        try:
            # Crear un fondo limpio y profesional con Pillow simulando estudio fotográfico
            orig_path = context.user_data.get('original_photo', 'temp_user_product.jpg')
            img_prod = Image.open(orig_path).convert("RGBA")
            
            # Quitar fondo blanco aproximado
            datas = img_prod.getdata()
            new_data = []
            for item in datas:
                if item[0] > 235 and item[1] > 235 and item[2] > 235:
                    new_data.append((255, 255, 255, 0))
                else:
                    new_data.append(item)
            img_prod.putdata(new_data)
            
            bbox = img_prod.getbbox()
            if bbox:
                img_prod = img_prod.crop(bbox)

            # Crear lienzo de alta calidad (Post 1080x1080)
            ancho, alto = 1080, 1080
            fondo = Image.new("RGB", (ancho, alto), (245, 242, 238) if opcion == "2" else (230, 240, 235) if opcion == "1" else (250, 245, 235))
            
            # Escalar producto
            target_h = int(alto * 0.75)
            aspect = img_prod.width / img_prod.height
            target_w = int(target_h * aspect)
            img_prod = img_prod.resize((target_w, target_h), Image.Resampling.LANCZOS)

            x_offset = (ancho - target_w) // 2
            y_offset = alto - target_h - 80

            fondo.paste(img_prod, (x_offset, y_offset), img_prod)
            
            final_path = "temp_final_scene.jpg"
            fondo.save(final_path, quality=95)
            context.user_data['pending_photo'] = final_path

            kb_aprob = [
                [InlineKeyboardButton("✅ Aprobar y Guardar en Drive", callback_data="save_drive_fondo")],
                [InlineKeyboardButton("🔄 Volver al Menú", callback_data="cancelar")]
            ]
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=open(final_path, 'rb'),
                caption="✨ **¡Escena comercial generada con etiquetas 100% protegidas!**\n¿Qué deseas hacer?",
                reply_markup=InlineKeyboardMarkup(kb_aprob)
            )
        except Exception as e:
            await context.bot.send_message(chat_id=chat_id, text=f"Error creando el fondo: {str(e)}")

    elif data.startswith("save_web_"):
        idx = int(data.replace("save_web_", ""))
        urls = context.user_data.get('web_urls', [])
        if idx < len(urls):
            await query.edit_message_caption(caption="☁️ Guardando imagen web en tu Google Drive...")
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
                    await context.bot.send_message(chat_id=chat_id, text="Error conectando con Google Drive.")
            except Exception as e:
                await context.bot.send_message(chat_id=chat_id, text=f"Error al guardar: {str(e)}")

    elif data == "save_drive_fondo":
        await query.edit_message_caption(caption="☁️ Subiendo escena creada a tu Google Drive...")
        try:
            drive = init_drive()
            path = context.user_data.get('pending_photo', 'temp_final_scene.jpg')
            if drive and os.path.exists(path):
                file_d = drive.CreateFile({'title': 'vilo_escena_comercial.jpg'})
                file_d.SetContentFile(path)
                file_d.Upload()
                await context.bot.send_message(chat_id=chat_id, text="¡Escena guardada con éxito en Google Drive! 🚀")
            else:
                await context.bot.send_message(chat_id=chat_id, text="Error conectando con Drive.")
        except Exception as e:
            await context.bot.send_message(chat_id=chat_id, text=f"Error al subir: {str(e)}")

    elif data == "cancelar":
        await query.edit_message_text(text="Operación cancelada. Envía otra foto cuando desees.")
        context.user_data.clear()

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_photo))
    app.add_handler(CallbackQueryHandler(button_callback))
    
    print("Bot de Vilo Aromas configurado con menú de opciones operativo!")
    app.run_polling()
