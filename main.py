import os
import io
import re
import json
import time
import uuid
import random
import logging
import threading
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor

import requests
from PIL import Image, ImageOps, ImageFilter, ImageEnhance
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

# Nuevo SDK oficial de Google Gen AI.
from google import genai

# DuckDuckGo: paquete actual (requirements.txt usa "ddgs").
try:
    from ddgs import DDGS
    DDGS_AVAILABLE = True
except ImportError:
    DDGS_AVAILABLE = False

# Segmentación local. Si no se puede cargar, el bot avisa y no destruye
# el fondo original mediante un recorte por umbral de color.
try:
    from rembg import remove, new_session
    REMBG_AVAILABLE = True
except ImportError:
    REMBG_AVAILABLE = False


# ---------------------------------------------------------------------
# CONFIGURACIÓN
# ---------------------------------------------------------------------

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip()

PORT = int(os.environ.get("PORT", "10000"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/tmp/vilo_aromas"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Opcional: API de Pexels para buscar fondos (no productos).
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "").strip()

# Opcional: carpeta de Google Drive compartida con la cuenta de servicio.
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

genai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# La sesión de rembg se crea una sola vez para evitar cargar el modelo
# en cada fotografía.
_rembg_session = None
_rembg_lock = threading.Lock()

# Fondos de respaldo. Se descargan bajo demanda y se guardan en caché local.
BACKGROUND_CATALOG = {
    "blanco": [
        "https://images.unsplash.com/photo-1602143407151-7111542de6e8?w=1400&q=90&fit=crop",
        "https://images.unsplash.com/photo-1603569283847-aa295f0d016a?w=1400&q=90&fit=crop",
    ],
    "lifestyle": [
        "https://images.unsplash.com/photo-1600210492486-724fe5c67fb0?w=1600&q=90&fit=crop",
        "https://images.unsplash.com/photo-1600607687939-ce8a6c25118c?w=1600&q=90&fit=crop",
        "https://images.unsplash.com/photo-1600566753086-00f18fb6b3ea?w=1600&q=90&fit=crop",
    ],
    "premium": [
        "https://images.unsplash.com/photo-1600607687920-4e2a09cf159d?w=1600&q=90&fit=crop",
        "https://images.unsplash.com/photo-1600607687920-4e2a09cf159d?w=1600&q=90&fit=crop",
        "https://images.unsplash.com/photo-1600585154340-be6161a56a0c?w=1600&q=90&fit=crop",
    ],
    "publicidad": [
        "https://images.unsplash.com/photo-1600607687939-ce8a6c25118c?w=1600&q=90&fit=crop",
        "https://images.unsplash.com/photo-1600210492486-724fe5c67fb0?w=1600&q=90&fit=crop",
    ],
}

STYLE_LABELS = {
    "blanco": "Catálogo fondo blanco",
    "lifestyle": "Lifestyle / ambiente real",
    "premium": "Fondo premium",
    "publicidad": "Imagen para publicidad",
}

HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "ViloAromasBot/2.0"})


# ---------------------------------------------------------------------
# SERVIDOR WEB PARA RENDER
# ---------------------------------------------------------------------

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"Bot de Vilo Aromas activo y funcionando."
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        logger.info("Health check: " + fmt, *args)


def run_web_server():
    try:
        server = ThreadingHTTPServer(("0.0.0.0", PORT), HealthHandler)
        logger.info("Servidor de salud activo en el puerto %s", PORT)
        server.serve_forever()
    except Exception:
        logger.exception("No se pudo iniciar el servidor web")


threading.Thread(target=run_web_server, daemon=True).start()


# ---------------------------------------------------------------------
# UTILIDADES DE ARCHIVOS / USUARIOS
# ---------------------------------------------------------------------

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


# ---------------------------------------------------------------------
# GEMINI: IDENTIFICACIÓN Y REDACCIÓN
# ---------------------------------------------------------------------

def identificar_producto(image):
    if not genai_client:
        return "Producto de limpieza o aromatización"
    prompt = (
        "Analiza esta fotografía de producto. Lee únicamente lo que sea visible "
        "en el envase y la etiqueta. Identifica marca, línea y fragancia si se "
        "pueden leer. No inventes datos. Devuelve una sola línea breve. "
        "Si no se distingue, responde 'Producto sin identificar'."
    )
    response = genai_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[image, prompt],
    )
    return (response.text or "Producto sin identificar").strip()[:250]


def redactar_copy(user_message):
    if not genai_client:
        raise RuntimeError("Falta configurar GEMINI_API_KEY en Render.")
    prompt = (
        "Actúa como community manager de Vilo Aromas, negocio de artículos "
        "de limpieza y productos de aromatización. Escribe en español rioplatense, "
        "con tono comercial, claro y cercano. No inventes precios, promociones, "
        "beneficios ni características que el usuario no haya indicado. "
        f"Solicitud del usuario: {user_message}"
    )
    response = genai_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
    )
    return (response.text or "No pude generar el texto.").strip()


# ---------------------------------------------------------------------
# BÚSQUEDA WEB DE IMÁGENES
# ---------------------------------------------------------------------

def buscar_imagenes_web(nombre_producto, max_resultados=4):
    """Busca imágenes, descarga y valida cada archivo antes de enviarlo."""
    if not DDGS_AVAILABLE:
        logger.warning("El paquete ddgs no está disponible.")
        return []

    queries = [
        f'"{nombre_producto}"',
        f"{nombre_producto} aromatizante",
        f"{nombre_producto} producto envase",
    ]
    resultados = []
    vistos = set()

    for query_text in queries:
        try:
            with DDGS() as ddgs:
                encontrados = ddgs.images(
                    query=query_text,
                    max_results=12,
                    safesearch="moderate",
                )
            for item in encontrados:
                url = item.get("image") or item.get("thumbnail")
                if not url or url in vistos:
                    continue
                vistos.add(url)
                try:
                    response = HTTP.get(url, timeout=(5, 15), stream=True)
                    response.raise_for_status()
                    content_type = response.headers.get("Content-Type", "").lower()
                    if "image" not in content_type:
                        response.close()
                        continue

                    data = response.content
                    response.close()
                    if len(data) < 5_000 or len(data) > 15_000_000:
                        continue

                    image = Image.open(io.BytesIO(data))
                    image.verify()
                    # Guardamos bytes originales validados.
                    resultados.append((url, data))
                    if len(resultados) >= max_resultados:
                        return resultados
                except Exception as exc:
                    logger.info("Se descartó una imagen web: %s", exc)
        except Exception as exc:
            logger.warning("Falló la búsqueda DDGS para '%s': %s", query_text, exc)
            time.sleep(0.5)

    return resultados


# ---------------------------------------------------------------------
# FONDOS Y COMPOSICIÓN: EL PRODUCTO NO SE REGENERA CON IA
# ---------------------------------------------------------------------

def get_rembg_session():
    global _rembg_session
    if not REMBG_AVAILABLE:
        return None
    with _rembg_lock:
        if _rembg_session is None:
            _rembg_session = new_session("u2net")
    return _rembg_session


def remove_background(product_image):
    """
    Segmentación del producto. No usa un umbral blanco que pueda borrar
    etiquetas o partes del envase. Devuelve RGBA.
    """
    if not REMBG_AVAILABLE:
        raise RuntimeError(
            "No está disponible rembg. Verificá la instalación de rembg[cpu] "
            "en requirements.txt y volvé a desplegar."
        )

    source = product_image.convert("RGBA")
    result = remove(
        source,
        session=get_rembg_session(),
        alpha_matting=False,
    )
    cutout = result.convert("RGBA")

    alpha = cutout.getchannel("A")
    bbox = alpha.getbbox()
    if not bbox:
        raise RuntimeError("No se pudo detectar el producto en la fotografía.")

    # Un pequeño margen evita cortar bordes, gatillos o tapas.
    left, top, right, bottom = bbox
    margin_x = max(8, int((right - left) * 0.025))
    margin_y = max(8, int((bottom - top) * 0.025))
    bbox = (
        max(0, left - margin_x),
        max(0, top - margin_y),
        min(cutout.width, right + margin_x),
        min(cutout.height, bottom + margin_y),
    )
    return cutout.crop(bbox)


def download_background(url):
    response = HTTP.get(url, timeout=(8, 25))
    response.raise_for_status()
    if len(response.content) > 20_000_000:
        raise ValueError("El fondo descargado es demasiado grande.")
    image = Image.open(io.BytesIO(response.content))
    return ImageOps.exif_transpose(image).convert("RGB")


def buscar_fondo_pexels(style, orientation="square"):
    """Busca fondos en Pexels si se configuró PEXELS_API_KEY."""
    if not PEXELS_API_KEY:
        return None

    terms = {
        "lifestyle": "minimalist modern living room interior",
        "premium": "luxury marble beige interior surface",
        "publicidad": "elegant neutral home interior product background",
        "blanco": "white studio photography background",
    }
    try:
        response = HTTP.get(
            "https://api.pexels.com/v1/search",
            headers={"Authorization": PEXELS_API_KEY},
            params={
                "query": terms.get(style, terms["lifestyle"]),
                "per_page": 10,
                "orientation": orientation,
            },
            timeout=(5, 20),
        )
        response.raise_for_status()
        photos = response.json().get("photos", [])
        if not photos:
            return None
        photo = random.choice(photos)
        src = photo.get("src", {})
        url = src.get("large2x") or src.get("large")
        return download_background(url) if url else None
    except Exception:
        logger.exception("No se pudo obtener un fondo de Pexels.")
        return None


def obtener_fondo(style, size=(1080, 1080)):
    # Primero Pexels (si hay API key); luego fondos curados de respaldo.
    background = buscar_fondo_pexels(style)
    if background is None:
        urls = BACKGROUND_CATALOG.get(style, BACKGROUND_CATALOG["lifestyle"])
        for url in random.sample(urls, len(urls)):
            try:
                background = download_background(url)
                break
            except Exception:
                logger.warning("No se pudo descargar un fondo: %s", url)

    if background is None:
        # Fondo neutro de emergencia, nunca altera el producto.
        background = Image.new("RGB", size, (242, 240, 235))

    if style == "blanco":
        background = Image.new("RGB", size, (250, 250, 250))
    else:
        # Recorte centrado a 1:1 para la publicación.
        bg_ratio = background.width / background.height
        target_ratio = size[0] / size[1]
        if bg_ratio > target_ratio:
            new_width = int(background.height * target_ratio)
            left = (background.width - new_width) // 2
            background = background.crop((left, 0, left + new_width, background.height))
        else:
            new_height = int(background.width / target_ratio)
            top = (background.height - new_height) // 2
            background = background.crop((0, top, background.width, top + new_height))

    return background.resize(size, Image.Resampling.LANCZOS).convert("RGBA")


def compose_product_scene(original_image, style):
    """
    Recorta el fondo con segmentación, pero pega los píxeles originales del
    producto. La etiqueta/envase no se vuelven a generar ni se redibujan.
    """
    product = remove_background(original_image)
    canvas_size = (1080, 1080)
    background = obtener_fondo(style, canvas_size)

    max_height = 760 if style != "blanco" else 820
    max_width = 650
    scale = min(max_width / product.width, max_height / product.height, 1.0)
    # No agrandamos demasiado una foto pequeña: evitamos pérdida de calidad.
    new_size = (
        max(1, int(product.width * scale)),
        max(1, int(product.height * scale)),
    )
    product = product.resize(new_size, Image.Resampling.LANCZOS)

    # Sombra suave calculada desde la transparencia del recorte.
    alpha = product.getchannel("A")
    shadow_alpha = alpha.filter(ImageFilter.GaussianBlur(24))
    shadow_alpha = shadow_alpha.point(lambda p: int(p * 0.30))
    shadow = Image.new("RGBA", product.size, (0, 0, 0, 0))
    shadow.putalpha(shadow_alpha)

    x = (canvas_size[0] - product.width) // 2
    y = canvas_size[1] - product.height - (100 if style == "blanco" else 115)

    shadow_layer = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    shadow_layer.alpha_composite(shadow, (x + 10, y + 18))
    background = Image.alpha_composite(background, shadow_layer)

    # El producto original segmentado se coloca por encima de la sombra.
    background.alpha_composite(product, (x, y))

    final = background.convert("RGB")
    if style == "blanco":
        # Mantiene blanco puro y una sombra natural debajo del producto.
        final = ImageEnhance.Contrast(final).enhance(1.02)
    return final


# ---------------------------------------------------------------------
# GOOGLE DRIVE
# ---------------------------------------------------------------------

def init_drive():
    try:
        client_email = os.environ.get("GOOGLE_CLIENT_EMAIL", "").strip()
        private_key = os.environ.get("GOOGLE_PRIVATE_KEY", "").strip().replace("\\n", "\n")
        if not client_email or not private_key:
            logger.warning("Faltan GOOGLE_CLIENT_EMAIL o GOOGLE_PRIVATE_KEY.")
            return None

        from pydrive2.auth import GoogleAuth
        from pydrive2.drive import GoogleDrive

        service_config = {
            "type": "service_account",
            "client_email": client_email,
            "private_key": private_key,
            "token_uri": "https://oauth2.googleapis.com/token",
        }
        project_id = os.environ.get("GOOGLE_PROJECT_ID", "").strip()
        if project_id:
            service_config["project_id"] = project_id

        gauth = GoogleAuth()
        gauth.settings["client_config_backend"] = "service"
        gauth.settings["service_config"] = {"client_json_dict": service_config}
        gauth.ServiceAuth()
        return GoogleDrive(gauth)
    except Exception:
        logger.exception("Error al inicializar Google Drive.")
        return None


def guardar_en_drive(local_path, filename):
    drive = init_drive()
    if not drive:
        raise RuntimeError(
            "No se pudo conectar con Google Drive. Revisá las variables "
            "GOOGLE_CLIENT_EMAIL y GOOGLE_PRIVATE_KEY."
        )

    metadata = {"title": filename}
    if GOOGLE_DRIVE_FOLDER_ID:
        metadata["parents"] = [{"id": GOOGLE_DRIVE_FOLDER_ID}]

    drive_file = drive.CreateFile(metadata)
    drive_file.SetContentFile(local_path)
    drive_file.Upload()
    return drive_file.get("alternateLink", "")


# ---------------------------------------------------------------------
# TELEGRAM: MENÚS
# ---------------------------------------------------------------------

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
        [InlineKeyboardButton("🌐 Buscar fotos comerciales en la Web", callback_data="modo_web")],
        [InlineKeyboardButton("📷 Catálogo: fondo blanco", callback_data="estilo_blanco")],
        [InlineKeyboardButton("🏡 Lifestyle: ambientes reales", callback_data="estilo_lifestyle")],
        [InlineKeyboardButton("✨ Fondo premium", callback_data="estilo_premium")],
        [InlineKeyboardButton("📢 Imagen para publicidad", callback_data="estilo_publicidad")],
        [InlineKeyboardButton("❌ Cancelar", callback_data="cancelar")],
    ]
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "✅ Foto recibida y analizada\n"
            f"🔎 Producto detectado: {product_name}\n\n"
            "Elegí qué tipo de imagen querés preparar:"
        ),
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user_message = update.message.text.strip()
    chat_id = update.effective_chat.id

    if "Enviar Foto" in user_message:
        context.user_data["awaiting_photo"] = True
        await update.message.reply_text(
            "✅ Enviá ahora la foto de tu producto.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    if "Ayuda" in user_message or user_message == "/help":
        await update.message.reply_text(
            "ℹ️ Cómo usar el bot:\n"
            "1. Tocá Enviar Foto de Producto.\n"
            "2. Mandá una foto nítida, preferentemente con fondo uniforme.\n"
            "3. Elegí fondo blanco, lifestyle, premium o publicidad.\n"
            "4. Revisá el resultado y guardalo en Drive.\n\n"
            "También podés escribirme directamente qué publicación necesitás."
        )
        return

    await update.message.reply_text("✨ Redactando propuesta...")
    try:
        copy_text = await asyncio_to_thread(redactar_copy, user_message)
        await update.message.reply_text(copy_text)
    except Exception as exc:
        logger.exception("Error redactando copy.")
        await update.message.reply_text(f"⚠️ No pude redactar el texto: {exc}")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    try:
        if update.message.photo:
            photo_file = await update.message.photo[-1].get_file()
        elif update.message.document and update.message.document.mime_type:
            if not update.message.document.mime_type.startswith("image/"):
                await update.message.reply_text("⚠️ El archivo no parece ser una imagen.")
                return
            photo_file = await update.message.document.get_file()
        else:
            await update.message.reply_text("⚠️ Imagen no válida.")
            return

        photo_bytes = await photo_file.download_as_bytearray()
        image = get_image_from_bytes(photo_bytes)
        if image.width * image.height > 30_000_000:
            await update.message.reply_text("⚠️ La imagen es demasiado grande. Enviá una versión más liviana.")
            return

        original_path = save_original_image(image, user_id)
        context.user_data["original_photo"] = original_path
        context.user_data["original_photo_bytes"] = bytes(photo_bytes)
        context.user_data["product_name"] = "Producto Vilo Aromas"

        await update.message.reply_text("🔎 Analizando el producto...")
        try:
            product_name = await asyncio_to_thread(identificar_producto, image)
        except Exception:
            logger.exception("No se pudo identificar el producto.")
            product_name = "Producto sin identificar"

        context.user_data["product_name"] = product_name
        context.user_data["awaiting_photo"] = False
        await mostrar_menu_principal(context, chat_id, product_name)

    except Exception as exc:
        logger.exception("Error procesando la foto.")
        await context.bot.send_message(chat_id=chat_id, text=f"⚠️ Error procesando la foto: {exc}")


# Evita bloquear el event loop de Telegram con llamadas síncronas.
async def asyncio_to_thread(func, *args, **kwargs):
    import asyncio
    return await asyncio.to_thread(func, *args, **kwargs)


# ---------------------------------------------------------------------
# CALLBACKS
# ---------------------------------------------------------------------

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
                caption=f"🖼️ Resultado web {index}\nFuente: {url[:180]}",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        except Exception:
            logger.exception("No se pudo enviar una imagen web.")


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    data = query.data or ""

    if data == "modo_web" or data == "buscar_mas_web":
        await query.edit_message_text("🌐 Buscando imágenes comerciales en la web...")
        product_name = context.user_data.get("product_name", "aromatizante")
        results = await asyncio_to_thread(buscar_imagenes_web, product_name, 4)

        if not results:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "⚠️ No pude obtener imágenes web en esta búsqueda.\n"
                    "Puede ser una limitación temporal del buscador o de los sitios de origen. "
                    "Probá nuevamente en unos minutos o usá los fondos profesionales."
                ),
            )
        else:
            context.user_data["web_results"] = results
            await enviar_imagenes_web(context, chat_id, results)

        keyboard = [
            [InlineKeyboardButton("🔄 Buscar más resultados", callback_data="buscar_mas_web")],
            [InlineKeyboardButton("🔙 Volver al menú", callback_data="volver_menu")],
        ]
        await context.bot.send_message(
            chat_id=chat_id,
            text="¿Qué querés hacer ahora?",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if data.startswith("estilo_"):
        style = data.replace("estilo_", "", 1)
        if style not in STYLE_LABELS:
            await query.edit_message_text("⚠️ Estilo no válido.")
            return

        original_path = context.user_data.get("original_photo")
        if not original_path or not os.path.exists(original_path):
            await query.edit_message_text("⚠️ No encuentro la foto original. Enviá nuevamente el producto.")
            return

        context.user_data["last_style"] = style
        await query.edit_message_text(
            f"🎨 Preparando {STYLE_LABELS[style]}...\n"
            "Conservando el envase y la etiqueta de la foto original."
        )
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
                    caption=(
                        f"✨ {STYLE_LABELS[style]} listo.\n\n"
                        "El producto se tomó de tu fotografía original; no se regeneró con IA.\n"
                        "Revisá especialmente los bordes y la etiqueta antes de aprobar."
                    ),
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
        except Exception as exc:
            logger.exception("Error creando composición.")
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"⚠️ No pude crear la composición: {exc}\n\n"
                    "Si el error menciona rembg, revisá la instalación y el despliegue."
                ),
            )
        return

    if data == "volver_menu":
        product_name = context.user_data.get("product_name", "Producto Vilo Aromas")
        await mostrar_menu_principal(context, chat_id, product_name)
        return

    if data.startswith("save_web_"):
        index = int(data.replace("save_web_", ""))
        results = context.user_data.get("web_results", [])
        if index < 0 or index >= len(results):
            await query.edit_message_caption(caption="⚠️ La imagen ya no está disponible. Volvé a buscarla.")
            return
        try:
            await query.edit_message_caption(caption="☁️ Guardando imagen web en Google Drive...")
            _, data_bytes = results[index]
            path = str(user_folder(update.effective_user.id) / f"web_{uuid.uuid4().hex[:8]}.jpg")
            image = get_image_from_bytes(data_bytes).convert("RGB")
            image.save(path, "JPEG", quality=94)
            link = await asyncio_to_thread(guardar_en_drive, path, f"vilo_web_{uuid.uuid4().hex[:6]}.jpg")
            await context.bot.send_message(
                chat_id=chat_id,
                text="✅ Imagen guardada en Google Drive." + (f"\n{link}" if link else ""),
            )
        except Exception as exc:
            logger.exception("Error guardando imagen web.")
            await context.bot.send_message(chat_id=chat_id, text=f"⚠️ Error al guardar en Drive: {exc}")
        return

    if data == "save_drive_escena":
        path = context.user_data.get("pending_photo")
        if not path or not os.path.exists(path):
            await query.edit_message_caption(caption="⚠️ No encuentro la imagen preparada.")
            return
        try:
            await query.edit_message_caption(caption="☁️ Subiendo imagen a Google Drive...")
            product = safe_filename(context.user_data.get("product_name", "producto"))
            style = safe_filename(context.user_data.get("last_style", "vilo"))
            link = await asyncio_to_thread(
                guardar_en_drive,
                path,
                f"vilo_{product}_{style}_{uuid.uuid4().hex[:6]}.jpg",
            )
            await context.bot.send_message(
                chat_id=chat_id,
                text="🚀 Imagen guardada en Google Drive." + (f"\n{link}" if link else ""),
            )
        except Exception as exc:
            logger.exception("Error guardando composición.")
            await context.bot.send_message(chat_id=chat_id, text=f"⚠️ Error al guardar en Drive: {exc}")
        return

    if data == "cancelar":
        await query.edit_message_text("Operación cancelada. Podés comenzar nuevamente con /start.")
        context.user_data.clear()
        return


# ---------------------------------------------------------------------
# ERRORES Y ARRANQUE
# ---------------------------------------------------------------------

async def error_handler(update, context):
    logger.exception("Error no controlado en Telegram:", exc_info=context.error)


def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("Falta configurar TELEGRAM_TOKEN en las variables de entorno.")
    if not GEMINI_API_KEY:
        logger.warning("GEMINI_API_KEY no está configurada: identificación y copies no funcionarán.")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_photo))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_error_handler(error_handler)

    logger.info("Bot de Vilo Aromas iniciado.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
