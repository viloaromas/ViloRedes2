import os
import logging
import io
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

# Función avanzada: Elimina el fondo blanco y fusiona el producto de manera realista
def componer_escena_comercial(input_path, output_path, ancho, alto, tipo_opcion):
    img_original = Image.open(input_path).convert("RGBA")
    
    # 1. Convertir el fondo blanco de la foto en transparente de manera automática
    datas = img_original.getdata()
    new_data = []
    for item in datas:
        # Si el píxel es blanco o muy cercano a blanco, lo hacemos transparente
        if item[0] > 240 and item[1] > 240 and item[2] > 240:
            new_data.append((255, 255, 255, 0))
        else:
            new_data.append(item)
    img_original.putdata(new_data)

    # Recortar los bordes transparentes sobrantes para ajustar exactamente al producto
    bbox = img_original.getbbox()
    if bbox:
        img_original = img_original.crop(bbox)

    # 2. Crear un fondo ambiental completo de alta gama según la opción elegida
    fondo = Image.new("RGB", (ancho, alto))
    if "1" in tipo_opcion:  # Botánico / Natural (Tonos verdes suaves de estudio)
        fondo.paste((220, 238, 225), [0, 0, ancho, alto])
    elif "2" in tipo_opcion:  # Living / Sala Elegante (Tonos madera clara / cálidos)
        fondo.paste((242, 234, 222), [0, 0, ancho, alto])
    else:  # Hogar Luminoso (Tonos crema / ámbar suave)
        fondo.paste((250, 243, 232), [0, 0, ancho, alto])

    # Aplicar un difuminado profundo para simular profundidad de campo profesional
    fondo = fondo.filter(ImageFilter.GaussianBlur(40))

    # 3. Escalar el producto recortado para que destaque con elegancia (ej. 75% del alto total)
    target_h = int(alto * 0.75)
    aspect_ratio = img_original.width / img_original.height
    target_w = int(target_h * aspect_ratio)
    
    img_original = img_original.resize((target_w, target_h), Image.Resampling.LANCZOS)

    # 4. Calcular posición centrada inferior para que repose de forma natural
    x_offset = (ancho - target_w) // 2
    y_offset = alto - target_h - int(alto * 0.12)

    # Pegar el producto usando su propia transparencia (canal alfa) sobre el fondo difuminado
    fondo.paste(img_original, (x_offset, y_offset), img_original)
    
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
        "Selecciona 📸 Enviar Foto de Producto para elegir el formato y crear tus escenas comerciales."
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
            text="ℹ️ Funcionamiento:\n1. Elige el formato y envía tu foto de producto.\n2. La IA analizará la fragancia y te ofrecerá opciones de ambientación.\n3. Elige tu favorita para generar la escena comercial lista."
        )
        return

    if "Story" in user_message:
        context.user_data['tipo_formato'] = "Story"
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

    await context.bot.send_message(chat_id=chat_id, text="🔍 Analizando el producto y diseñando opciones de ambientación...")

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

        original_path = "temp_original.jpg"
        image.save(original_path)
        context.user_data['original_photo'] = original_path

        prompt_analisis = (
            "Analiza este producto de Vilo Aromas. "
            "Proporciona exactamente 3 opciones de escenarios comerciales atractivos "
            "(ejemplo: Opción 1: Entorno botánico y natural, Opción 2: Sala elegante con luz cálida, Opción 3: Hogar luminoso)."
        )

        response = model.generate_content([image, prompt_analisis])
        texto_analisis = response.text.strip()

        keyboard = [
            [InlineKeyboardButton("🌿 Opción 1: Entorno Botánico", callback_data="escena_1")],
            [InlineKeyboardButton("🛋️ Opción 2: Sala / Living Elegante", callback_data="escena_2")],
            [InlineKeyboardButton("☀️ Opción 3: Hogar Luminoso", callback_data="escena_3")],
            [InlineKeyboardButton("❌ Cancelar", callback_data="cancel")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"✨ **Producto analizado con éxito**\n\n"
                f"{texto_analisis}\n\n"
                f"👉 **Elige el escenario comercial que prefieres generar:**"
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
        await query.edit_message_text(text=f"🎨 Eliminando fondo blanco y generando escena integrada (Opción {opcion_num})...")

        try:
            original_path = context.user_data.get('original_photo', 'temp_original.jpg')
            ancho = context.user_data.get('ancho', 1080)
            alto = context.user_data.get('alto', 1920)
            final_path = "temp_comercial_final.jpg"

            if os.path.exists(original_path):
                componer_escena_comercial(original_path, final_path, ancho, alto, f"Opción {opcion_num}")
                context.user_data['pending_photo'] = final_path

                keyboard_aprobacion = [
                    [InlineKeyboardButton("✅ Aprobar y Guardar en Drive", callback_data="save_drive")],
                    [InlineKeyboardButton("🔄 Generar Otra Escena", callback_data="reiniciar_escena")],
                    [InlineKeyboardButton("❌ Cancelar", callback_data="cancel")]
                ]
                reply_markup_aprob = InlineKeyboardMarkup(keyboard_aprobacion)

                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=open(final_path, 'rb'),
                    caption=(
                        f"✨ **¡Escena comercial integrada con éxito!**\n"
                        f"*(Fondo blanco removido y producto fusionado)*\n\n"
                        f"¿Qué deseas hacer?"
                    ),
                    reply_markup=reply_markup_aprob
                )
            else:
                await context.bot.send_message(chat_id=chat_id, text="El archivo temporal expiró. Vuelve a enviar la foto.")
        except Exception as e:
            await context.bot.send_message(chat_id=chat_id, text=f"Error generando la escena: {str(e)}")

    elif query.data == "save_drive":
        await query.edit_message_caption(caption="☁️ Subiendo escena aprobada a tu Google Drive...")
        try:
            drive = init_drive()
            photo_path = context.user_data.get('pending_photo', 'temp_comercial_final.jpg')
            
            if drive and os.path.exists(photo_path):
                file_drive = drive.CreateFile({'title': 'vilo_escena_comercial.jpg'})
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
    
    print("Bot de Vilo Aromas con remoción inteligente de fondo en marcha!")
    app.run_polling()
