import os
import io
import re
import json
import uuid
import logging
import asyncio
import threading
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

import requests
from PIL import Image, ImageOps
import google.generativeai as genai
from ddgs import DDGS

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

# ---------------------------------------------------------
# CONFIGURACIÓN
# ---------------------------------------------------------

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

HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "ViloAromasBot/3.0"})

if not TELEGRAM_TOKEN:
    logger.warning("Falta la variable TELEGRAM_TOKEN.")
if not GEMINI_API_KEY:
    logger.warning("Falta la variable GEMINI_API_KEY. La identificación y los copies estarán deshabilitados.")
else:
    genai.configure(api_key=GEMINI_API_KEY)


# ---------------------------------------------------------
# SERVIDOR DE ESTADO PARA RENDER
# ---------------------------------------------------------

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


# ---------------------------------------------------------
# UTILIDADES DE IMÁGENES
# ---------------------------------------------------------

def user_folder(user_id):
    folder = DATA_DIR / str(user_id)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def get_image_from_bytes(data):
    image = Image.open(io.BytesIO(data))
    image = ImageOps.exif_transpose(image)
    return image.convert("RGB")


# ---------------------------------------------------------
# GEMINI: IDENTIFICAR PRODUCTO Y REDACTAR COPIES
# ---------------------------------------------------------

def identificar_producto(image):
    prompt = """
Analiza cuidadosamente la fotografía del producto.

Objetivo: identificar el producto REAL que aparece en la imagen para buscar
fotografías del mismo producto en Internet.

Lee únicamente información que puedas observar en la etiqueta o envase:
- marca
- línea o nombre comercial
- tipo de producto/envase
- fragancia o variante, solo si se puede leer con claridad.

No adivines fragancias, variantes, marcas ni modelos.
No deduzcas una fragancia por el color del envase.
Si no puedes leer el nombre o la variante, indícalo como desconocido.

Responde SOLO con JSON válido y con esta estructura:
{
  "identified": true,
  "name": "marca + nombre/variante que se ve en la etiqueta",
  "brand": "marca o cadena vacía",
  "product_type": "tipo de producto o cadena vacía",
  "variant": "fragancia/variante legible o cadena vacía",
  "confidence": 0.0,
  "search_terms": ["consulta precisa 1", "consulta precisa 2"]
}

Si no puedes identificar el producto con seguridad:
{
  "identified": false,
  "name": "",
  "brand": "",
  "product_type": "",
  "variant": "",
  "confidence": 0.0,
  "search_terms": []
}

La confianza debe ser un número entre 0 y 1.
Las consultas deben incluir solo marca, nombre y variante que realmente hayas podido leer.
"""
    try:
        model = genai.GenerativeModel(GEMINI_MODEL)
        response = model.generate_content([image, prompt])
        raw = (response.text or "").strip()
        match = re.search(r"\{.*\}", raw, flags=re.S)
        if not match:
            raise ValueError("Gemini no devolvió un JSON válido.")

        data = json.loads(match.group(0))
        name = str(data.get("name") or "").strip()
        confidence = float(data.get("confidence") or 0)
        identified = bool(data.get("identified")) and bool(name) and confidence >= 0.70

        terms = data.get("search_terms") or []
        if not isinstance(terms, list):
            terms = []
        terms = [
            str(term).strip()
            for term in terms
            if str(term).strip()
        ][:3]

        if not identified or not terms:
            return {
                "identified": False,
                "name": name,
                "confidence": confidence,
                "search_terms": [],
            }

        return {
            "identified": True,
            "name": name[:180],
            "brand": str(data.get("brand") or "").strip()[:80],
            "product_type": str(data.get("product_type") or "").strip()[:100],
            "variant": str(data.get("variant") or "").strip()[:100],
            "confidence": confidence,
            "search_terms": terms,
        }

    except Exception:
        logger.exception("Error identificando el producto con Gemini")
        raise


def redactar_copy(user_message):
    if not GEMINI_API_KEY:
        raise RuntimeError("Falta configurar GEMINI_API_KEY.")
    model = genai.GenerativeModel(GEMINI_MODEL)
    prompt = (
        "Actúa como community manager de Vilo Aromas, negocio de artículos "
        "de limpieza y aromatización. Escribe en español rioplatense, "
        "con tono profesional, comercial, claro y cercano. "
        "No inventes precios, promociones, tamaños ni características que "
        "el usuario no haya indicado.\n\n"
        f"Solicitud del usuario: {user_message}"
    )
    response = model.generate_content(prompt)
    return (response.text or "No pude generar el texto.").strip()


# ---------------------------------------------------------
# BÚSQUEDA DE IMÁGENES
# ---------------------------------------------------------

def buscar_imagenes_web(search_terms, max_resultados=4):
    if not search_terms:
        return []

    results = []
    seen = set()

    for query_text in search_terms:
        try:
            with DDGS() as ddgs:
                found = list(ddgs.images(query=query_text, max_results=15))

            for item in found:
                url = item.get("image")
                if not url or url in seen:
                    continue

                seen.add(url)

                try:
                    response = HTTP.get(url, timeout=(5, 15))
                    response.raise_for_status()
                    content_type = response.headers.get("Content-Type", "").lower()
                    data = response.content

                    if not content_type.startswith("image/"):
                        continue
                    if not 5_000 <= len(data) <= 10_000_000:
                        continue

                    image = get_image_from_bytes(data)
                    if image.width < 180 or image.height < 180:
                        continue

                    results.append((url, data))
                    if len(results) >= max_resultados:
                        return results

                except Exception:
                    logger.debug("No se pudo descargar una imagen: %s", url)

        except Exception:
            logger.exception("Falló la búsqueda de imágenes para: %s", query_text)

    return results


# ---------------------------------------------------------
# GOOGLE DRIVE
# ---------------------------------------------------------

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
        logger.exception("No se pudo iniciar Google Drive")
        return None


def guardar_en_drive(local_path, filename):
    drive = init_drive()
    if not drive:
        raise RuntimeError(
            "No se pudo conectar con Google Drive. Revisá las credenciales."
        )

    metadata = {"title": filename}
    if GOOGLE_DRIVE_FOLDER_ID:
        metadata["parents"] = [{"id": GOOGLE_DRIVE_FOLDER_ID}]

    drive_file = drive.CreateFile(metadata)
    drive_file.SetContentFile(local_path)
    drive_file.Upload()
    return drive_file.get("alternateLink", "")


# ---------------------------------------------------------
# MENÚS Y COMANDOS DE TELEGRAM
# ---------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()

    user_name = update.effective_user.first_name or "¡hola!"
    keyboard = [
        ["📸 Enviar Foto de Producto"],
        ["📝 Redactar Copy para Redes"],
        ["❓ Ayuda / Estado"],
    ]

    await update.message.reply_text(
        f"¡Hola {user_name}! 👋 Bienvenido al asistente de Vilo Aromas.\n\n"
        "Enviá una foto clara de un producto para identificarlo y buscar "
        "imágenes comerciales del mismo producto.",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
    )


async def mostrar_confirmacion_producto(context, chat_id, product_name):
    keyboard = [
        [InlineKeyboardButton("✅ Sí, es este producto", callback_data="confirmar_producto")],
        [InlineKeyboardButton("✏️ Corregir nombre o fragancia", callback_data="corregir_producto")],
        [InlineKeyboardButton("❌ Cancelar", callback_data="cancelar")],
    ]

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "🔎 Creo que identifiqué este producto:\n\n"
            f"*{product_name}*\n\n"
            "Antes de buscar imágenes, confirmame que la marca y la variante "
            "sean correctas."
        ),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user_message = update.message.text.strip()

    if "Enviar Foto" in user_message:
        context.user_data["awaiting_photo"] = True
        await update.message.reply_text(
            "📸 Enviá ahora una foto clara del producto.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    if "Ayuda" in user_message or user_message == "/help":
        await update.message.reply_text(
            "ℹ️ Cómo usar el bot:\n"
            "1. Elegí «Enviar Foto de Producto».\n"
            "2. Enviá una foto donde se lea la etiqueta.\n"
            "3. Confirmá la marca y variante detectadas.\n"
            "4. Revisá las imágenes encontradas y guardá las que te sirvan en Drive.\n\n"
            "También podés escribir una solicitud para redactar un copy para redes."
        )
        return

    if context.user_data.get("awaiting_product_correction"):
        product_name = user_message[:180]
        context.user_data["awaiting_product_correction"] = False
        context.user_data["product_name"] = product_name
        context.user_data["search_terms"] = [product_name]
        await mostrar_confirmacion_producto(
            context,
            update.effective_chat.id,
            product_name,
        )
        return

    await update.message.reply_text("✨ Redactando propuesta...")
    try:
        copy_text = await asyncio.to_thread(redactar_copy, user_message)
        await update.message.reply_text(copy_text)
    except Exception:
        logger.exception("Error redactando copy")
        await update.message.reply_text(
            "⚠️ No pude redactar el texto. Revisá que GEMINI_API_KEY esté configurada."
        )


# ---------------------------------------------------------
# RECEPCIÓN Y ANÁLISIS DE FOTOS
# ---------------------------------------------------------

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    try:
        if update.message.photo:
            photo_file = await update.message.photo[-1].get_file()
        elif update.message.document and update.message.document.mime_type:
            if not update.message.document.mime_type.startswith("image/"):
                await update.message.reply_text("⚠️ El archivo enviado no parece ser una imagen.")
                return
            photo_file = await update.message.document.get_file()
        else:
            await update.message.reply_text("⚠️ No pude reconocer la imagen.")
            return

        photo_bytes = await photo_file.download_as_bytearray()
        image = get_image_from_bytes(photo_bytes)

        await update.message.reply_text(
            "🔎 Leyendo la etiqueta y tratando de identificar el producto..."
        )

        try:
            product_info = await asyncio.to_thread(identificar_producto, image)
        except Exception:
            await update.message.reply_text(
                "⚠️ No pude analizar la imagen en este momento. "
                "Verificá la configuración de Gemini o intentá nuevamente."
            )
            return

        if not product_info.get("identified"):
            context.user_data["awaiting_product_correction"] = True
            await update.message.reply_text(
                "No pude leer con suficiente seguridad la marca o la variante.\n\n"
                "Para evitar buscar un producto equivocado, escribime el nombre "
                "exacto que aparece en el envase (marca y fragancia, si corresponde)."
            )
            return

        context.user_data["product_info"] = product_info
        context.user_data["product_name"] = product_info["name"]
        context.user_data["search_terms"] = product_info["search_terms"]

        await mostrar_confirmacion_producto(
            context,
            chat_id,
            product_info["name"],
        )

    except Exception:
        logger.exception("Error procesando la foto")
        await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ Hubo un error procesando la foto. Probá con otra imagen."
        )


# ---------------------------------------------------------
# ENVÍO DE RESULTADOS Y GUARDADO
# ---------------------------------------------------------

async def enviar_imagenes_web(context, chat_id, urls_and_bytes):
    for index, (url, data) in enumerate(urls_and_bytes):
        try:
            image = get_image_from_bytes(data)
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=92)
            buffer.seek(0)
            buffer.name = f"resultado_web_{index + 1}.jpg"

            keyboard = [[
                InlineKeyboardButton(
                    f"☁️ Guardar opción {index + 1} en Drive",
                    callback_data=f"save_web_{index}",
                )
            ]]

            await context.bot.send_photo(
                chat_id=chat_id,
                photo=buffer,
                caption=(
                    f"🖼️ Resultado {index + 1}\n"
                    "Revisá que coincidan el envase, la marca y la variante."
                ),
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

        except Exception:
            logger.exception("No se pudo enviar una imagen encontrada")


# ---------------------------------------------------------
# BOTONES
# ---------------------------------------------------------

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id
    data = query.data or ""

    if data == "confirmar_producto":
        search_terms = context.user_data.get("search_terms") or []
        if not search_terms:
            await query.edit_message_text(
                "⚠️ No tengo una búsqueda válida. Enviá nuevamente la foto."
            )
            return

        await query.edit_message_text(
            "🌐 Buscando imágenes para el producto confirmado..."
        )

        results = await asyncio.to_thread(
            buscar_imagenes_web,
            search_terms,
            4,
        )

        if not results:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "⚠️ No encontré imágenes para esa búsqueda. "
                    "Podés probar enviando otra foto o corrigiendo el nombre."
                ),
            )
        else:
            context.user_data["web_results"] = results
            await enviar_imagenes_web(context, chat_id, results)

            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "Listo. Revisá cada resultado antes de usarlo: "
                    "el buscador puede mostrar variantes parecidas."
                ),
            )
        return

    if data == "corregir_producto":
        context.user_data["awaiting_product_correction"] = True
        await query.edit_message_text(
            "✏️ Escribime el nombre exacto del producto, incluyendo marca "
            "y fragancia o variante si corresponde."
        )
        return

    if data.startswith("save_web_"):
        try:
            index = int(data.replace("save_web_", "", 1))
            results = context.user_data.get("web_results", [])

            if not 0 <= index < len(results):
                await query.edit_message_caption(
                    caption="⚠️ Este resultado ya no está disponible. Buscá las imágenes nuevamente."
                )
                return

            await query.edit_message_caption(
                caption="☁️ Guardando imagen en Google Drive..."
            )

            _, data_bytes = results[index]
            image = get_image_from_bytes(data_bytes)

            path = user_folder(chat_id) / f"web_{uuid.uuid4().hex[:8]}.jpg"
            image.save(path, "JPEG", quality=94)

            product_clean = re.sub(
                r"[^a-zA-Z0-9_-]+",
                "_",
                context.user_data.get("product_name", "producto"),
            )[:30]

            filename = f"vilo_{product_clean}_{uuid.uuid4().hex[:6]}.jpg"
            link = await asyncio.to_thread(
                guardar_en_drive,
                str(path),
                filename,
            )

            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "✅ Imagen guardada en Google Drive.\n"
                    f"{link if link else 'La imagen se subió, pero Drive no devolvió un enlace.'}"
                ),
            )

        except Exception:
            logger.exception("Error guardando imagen en Drive")
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "⚠️ No pude guardar la imagen. Revisá las credenciales "
                    "y los permisos de Google Drive."
                ),
            )
        return

    if data == "cancelar":
        context.user_data.clear()
        await query.edit_message_text(
            "Operación cancelada. Cuando quieras, enviá una nueva foto con /start."
        )
        return


# ---------------------------------------------------------
# INICIO DEL BOT
# ---------------------------------------------------------

async def main_async():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("Falta configurar TELEGRAM_TOKEN.")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(
        MessageHandler(
            filters.TEXT & (~filters.COMMAND),
            handle_message,
        )
    )
    app.add_handler(
        MessageHandler(
            filters.PHOTO | filters.Document.IMAGE,
            handle_photo,
        )
    )
    app.add_handler(CallbackQueryHandler(button_callback))

    logger.info("Bot de Vilo Aromas iniciado.")

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    threading.Thread(target=run_web_server, daemon=True).start()

    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


def main():
    try:
        asyncio.run(main_async())
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
