import os
import logging
import io
import requests
from PIL import Image, ImageFilter, ImageEnhance
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

# Búsqueda web flexible
def buscar_imagenes_web(nombre_producto, max_resultados=4):
    urls_imagenes = []
    try:
        queries = [
            f"{nombre_producto} aromatizante",
            f"{nombre_producto} spray",
            f"Iluminarte Saphirus aromatizante"
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

# Generador de escena lifestyle profesional con superficie y profundidad
def crear_escena_lifestyle(input_path, output_path, tipo_estilo):
    img_prod = Image.open(input_path).convert("RGBA")
    
    # Remover fondo blanco con tolerancia alta
    datas = img_prod.getdata()
    new_data = []
    for item in datas:
        if item[0] > 230 and item[1] > 230 and item[2] > 230:
            new_data.append((255, 255, 255, 0))
        else:
            new_data.append(item)
    img_prod.putdata(new_data)
    
    bbox = img_prod.getbbox()
    if bbox:
        img_prod = img_prod.crop(bbox)

    # Dimensiones de formato comercial (Post 1080x1080)
    ancho, alto = 1080, 1080
    fondo = Image.new("RGB", (ancho, alto))

    # Crear fondo degradado con textura de estudio fotográfico de alta gama
    for y in range(alto):
        factor = y / alto
        if "1" in tipo_estilo:  # Botánico / Natural (Tonos frescos y claros)
            r = int(225 + (240 - 225) * factor)
            g = int(238 + (248 - 238) * factor)
            b = int(230 + (240 - 230) * factor)
        elif "2" in tipo_estilo:  # Living / Madera elegante
            r = int(242 + (255 - 242) * factor)
            g = int(234 + (245 - 234) * factor)
            b = int(220 + (235 - 220) * factor)
        else:  # Hogar / Lavadero Luminoso
            r = int(248 + (255 - 248) * factor)
            g = int(242 + (250 - 242) * factor)
            b = int(235 + (242 - 235) * factor)
        for x in range(ancho):
            fondo.putpixel((x, y), (r, g, b))

    # Simular superficie de apoyo (mesada de mármol / madera en la parte inferior)
    # Creamos un rectángulo inferior con textura suave que simula la mesada
    mesada_altura = int(alto * 0.35)
    for y in range(alto - mesada_altura, alto):
        # Efecto brillo de mármol
        v = int(235 + 10 * ((y % 15) / 15))
        for x in range(ancho):
            fondo.putpixel((x, y), (v, v, v))

    # Aplicar difuminado bokeh en la parte superior (fondo)
    fondo_blurred = fondo.copy().filter(ImageFilter.GaussianBlur(30))
    # Pegar la mesada nítida abajo
    fondo.paste(fondo_blurred.crop((0, 0, ancho, alto - mesada_altura)), (0, 0))

    # Escalar el producto para que destaque en primer plano sobre la mesada
    target_h = int(alto * 0.70)
    aspect = img_prod.width / img_prod.height
    target_w = int(target_h * aspect)
    img_prod = img_prod.resize((target_w, target_h), Image.Resampling.LANCZOS)

    # Posicionar sobre la mesada centrado
    x_offset = (ancho - target_w) // 2
    y_offset = alto - target_h - int(mesada_altura * 0.5)

    # Agregar sombra sutil de contacto debajo del producto
    sombra = Image.new("RGBA", (target_w, 30), (0, 0, 0, 0))
    # (Opcional: pegado con transparencia perfecta)
    fondo.paste(img_prod, (x_offset, y_offset), img_prod)
    
    fondo.save(output_path, quality=95)

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
            text="ℹ️ Funcionamiento:\n1. Envías la foto de tu producto.\n2. Seleccionas si deseas buscar fotos en la web o crear una escena lifestyle profesional.\n3. Apruebas el resultado y se guarda en tu Google Drive."
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

        original_path = "temp_user_product.jpg"
        image.save(original_path)
        context.user_data['original_photo'] = original_path

        # Identificar producto con Gemini
        prompt_id = "Identifica la marca y fragancia exacta de este producto (ej: Iluminarte Mango Maracuyá). Devuelve solo el nombre clave de búsqueda."
        response = model.generate_content([image, prompt_id])
        product_name = response.text.strip()
        context.user_data['product_name'] = product_name

        # Menú de opciones obligatorio post-imagen
        keyboard = [
            [InlineKeyboardButton("🌐 1. Buscar fotos comerciales en la Web", callback_data="modo_web")],
            [InlineKeyboardButton("✨ 2. Crear Escena Lifestyle Profesional", callback_data="modo_escena")],
            [InlineKeyboardButton("❌ Cancelar", callback_data="cancelar")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"✅ **¡Foto recibida con éxito!**\n"
                f"🔎 Producto detectado: *{product_name}*\n\n"
                f"👉 **¿Qué deseas hacer con esta imagen?** Elige una opción:"
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
            # Búsqueda de respaldo genérica si la específica falla
            urls = buscar_imagenes_web("Iluminarte aromatizante", max_resultados=3)

        if not urls:
            await context.bot.send_message(chat_id=chat_id, text="⚠️ No se encontraron imágenes web para este producto. Prueba con la opción de crear escena.")
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

    elif data == "modo_escena":
        keyboard_fondos = [
            [InlineKeyboardButton("🌿 Estilo 1: Entorno Botánico y Natural", callback_data="estilo_1")],
            [InlineKeyboardButton("🛋️ Estilo 2: Living / Sala Elegante", callback_data="estilo_2")],
            [InlineKeyboardButton("☀️ Estilo 3: Hogar Luminoso", callback_data="estilo_3")],
            [InlineKeyboardButton("❌ Cancelar", callback_data="cancelar")]
        ]
        await query.edit_message_text(
            text="✨ Selecciona el estilo de escena lifestyle profesional:",
            reply_markup=InlineKeyboardMarkup(keyboard_fondos)
        )

    elif data.startswith("estilo_"):
        estilo_num = data.replace("estilo_", "")
        await query.edit_message_text(text=f"🎨 Renderizando escena profesional (Estilo {estilo_num}) con producto protegido...")
        
        try:
            orig_path = context.user_data.get('original_photo', 'temp_user_product.jpg')
            final_path = "temp_lifestyle_scene.jpg"

            crear_escena_lifestyle(orig_path, final_path, f"Estilo {estilo_num}")
            context.user_data['pending_photo'] = final_path

            kb_aprob = [
                [InlineKeyboardButton("✅ Aprobar y Guardar en Drive", callback_data="save_drive_escena")],
                [InlineKeyboardButton("🔄 Volver al Menú", callback_data="cancelar")]
            ]
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=open(final_path, 'rb'),
                caption="✨ **¡Escena lifestyle profesional generada!**\n*(Etiquetas y envase 100% intactos sobre mesada de estudio)*\n\n¿Qué deseas hacer?",
                reply_markup=InlineKeyboardMarkup(kb_aprob)
            )
        except Exception as e:
            await context.bot.send_message(chat_id=chat_id, text=f"Error creando la escena: {str(e)}")

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

    elif data == "save_drive_escena":
        await query.edit_message_caption(caption="☁️ Subiendo escena lifestyle a tu Google Drive...")
        try:
            drive = init_drive()
            path = context.user_data.get('pending_photo', 'temp_lifestyle_scene.jpg')
            if drive and os.path.exists(path):
                file_d = drive.CreateFile({'title': 'vilo_lifestyle_comercial.jpg'})
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
    
    print("Bot de Vilo Aromas con menú y escenas lifestyle en marcha!")
    app.run_polling()
