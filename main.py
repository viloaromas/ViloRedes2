import os
import io
import re
import json
import time
import uuid
import random
import logging
import asyncio
import threading
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

import requests
from PIL import Image, ImageOps, ImageFilter
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

# Fondos profesionales con superficies de apoyo claras (mesas, encimeras, muebles bajos)
BACKGROUND_CATALOG = {
    "lifestyle": [
        "https://images.unsplash.com/photo-1600210492486-724fe5c67fb0?w=1600&q=90&fit=crop",
        "https://images.unsplash.com/photo-1618221195710-dd6b41faaea6?w=1600&q=90&fit=crop",
    ],
    "premium": [
        "https://images.unsplash.com/photo-1600607687920-4e2a09cf159d?w=1600&q=90&fit=crop",
        "https://images.unsplash.com/photo-1600585154340-be6161a56a0c?w=1600&q=90&fit=crop",
    ],
    "bano": ["https://images.unsplash.com/photo-1620626011761-996317b8d101?w=1600&q=90&fit=crop"],
    "dormitorio": ["https://images.unsplash.com/photo-1616486338812-3dadae4b4ace?w=1600&q=90&fit=crop"],
    "natural": ["https://images.unsplash.com/photo-1494438639946-1ebd1d20bf85?w=1600&q=90&fit=crop"],
}

STYLE_LABELS = {
    "lifestyle": "Lifestyle: living / hogar",
    "bano": "Baño elegante / spa",
    "dormitorio": "Dormitorio cálido",
    "natural": "Natural / botánico",
    "premium": "Premium / mármol y detalles finos",
    "blanco": "Fondo blanco limpio",
}

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


def safe_filename(value):
    value = re.sub(r"[^a-zA-Z0-9_-]+", "_", value or "producto")
    return value[:50].strip("_") or "producto"


def get_image_from_bytes(data):
    image = Image.open(io.BytesIO(data))
    image = ImageOps.exif_transpose(image)
    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGBA")
    return image


def save_original_image(image, user_id):
    path = user_folder(user_id) / f"original_{uuid.uuid4().hex[:8]}.png"
    image.save(path, format="PNG")
    return str(path)


def identificar_producto(image):
    if not GEMINI_API_KEY:
        return {"name": "Producto Vilo Aromas", "search_terms": ["vilo aromas"]}
    try:
        model = genai.GenerativeModel(GEMINI_MODEL)
        prompt = """
Analiza la fotografía y lee el envase. Devuelve SOLO JSON válido:
{"name":"nombre exacto visible","search_terms":["3 consultas para buscar fotos"]}
"""
        response = model.generate_content([image, prompt])
        raw = (response.text or "").strip()
        m = re.search(r"\{.*\}", raw, flags=re.S)
        data = json.loads(m.group(0) if m else raw)
        name = str(data.get("name") or "").strip() or "Producto Vilo Aromas"
        terms = data.get("search_terms") or [name]
        return {"name": name[:180], "search_terms": terms[:5]}
    except Exception:
        return {"name": "Producto Vilo Aromas", "search_terms": ["vilo aromas"]}


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


def buscar_imagenes_web(nombre_producto, max_resultados=4, search_terms=None):
    if not DDGS_AVAILABLE:
        return []
    queries = []
    for term in (search_terms or []):
        term = str(term).strip()
        if term and term not in queries:
            queries.append(term)
    if nombre_producto:
        queries.append(nombre_producto)

    results = []
    seen = set()
    for query_text in queries[:5]:
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
            time.sleep(0.5)
    return results


def remove_background_pure(product_image):
    source = product_image.convert("RGBA")
    datas = source.getdata()
    new_data = []
    for item in datas:
        if item[0] > 242 and item[1] > 242 and item[2] > 242:
            new_data.append((255, 255, 255, 0))
        else:
            new_data.append(item)
    source.putdata(new_data)
    bbox = source.getbbox()
    if not bbox:
        return source
    return source.crop(bbox)


def download_background(url):
    response = HTTP.get(url, timeout=(6, 15))
    response.raise_for_status()
    image = Image.open(io.BytesIO(response.content))
    return ImageOps.exif_transpose(image).convert("RGB")


def obtener_fondo(style, size=(1080, 1080)):
    if style == "blanco":
        return Image.new("RGB", size, (250, 250, 250)).convert("RGBA")
    
    background = None
    urls = BACKGROUND_CATALOG.get(style, BACKGROUND_CATALOG.get("lifestyle", []))
    for url in (random.sample(urls, len(urls)) if urls else []):
        try:
            background = download_background(url)
            break
        except Exception:
            pass

    if background is None:
        background = Image.new("RGB", size, (239, 236, 229))

    return background.resize(size, Image.Resampling.LANCZOS).convert("RGBA")


def compose_product_scene(original_image, style):
    product = remove_background_pure(original_image)
    canvas_size = (1080, 1080)
    background = obtener_fondo(style, canvas_size)

    # Escalar proporcionalmente para que el producto tenga un tamaño comercial armónico
    max_height = 620 if style != "blanco" else 750
    max_width = 520
    scale = min(max_width / product.width, max_height / product.height, 1.0)
    new_size = (
        max(1, int(product.width * scale)),
        max(1, int(product.height * scale)),
    )
    product = product.resize(new_size, Image.Resampling.LANCZOS)

    # Generar sombra de contacto realista bajo el producto
    alpha = product.getchannel("A")
    shadow_alpha = alpha.filter(ImageFilter.GaussianBlur(16))
    shadow_alpha = shadow_alpha.point(lambda p: int(p * 0.35))
    shadow = Image.new("RGBA", product.size, (0, 0, 0, 0))
    shadow.putalpha(shadow_alpha)

    # POSICIONAMIENTO LÓGICO: Ubicado firmemente apoyado sobre la superficie inferior de la escena
    x = (canvas_size[0] - product.width) // 2
    # El eje Y se ajusta para que la base del envase apoye contra el tercio inferior del fondo (zona de muebles/mesas)
    y = canvas_size[1] - product.height - 130

    shadow_layer = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    # Sombra proyectada detrás y debajo de la base del producto
    shadow_layer.alpha_composite(shadow, (x + 12, y + product.height - 25))
    background = Image.alpha_composite(background, shadow_layer)
    background.alpha_composite(product, (x, y))

    return background.convert("RGB")


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
        ["📝 Redactar Copy (Story / Reel / Post)"],
        ["❓ Ayuda / Estado"],
    ]
    await update.message.reply_text(
        f"¡Hola {user_name}! 👋 Bienvenido al asistente de Vilo Aromas.\n\n"
        "Podés enviarme una foto de producto o pedirme un texto para tus redes.",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
    )


async def mostrar_menu_principal(context, chat_id, product_name):
    keyboard = [
        [InlineKeyboardButton("🌐 Buscar fotos de ESTE producto en la Web", callback_data="modo_web")],
        [InlineKeyboardButton("🏡 Living / hogar", callback_data="estilo_lifestyle")],
        [InlineKeyboardButton("🛁 Baño elegante / spa", callback_data="estilo_bano")],
        [InlineKeyboardButton("🛏️ Dormitorio cálido", callback_data="estilo_dormitorio")],
        [InlineKeyboardButton("🌿 Natural / botánico", callback_data="estilo_natural")],
        [InlineKeyboardButton("✨ Premium / mármol", callback_data="estilo_premium")],
        [InlineKeyboardButton("⬜ Fondo blanco", callback_data="estilo_blanco")],
        [InlineKeyboardButton("❌ Cancelar", callback_data="cancelar")],
    ]
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"✅ Foto recibida y analizada\n"
            f"🔎 Producto detectado: {product_name}\n\n"
            "Elegí qué acción querés realizar:"
        ),
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    user_message = update.message.text.strip()
    chat_id = update.effective_chat.id

    if "Enviar Foto" in user_message:
        await update.message.reply_text(
            "✅ Enviá ahora la foto de tu producto.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    if "Ayuda" in user_message or user_message == "/help":
        await update.message.reply_text(
            "ℹ️ Cómo usar el bot:\n"
            "1. Tocá Enviar Foto de Producto.\n"
            "2. Mandá una foto nítida de tu envase con fondo claro.\n"
            "3. Elegí buscar fotos en la web o armar una escena lifestyle profesional."
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
    user_id = update.effective_user.id

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
        original_path = save_original_image(image, user_id)
        context.user_data["original_photo"] = original_path

        await update.message.reply_text("🔎 Analizando el producto...")
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
                caption=f"🖼️ Resultado web {index}",
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
        await query.edit_message_text("🌐 Buscando imágenes comerciales en la web...")
        product_name = context.user_data.get("product_name", "aromatizante")
        search_terms = context.user_data.get("search_terms", [])
        results = await asyncio_to_thread(buscar_imagenes_web, product_name, 4, search_terms)

        if not results:
            await context.bot.send_message(chat_id=chat_id, text="⚠️ No se encontraron fotos web para este producto.")
        else:
            context.user_data["web_results"] = results
            await enviar_imagenes_web(context, chat_id, results)

        keyboard = [
            [InlineKeyboardButton("🔄 Buscar más resultados", callback_data="buscar_mas_web")],
            [InlineKeyboardButton("🔙 Volver al menú", callback_data="volver_menu")],
        ]
        await context.bot.send_message(chat_id=chat_id, text="¿Qué querés hacer ahora?", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data.startswith("estilo_"):
        style = data.replace("estilo_", "", 1)
        original_path = context.user_data.get("original_photo")
        if not original_path or not os.path.exists(original_path):
            await query.edit_message_text("⚠️ No encuentro la foto original. Enviá nuevamente el producto.")
            return

        context.user_data["last_style"] = style
        await query.edit_message_text(f"🎨 Preparando {STYLE_LABELS.get(style, 'escena')}...")
        try:
            original = Image.open(original_path).convert("RGBA")
            final_image = await asyncio_to_thread(compose_product_scene, original, style)
            final_path = str(user_folder(update.effective_user.id) / f"vilo_{style}_{uuid.uuid4().hex[:8]}.jpg")
            final_image.save(final_path, "JPEG", quality=95, optimize=True)
            context.user_data["pending_photo"] = final_path

            keyboard = [
                [InlineKeyboardButton("✅ Aprobar y guardar en Drive", callback_data="save_drive_escena")],
                [InlineKeyboardButton("🔄 Probar otro estilo", callback_data="volver_menu")],
                [InlineKeyboardButton("❌ Cancelar", callback_data="cancelar")],
            ]
            with open(final_path, "rb") as photo:
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=photo,
                    caption=f"✨ Escena estilo {STYLE_LABELS.get(style, '')} lista.",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
        except Exception as exc:
            await context.bot.send_message(chat_id=chat_id, text=f"⚠️ No pude crear la composición: {exc}")
        return

    if data == "volver_menu":
        product_name = context.user_data.get("product_name", "Producto Vilo Aromas")
        await mostrar_menu_principal(context, chat_id, product_name)
        return

    if data.startswith("save_web_"):
        index = int(data.replace("save_web_", ""))
        results = context.user_data.get("web_results", [])
        if 0 <= index < len(results):
            await query.edit_message_caption(caption="☁️ Guardando imagen web en Google Drive...")
            _, data_bytes = results[index]
            path = str(user_folder(update.effective_user.id) / f"web_{uuid.uuid4().hex[:8]}.jpg")
            image = get_image_from_bytes(data_bytes).convert("RGB")
            image.save(path, "JPEG", quality=94)
            link = await asyncio_to_thread(guardar_en_drive, path, f"vilo_web_{uuid.uuid4().hex[:6]}.jpg")
            await context.bot.send_message(chat_id=chat_id, text=f"✅ Imagen guardada en Google Drive.\n{link}")
        return

    if data == "save_drive_escena":
        path = context.user_data.get("pending_photo")
        if path and os.path.exists(path):
            await query.edit_message_caption(caption="☁️ Subiendo escena a Google Drive...")
            product = safe_filename(context.user_data.get("product_name", "producto"))
            style = safe_filename(context.user_data.get("last_style", "vilo"))
            link = await asyncio_to_thread(guardar_en_drive, path, f"vilo_{product}_{style}_{uuid.uuid4().hex[:6]}.jpg")
            await context.bot.send_message(chat_id=chat_id, text=f"🚀 Imagen guardada en Google Drive.\n{link}")
        return

    if data == "cancelar":
        await query.edit_message_text("Operación cancelada. Podés comenzar nuevamente con /start.")
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
