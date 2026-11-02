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

def generar_ideas_personalizadas(image, product_name):
    try:
        prompt = (
            f"Actúa como director de arte publicitario para 'Vilo Aromas'. "
            f"Para este producto detectado: '{product_name}', genera exactamente 3 ideas cortas, "
            f"creativas y atractivas de fondos fotográficos comerciales lifestyle (ej: tipo de entorno, iluminación, elementos decorativos). "
            f"Devuelve únicamente las 3 ideas separadas por líneas, numeradas del 1 al 3, sin introducciones ni despedidas."
        )
        response = model.generate_content([image, prompt])
        lineas = [l.strip() for l in response.text.strip().split('\n') if l.strip()]
        # Limpiar numeración si la trae
        ideas = []
        for l in lineas:
            for prefix in ["1.", "2.", "3.", "1-", "2-", "3-", "•"]:
                if l.startswith(prefix):
                    l = l[len(prefix):].strip()
            if l:
                ideas.append(l)
        if len(ideas) < 3:
            ideas = [
                "Entorno botánico natural con hojas verdes y luz suave de ventana",
                "Superficie de mármol blanco elegante con iluminación cálida de estudio",
                "Espacio moderno y luminoso con detalles minimalistas"
            ]
        return ideas[:3]
    except Exception:
        return [
            "Entorno botánico natural con hojas verdes y luz suave de ventana",
            "Superficie de mármol blanco elegante con iluminación cálida de estudio",
            "Espacio moderno y luminoso con detalles minimalistas"
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
        await context.bot.send_message(chat_id=chat_id, text="ℹ️ Envía tu foto para que la IA analice el producto y te proponga ideas de fondos profesionales a medida.")
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

        prompt_id = "Identifica la marca exacta, línea y fragancia o tipo de este producto (ej: Iluminarte Mango Maracuyá). Escribe únicamente el nombre exacto sin explicaciones."
        response = model.generate_content([image, prompt_id])
        product_name = response.text.strip()
        context.user_data['product_name'] = product_name

        # Generar 3 ideas personalizadas basadas en la foto
        await context.bot.send_message(chat_id=chat_id, text=f"🔎 Producto detectado: *{product_name}*\n🧠 Analizando y diseñando ideas creativas de fondos...", parse_mode="Markdown")
        
        ideas = generar_ideas_personalizadas(image, product_name)
        context.user_data['current_ideas'] = ideas

        await mostrar_menu_ideas(context, chat_id, product_name, ideas)

    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"Error procesando la foto: {str(e)}")

async def mostrar_menu_ideas(context, chat_id, product_name, ideas):
    keyboard = []
    for idx, idea in enumerate(ideas):
        # Recortar texto si es muy largo para el botón
        texto_boton = f"💡 {idea[:45]}..." if len(idea) > 48 else f"💡 {idea}"
        keyboard.append([InlineKeyboardButton(texto_boton, callback_data=f"idea_{idx}")])
    
    keyboard.append([InlineKeyboardButton("🔄 ¡Más ideas diferentes!", callback_data="mas_ideas")])
    keyboard.append([InlineKeyboardButton("❌ Cancelar", callback_data="cancelar")])

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"✨ **Ideas de diseño para {product_name}:**\nSelecciona la que más te guste o pide más alternativas:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    data = query.data

    if data == "mas_ideas":
        await query.edit_message_text(text="🔄 Generando nuevas ideas creativas...")
        orig_path = context.user_data.get('original_photo', 'temp_user_product.jpg')
        product_name = context.user_data.get('product_name', 'Producto')
        try:
            image = Image.open(orig_path)
            ideas = generar_ideas_personalizadas(image, product_name)
            context.user_data['current_ideas'] = ideas
            await mostrar_menu_ideas(context, chat_id, product_name, ideas)
        except Exception as e:
            await context.bot.send_message(chat_id=chat_id, text=f"Error generando ideas: {str(e)}")

    elif data.startswith("idea_"):
        idx = int(data.replace("idea_", ""))
        ideas = context.user_data.get('current_ideas', [])
        idea_elegida = ideas[idx] if idx < len(ideas) else "Entorno profesional elegante"

        await query.edit_message_text(text=f"🎨 Creando escena profesional basada en:\n_{idea_elegida}_", parse_mode="Markdown")
        
        try:
            # Seleccionar una imagen de fondo según el concepto o aleatoria de alta gama
            fondos_pool = [
                "https://images.unsplash.com/photo-1540555700478-4be289fbecef?q=80&w=1080&auto=format&fit=crop",
                "https://images.unsplash.com/photo-1513519245088-0e12902e5a38?q=80&w=1080&auto=format&fit=crop",
                "https://images.unsplash.com/photo-1586023492125-27b2c045efd7?q=80&w=1080&auto=format&fit=crop",
                "https://images.unsplash.com/photo-1600585154340-be6161a56a0c?q=80&w=1080&auto=format&fit=crop",
                "https://images.unsplash.com/photo-1618221195710-dd6b41faaea6?q=80&w=1080&auto=format&fit=crop"
            ]
            # Elegir fondo basado en el índice o rotar
            bg_url = fondos_pool[idx % len(fondos_pool)]
            response_bg = requests.get(bg_url)
            fondo = Image.open(io.BytesIO(response_bg.content)).convert("RGBA")
            fondo = fondo.resize((1080, 1080), Image.Resampling.LANCZOS)

            orig_path = context.user_data.get('original_photo', 'temp_user_product.jpg')
            img_prod = Image.open(orig_path).convert("RGBA")
            
            # Limpieza precisa del fondo blanco (umbral 248)
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
                [InlineKeyboardButton("🔄 Ver otras ideas", callback_data="mas_ideas")],
                [InlineKeyboardButton("❌ Cancelar", callback_data="cancelar")]
            ]
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=open(final_path, 'rb'),
                caption=f"✨ **¡Escena generada con éxito!**\n*Idea:* _{idea_elegida}_\n\n¿Qué deseas hacer?",
                reply_markup=InlineKeyboardMarkup(kb_aprob),
                parse_mode="Markdown"
            )
        except Exception as e:
            await context.bot.send_message(chat_id=chat_id, text=f"Error creando la escena: {str(e)}")

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
    
    print("Bot de Vilo Aromas con generación de ideas dinámicas operativo!")
    app.run_polling(drop_pending_updates=True)
