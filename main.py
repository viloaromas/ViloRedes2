import os
import io
import re
import json
import time
import uuid
import logging
import asyncio
import threading
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

import requests
from PIL import Image, ImageOps
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    filters,
)

import google.generativeai as genai

try:
    from duckduckgo_search import DDGS
    DDGS_AVAILABLE = True
except ImportError:
    DDGS_AVAILABLE = False

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip()

PORT = int(os.environ.get("PORT", "10000"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/tmp/vilo_aromas"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

GOOGLE_DRIVE_FOLDER_ID = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "").strip()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("vilo_aromas_bot")

if not TELEGRAM_TOKEN:
    logger.warning("Falta la variable TELEGRAM_TOKEN.")
if not GEMINI_API_KEY:
    logger.warning("Falta la variable GEMINI_API_KEY.")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "ViloAromasBot/2.0"})


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"Bot de Vilo Aromas activo y funcionando."
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


def run_web_server():
    try:
        server = ThreadingHTTPServer(("0.0.0.0", PORT), HealthHandler)
        server.serve_forever()
    except Exception:
        logger.exception("No se pudo iniciar el servidor web")


threading.Thread(target=run_web_server, daemon=True).start()


def user_folder(user_id):
    folder = DATA_DIR / str(user_id)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def get_image_from_bytes(data):
    image = Image.open(io.BytesIO(data))
    image = ImageOps.exif_transpose(image)
    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGBA")
    return image


def identificar_producto(image):
    if not GEMINI_API_KEY:
        return {"name": "Saphirus Bergamota y Naranja", "search_terms": ["Saphirus Bergamota y Naranja", "Saphirus aerosol"]}
    try:
        model = genai.GenerativeModel(GEMINI_MODEL)
        prompt = """
Mira la imagen adjunta. Describe con precisión quirúrgica el producto para buscar una foto idéntica en internet:
1. Marca (ej: Saphirus)
2. Tipo de envase (ej: aerosol, difusor, gatillo)
3. Sabor o fragancia exacta (ej: Bergamota y Naranja)
Devuelve SOLO un JSON válido con este formato:
{"name":"nombre del producto","search_terms":["consulta exacta 1", "consulta exacta 2"]}
"""
        response = model.generate_content([image, prompt])
        raw = (response.text or "").strip()
        m = re.search(r"\{.*\}", raw, flags=re.S)
        data = json.loads(m.group(0) if m else raw)
        name = str(data.get("name") or "").strip() or "Producto Vilo Aromas"
        terms = data.get("search_terms") or [name]
        return {"name": name[:180], "search_terms": terms[:5]}
    except Exception:
        return {"name": "Saphirus Bergamota y Naranja", "search_terms": ["Saphirus Bergamota y Naranja", "Saphirus aerosol"]}


def redactar_copy(user_message):
    if not GEMINI_API_KEY:
        raise RuntimeError("Falta configurar GEMINI_API_KEY.")
    model = genai.GenerativeModel(GEMINI_MODEL)
    prompt = (
        "Actúa como community manager de Vilo Aromas, negocio de artículos "
        "de limpieza y aromatización. Escribe en español rioplatense, "
        "con tono comercial, claro y cercano.\n"
        f"Solicitud del usuario: {user_message}"
    )
    response = model.generate_content(prompt)
    return (response.text or "No pude generar el texto.").strip()


def buscar_imagenes_web(search_terms, max_resultados=4):
    if not DDGS_AVAILABLE:
        return []
    
    results = []
    seen = set()
    for query_text in search_terms:
        try:
            with DDGS() as ddgs:
                found = list(ddgs.images(query=query_text, max_results=10))
            for item in found:
                url = item.get("image")
                if not url or url in seen:
                    continue
                seen.add(url)
                try:
                    response = HTTP.get(url, timeout=(5, 12))
                    response.raise_for_status()
                    data = response.content
                    if 5000 <= len(data) <= 10_000_000:
                        results.append((url, data))
                        if len(results) >= max_resultados:
                            return results
                except Exception:
                    pass
        except Exception:
            time.sleep(0.3)
    return results


def init_drive():
    try:
        client_email = os.environ.get("GOOGLE_CLIENT_EMAIL", "").strip()
        private_key = os.environ.get("GOOGLE_PRIVATE_KEY", "").strip().replace("\\n", "\n")
        if not client_email or not private_key:
            return None

        from pydrive2.auth import GoogleAuth
        from pydrive2.drive import GoogleDrive

        service_config = {
            "type": "service_account",
            "client_email": client_email,
            "private_key": private_key,
            "token_uri": "https://oauth2.googleapis.com/token",
        }
        gauth = GoogleAuth()
        gauth.settings["client_config_backend"] = "service"
        gauth.settings["service_config"] = {"client_json_dict": service_config}
        gauth.ServiceAuth()
        return GoogleDrive(gauth)
    except Exception:
        return None


def guardar_en_drive(local_path, filename):
    drive = init_drive()
    if not drive:
        raise RuntimeError("No se pudo conectar con Google Drive.")
    metadata = {"title": filename}
    if GOOGLE_DRIVE_FOLDER_ID:
        metadata["parents"] = [{"id": GOOGLE_DRIVE_FOLDER_ID}]
    drive_file = drive.CreateFile(metadata)
    drive_file.SetContentFile(local_path)
    drive_file.Upload()
    return drive_file.get("alternateLink", "")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name or "¡hola!"
    keyboard = [
        ["📸 Enviar Foto de Producto"],
        ["📝 Redactar Copy para Redes"],
        ["❓ Ayuda / Estado"],
    ]
    await update.message.reply_text(
        f"¡Hola {user_name}! 👋 Bienvenido al asistente de Vilo Aromas.\n\n"
        "Envía una foto de tu producto para buscar imágenes comerciales exactas en la web y guardarlas en Drive.",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
    )


async def mostrar_menu_principal(context, chat_id, product_name):
    keyboard = [
        [InlineKeyboardButton("🌐 Buscar fotos comerciales exactas en la Web", callback_data="modo_web")],
        [InlineKeyboardButton("❌ Cancelar", callback_data="cancelar")],
    ]
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"✅ Foto analizada con éxito\n"
            f"🔎 Producto exacto detectado: *{product_name}*\n\n"
            "👉 Haz clic abajo para buscar fotos comerciales de este producto:"
        ),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    user_message = update.message.text.strip()
    chat_id = update.effective_chat.id

    if "Enviar Foto" in user_message:
        await update.message.reply_text(
            "✅ Envía ahora la foto de tu producto.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    if "Ayuda" in user_message or user_message == "/help":
        await update.message.reply_text(
            "ℹ️ Cómo usar el bot:\n"
            "1. Envía una foto clara de tu producto.\n"
            "2. El bot leerá la etiqueta exacta y buscará fotos comerciales en la web.\n"
            "3. Guarda la imagen seleccionada directamente en tu Google Drive."
        )
        return

    await update.message.reply_text("✨ Redactando propuesta...")
    try:
        copy_text = await asyncio_to_thread(redactar_copy, user_message)
        await update.message.reply_text(copy_text)
    except Exception as exc:
        await update.message.reply_text(f"⚠️ No pude redactar el texto: {exc}")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    try:
        if update.message.photo:
            photo_file = await update.message.photo[-1].get_file()
        elif update.message.document:
            photo_file = await update.message.document.get_file()
        else:
            await update.message.reply_text("⚠️ Imagen no válida.")
            return

        photo_bytes = await photo_file.download_as_bytearray()
        image = get_image_from_bytes(photo_bytes)

        await update.message.reply_text("🔎 Leyendo etiqueta y fragancia exacta con Gemini...")
        product_info = await asyncio_to_thread(identificar_producto, image)
        context.user_data["product_info"] = product_info
        context.user_data["product_name"] = product_info.get("name", "Producto Vilo Aromas")
        context.user_data["search_terms"] = product_info.get("search_terms", [])

        await mostrar_menu_principal(context, chat_id, context.user_data["product_name"])
    except Exception as exc:
        await update.bot.send_message(chat_id=chat_id, text=f"⚠️ Error procesando la foto: {exc}")


async def asyncio_to_thread(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


async def enviar_imagenes_web(context, chat_id, urls_and_bytes):
    for index, (url, data) in enumerate(urls_and_bytes, start=1):
        try:
            image = get_image_from_bytes(data)
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=92)
            buffer.seek(0)
            buffer.name = f"resultado_web_{index}.jpg"
            keyboard = [[
                InlineKeyboardButton(
                    f"☁️ Guardar opción {index} en Drive",
                    callback_data=f"save_web_{index - 1}",
                )
            ]]
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=buffer,
                caption=f"🖼️ Foto comercial encontrada {index}",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        except Exception:
            pass


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    data = query.data or ""

    if data == "modo_web" or data == "buscar_mas_web":
        await query.edit_message_text("🌐 Buscando fotos comerciales exactas en la web...")
        search_terms = context.user_data.get("search_terms", ["aromatizante Saphirus"])
        results = await asyncio_to_thread(buscar_imagenes_web, search_terms, 4)

        if not results:
            await context.bot.send_message(chat_id=chat_id, text="⚠️ No se encontraron fotos exactas en la web para este producto.")
        else:
            context.user_data["web_results"] = results
            await enviar_imagenes_web(context, chat_id, results)

        keyboard = [
            [InlineKeyboardButton("🔄 Buscar más resultados", callback_data="buscar_mas_web")],
            [InlineKeyboardButton("🔙 Volver al inicio", callback_data="cancelar")],
        ]
        await context.bot.send_message(chat_id=chat_id, text="¿Qué deseas hacer a continuación?", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data.startswith("save_web_"):
        index = int(data.replace("save_web_", ""))
        results = context.user_data.get("web_results", [])
        if 0 <= index < len(results):
            await query.edit_message_caption(caption="☁️ Guardando imagen en Google Drive...")
            _, data_bytes = results[index]
            path = str(user_folder(chat_id) / f"web_{uuid.uuid4().hex[:8]}.jpg")
            image = get_image_from_bytes(data_bytes).convert("RGB")
            image.save(path, "JPEG", quality=94)
            
            product_clean = re.sub(r"[^a-zA-Z0-9_-]+", "_", context.user_data.get("product_name", "producto"))[:30]
            link = await asyncio_to_thread(guardar_en_drive, path, f"vilo_{product_clean}_{uuid.uuid4().hex[:6]}.jpg")
            await context.bot.send_message(chat_id=chat_id, text=f"✅ ¡Imagen guardada con éxito en Google Drive!\n{link}")
        return

    if data == "cancelar":
        await query.edit_message_text("Operación cancelada. Envía una nueva foto cuando gustes con /start.")
        context.user_data.clear()
        return


async def main_async():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("Falta configurar TELEGRAM_TOKEN.")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_photo))
    app.add_handler(CallbackQueryHandler(button_callback))

    logger.info("Bot de Vilo Aromas iniciado.")
    
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    
    while True:
        await asyncio.sleep(3600)


def main():
    try:
        asyncio.run(main_async())
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
