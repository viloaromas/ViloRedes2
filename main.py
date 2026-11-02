import os
import logging
import io
import requests
from PIL import Image, ImageFilter
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, CallbackQueryHandler, filters
import google.generativeai as genai
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive

# Importación segura de búsqueda web para evitar cualquier caída
try:
    from duckduckgo_search import DDGS
    DDGS_AVAILABLE = True
except ImportError:
    DDGS_AVAILABLE = False

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

def buscar_imagenes_web(nombre_producto, max_resultados=3):
    urls_imagenes = []
    if not DDGS_AVAILABLE:
        return urls_imagenes
    try:
        queries = [
            f"{nombre_producto} aromatizante",
            f"{nombre_producto}"
        ]
        with DDGS() as ddgs:
            for q in queries:
                results = ddgs.images(query=q, max_results=5)
                for r in results:
                    if 'image' in r:
                        url = r['image']
                        if url not in urls_imagenes and ('http://' in url or 'https://' in url):
                            urls_imagenes.append(url)
                    if len(urls_imagenes) >= max_resultados:
                        break
                if len(urls_imagenes) >= max_resultados:
                    break
    except Exception as e:
        logging.error(f"Error en búsqueda web: {e}")
    return urls_imagenes

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
        await context.bot.send_message(chat_id=chat_id, text="ℹ️ Envía tu foto para buscar imágenes comerciales en la web o generar composiciones lifestyle profesionales.")
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

        response = model.generate_content([
            image,
            "Identifica la marca exacta, línea y tipo de este producto (ej: Aromatizante Saphirus Linah). Escribe únicamente el nombre exacto sin explicaciones."
        ])
        product_name = response.text.strip()
        context.user_data['product_name'] = product_name

        await mostrar_menu_principal(context, chat_id, product_name)

    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"Error procesando la foto: {str(e)}")

async def mostrar_menu_principal(context, chat_id, product_name):
    keyboard = [
        [InlineKeyboardButton("🌐 1. Buscar fotos comerciales en la Web", callback_data="modo_web")],
        [InlineKeyboardButton("✨ 2. Cambiar Fondo (Escenas Lifestyle Pro)", callback_data="modo_escena")],
        [InlineKeyboardButton("❌ Cancelar", callback_data="cancelar")]
    ]
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"✅ **Foto recibida y analizada**\n🔎 Producto: *{product_name}*\n\n👉 **¿Qué deseas hacer con este producto?**",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    data = query.data

    if data == "modo_web" or data == "buscar_mas_web":
        await query.edit_message_text(text="🌐 Buscando imágenes comerciales en la web...")
        product_name = context.user_data.get('product_name', 'aromatizante')
        urls = buscar_imagenes_web(product_name, max_resultados=3)

        if not urls:
            await context.bot.send_message(chat_id=chat_id, text="⚠️ No se encontraron resultados web precisos. Prueba usando la opción de cambiar fondo lifestyle.")
            return

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

        kb_control = [
            [InlineKeyboardButton("🔄 Buscar más resultados web", callback_data="buscar_mas_web")],
            [InlineKeyboardButton("🔙 Volver al menú principal", callback_data="volver_menu")]
        ]
        await context.bot.send_message(
            chat_id=chat_id,
            text="¿Qué deseas hacer a continuación?",
            reply_markup=InlineKeyboardMarkup(kb_control)
        )

    elif data == "modo_escena":
        keyboard_fondos = [
            [InlineKeyboardButton("🌿 Estilo 1: Entorno Minimalista Neutro", callback_data="estilo_1")],
            [InlineKeyboardButton("🛋️ Estilo 2: Superficie de Madera Fina", callback_data="estilo_2")],
            [InlineKeyboardButton("☀️ Estilo 3: Mármol Blanco Luminoso", callback_data="estilo_3")],
            [InlineKeyboardButton("🔙 Volver al menú principal", callback_data="volver_menu")]
        ]
        await query.edit_message_text(text="✨ Selecciona el fondo lifestyle profesional:", reply_markup=InlineKeyboardMarkup(keyboard_fondos))

    elif data == "volver_menu":
        product_name = context.user_data.get('product_name', 'Producto Vilo Aromas')
        await mostrar_menu_principal(context, chat_id, product_name)

    elif data.startswith("estilo_"):
        estilo_num = data.replace("estilo_", "")
        await query.edit_message_text(text=f"🎨 Creando escena lifestyle profesional (Estilo {estilo_num})...")
        
        try:
            fondos_urls = {
                "1": "https://images.unsplash.com/photo-1513519245088-0e12902e5a38?q=80&w=1080&auto=format&fit=crop",
                "2": "https://images.unsplash.com/photo-1586023492125-27b2c045efd7?q=80&w=1080&auto=format&fit=crop",
                "3": "https://images.unsplash.com/photo-1600585154340-be6161a56a0c?q=80&w=1080&auto=format&fit=crop"
            }
            
            bg_url = fondos_urls.get(estilo_num, fondos_urls["1"])
            response_bg = requests.get(bg_url)
            fondo = Image.open(io.BytesIO(response_bg.content)).convert("RGBA")
            fondo = fondo.resize((1080, 1080), Image.Resampling.LANCZOS)

            orig_path = context.user_data.get('original_photo', 'temp_user_product.jpg')
            img_prod = Image.open(orig_path).convert("RGBA")
            
            # Umbral 248 para eliminar únicamente el fondo blanco protegiendo el producto y etiquetas
            datas = img_prod.getdata()
            new_data = []
            for item in datas:
                if item[0] > 248 and item[1] > 248 and item[2] > 248:
                    new_data.append((255, 255, 255, 0))
                else:
                    new_data.append(item)
            img_prod.putdata(new_data)
            bbox = img_prod.getbbox()
            if bbox:
                img_prod = img_prod.crop(bbox)

            ancho, alto = fondo.size
            target_h = int(alto * 0.72)
            aspect = img_prod.width / img_prod.height
            target_w = int(target_h * aspect)
            img_prod = img_prod.resize((target_w, target_h), Image.Resampling.LANCZOS)

            x_offset = (ancho - target_w) // 2
            y_offset = alto - target_h - int(alto * 0.10)

            fondo.paste(img_prod, (x_offset, y_offset), img_prod)
            
            final_path = "temp_lifestyle_scene.jpg"
            fondo.convert("RGB").save(final_path, quality=95)
            context.user_data['pending_photo'] = final_path

            kb_aprob = [
                [InlineKeyboardButton("✅ Aprobar y Guardar en Drive", callback_data="save_drive_escena")],
                [InlineKeyboardButton("🔄 Probar otro fondo", callback_data="modo_escena")],
                [InlineKeyboardButton("🔙 Volver al menú principal", callback_data="volver_menu")]
            ]
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=open(final_path, 'rb'),
                caption="✨ **¡Escena lifestyle profesional generada!**\n*(Producto real fusionado con etiquetas y envase 100% intactos)*\n\n¿Qué deseas hacer?",
                reply_markup=InlineKeyboardMarkup(kb_aprob)
            )
        except Exception as e:
            await context.bot.send_message(chat_id=chat_id, text=f"Error creando la escena lifestyle: {str(e)}")

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
                    await context.bot.send_message(chat_id=chat_id, text="¡Imagen web guardada con éxito en Google Drive! 🚀")
                else:
                    await context.bot.send_message(chat_id=chat_id, text="Error conectando con Google Drive.")
            except Exception as e:
                await context.bot.send_message(chat_id=chat_id, text=f"Error: {str(e)}")

    elif data == "save_drive_escena":
        await query.edit_message_caption(caption="☁️ Subiendo escena a Google Drive...")
        try:
            drive = init_drive()
            path = context.user_data.get('pending_photo', 'temp_lifestyle_scene.jpg')
            if drive and os.path.exists(path):
                file_d = drive.CreateFile({'title': 'vilo_lifestyle_pro.jpg'})
                file_d.SetContentFile(path)
                file_d.Upload()
                await context.bot.send_message(chat_id=chat_id, text="¡Escena lifestyle guardada con éxito en Google Drive! 🚀")
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
    
    print("Bot de Vilo Aromas operativo con Búsqueda Web y Fondos Lifestyle!")
    app.run_polling(drop_pending_updates=True)
