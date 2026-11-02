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
from duckduckgo_search import DDGS

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

# Configuración básica
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip()

PORT = int(os.environ.get("PORT", "10000"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/tmp/vilo_aromas"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
GOOGLE_DRIVE_FOLDER_ID = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "").strip()

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger("vilo_bot")

HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "ViloAromasBot/4.1"})

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)


# Servidor web para mantener vivo a Render
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"Vilo Aromas Bot activo."
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
        pass


# Utilidades de imagen y Drive
def user_folder(user_id):
    folder = DATA_DIR / str(user_id)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def get_image_from_bytes(data):
    image = Image.open(io.BytesIO(data))
    image = ImageOps.exif_transpose(image)
    return image.convert("RGB")


def identificar_producto(image):
    if not GEMINI_API_KEY:
        return {"name": "Producto Vilo Aromas", "search_terms": ["aromatizante Saphirus", "Saphirus aerosol"]}
    try:
        model = genai.GenerativeModel(GEMINI_MODEL)
        prompt = """
Analiza la fotografía del producto. Lee la etiqueta con precisión (marca, tipo de producto y fragancia).
Devuelve SOLO JSON válido con este formato estricto:
{
  "name": "nombre exacto del producto",
  "search_terms": [
    "marca + tipo + fragancia exacta",
    "marca + tipo de producto",
    "marca aromatizante"
  ]
}
"""
        response = model.generate_content([image, prompt])
        raw = (response.text or "").strip()
        match = re.search(r"\{.*\}", raw, flags=re.S)
        if not match:
            return {"name": "Producto Vilo Aromas", "search_terms": ["aromatizante Saphirus", "Saphirus aerosol"]}
        data = json.loads(match.group(0))
        name = str(data.get("name") or "").strip() or "Producto Vilo Aromas"
        terms = data.get("search_terms") or [name, "Saphirus aerosol"]
        return {"name": name[:180], "search_terms": terms[:5]}
    except Exception:
        return {"name": "Producto Vilo Aromas", "search_terms": ["aromatizante Saphirus", "Saphirus aerosol"]}


def buscar_imagenes_web(search_terms):
    results = []
    seen = set()
    
    # Consultas de respaldo en cascada para asegurar que encuentre resultados
    queries_to_try = list(search_terms) + ["aromatizante Saphirus", "Saphirus aerosol ambiental"]

    for query in queries_to_try:
        try:
            with DDGS() as ddgs:
                found = list(ddgs.images(query=query, max_results=12))
            for item in found:
                url = item.get("image")
                if not url or url in seen:
                    continue
                seen.add(url)
                try:
                    resp = HTTP.get(url, timeout=(5, 10))
                    resp.raise_for_status()
                    data = resp.content
                    if 5000 <= len(data) <= 10_000_000:
                        results.append((url, data))
                        if len(results) >= 4:
                            return results
                except Exception:
                    pass
        except Exception:
            pass
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
        raise RuntimeError("Google Drive no configurado.")
    metadata = {"title": filename}
    if GOOGLE_DRIVE_FOLDER_ID:
        metadata["parents"] = [{"id": GOOGLE_DRIVE_FOLDER_ID}]
    f = drive.CreateFile(metadata)
    f.SetContentFile(local_path)
    f.Upload()
    return f.get("alternateLink", "")


# Handlers de Telegram
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    keyboard = [
        ["📸 Enviar Foto de Producto"],
        ["❓ Ayuda"],
    ]
    await update.message.reply_text(
        "👋 ¡Hola! Bienvenido al asistente de **Vilo Aromas**.\n\n"
        "Envía una foto de tu producto para buscar imágenes comerciales exactas.",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
        parse_mode="Markdown",
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message.text.strip()
    if "Enviar Foto" in msg:
        await update.message.reply_text("📸 Envía ahora la foto de tu producto:", reply_markup=ReplyKeyboardRemove())
        return
    await update.message.reply_text("Por favor, usa el menú o envía una foto de tu producto.")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        photo_file = await update.message.photo[-1].get_file()
        photo_bytes = await photo_file.download_as_bytearray()
        image = get_image_from_bytes(photo_bytes)

        await update.message.reply_text("🔎 Analizando etiqueta con Gemini...")
