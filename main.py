import os
import io
import json
import html
import uuid
import hashlib
import time
import logging
import asyncio
import threading
from pathlib import Path
from functools import wraps
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

import requests
from PIL import Image, ImageOps
from google import genai
from google.genai import types

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
)
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    TypeHandler,
    filters,
)
from telegram.error import Conflict

# ───────────────────────── Configuración ─────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip()
PORT = int(os.environ.get("PORT", "10000"))

# Seguridad: IDs de Telegram permitidos, separados por coma. Vacío = cualquiera.
ALLOWED = {int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip().isdigit()}

BRAND = os.environ.get("BRAND_NAME", "Vilo Aromas")
CONTACT = os.environ.get("CONTACT_INFO", "")  # ej: "Pedidos por DM o WhatsApp 11-1234-5678"
MODEL_DIR = Path(os.environ.get("MODEL_DIR", "/tmp/vilo_models"))

# Destinos de publicación (opcionales)
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "").strip()  # @micanal o -100123...
FB_PAGE_ID = os.environ.get("FB_PAGE_ID", "").strip()
FB_PAGE_TOKEN = os.environ.get("FB_PAGE_TOKEN", "").strip()
FB_API_VERSION = os.environ.get("FB_API_VERSION", "v23.0").strip()
GOOGLE_DRIVE_FOLDER_ID = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "").strip()
GOOGLE_CLIENT_EMAIL = os.environ.get("GOOGLE_CLIENT_EMAIL", "").strip()
GOOGLE_PRIVATE_KEY = os.environ.get("GOOGLE_PRIVATE_KEY", "").strip().replace("\\n", "\n")

MAX_POST_IMAGES = 10  # límite de Telegram para álbumes

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger("vilo_bot")

HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})

client = (
    genai.Client(api_key=GEMINI_API_KEY, http_options=types.HttpOptions(timeout=60_000))
    if GEMINI_API_KEY else None
)


# ───────────────────────── Servidor de salud (Render) ─────────────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"Vilo Aromas Bot activo."
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, fmt, *args):
        pass


def keep_alive():
    """Auto-ping cada 10 min para que Render (plan gratis) no duerma el servicio."""
    url = os.environ.get("RENDER_EXTERNAL_URL", "").strip()
    if not url:
        return
    while True:
        time.sleep(600)
        try:
            HTTP.get(url, timeout=15)
        except Exception:
            pass


def run_web_server():
    try:
        ThreadingHTTPServer(("0.0.0.0", PORT), HealthHandler).serve_forever()
    except Exception:
        logger.exception("Servidor de salud caído")


# ───────────────────────── Utilidades de imagen ─────────────────────────
def get_image_from_bytes(data):
    image = Image.open(io.BytesIO(data))
    image = ImageOps.exif_transpose(image)
    return image.convert("RGB")


def shrink(image, max_side):
    img = image.copy()
    img.thumbnail((max_side, max_side), Image.LANCZOS)
    return img


def to_jpeg(image, max_side=1600, quality=90):
    """Devuelve bytes JPEG listos para Telegram (< 10 MB)."""
    buf = io.BytesIO()
    shrink(image, max_side).convert("RGB").save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


U2NETP_URL = "https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2netp.onnx"
_onnx_session = None
_onnx_lock = threading.Lock()
_run_lock = threading.Lock()


def _get_onnx():
    """Carga U2-Netp (4.7 MB) directo con onnxruntime, sin la librería rembg (que consume >500 MB)."""
    global _onnx_session
    with _onnx_lock:
        if _onnx_session is None:
            import onnxruntime as ort

            path = MODEL_DIR / "u2netp.onnx"
            if not path.exists() or path.stat().st_size < 1_000_000:
                MODEL_DIR.mkdir(parents=True, exist_ok=True)
                r = HTTP.get(U2NETP_URL, timeout=60)
                r.raise_for_status()
                tmp = path.with_suffix(".part")
                tmp.write_bytes(r.content)
                tmp.replace(path)
            so = ort.SessionOptions()
            so.enable_cpu_mem_arena = False  # evita que la memoria crezca en cada foto
            so.enable_mem_pattern = False
            so.intra_op_num_threads = 1
            _onnx_session = ort.InferenceSession(str(path), sess_options=so, providers=["CPUExecutionProvider"])
        return _onnx_session


def _recortar(image):
    """Quita el fondo y devuelve una imagen RGBA."""
    import numpy as np

    sess = _get_onnx()
    src = shrink(image, 1024).convert("RGB")
    small = np.asarray(src.resize((320, 320), Image.LANCZOS), dtype=np.float32)
    small = small / max(float(small.max()), 1e-6)
    mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
    std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
    x = ((small - mean) / std).transpose(2, 0, 1)[None].astype(np.float32)
    with _run_lock:
        pred = sess.run(None, {sess.get_inputs()[0].name: x})[0][0, 0]
    pred = (pred - pred.min()) / max(float(pred.max() - pred.min()), 1e-6)
    mask = Image.fromarray((pred * 255).astype("uint8"), "L").resize(src.size, Image.LANCZOS)
    mask = mask.point(lambda v: 0 if v < 40 else 255 if v > 200 else int((v - 40) * 255 / 160))  # bordes más nítidos
    rgba = src.convert("RGBA")
    rgba.putalpha(mask)
    return rgba


def fondo_blanco(image, size=1080, margen=0.08):
    """Recorta el producto, lo centra en un lienzo blanco cuadrado. Devuelve (imagen, quitó_fondo)."""
    try:
        recortada = _recortar(image)
        ok = True
    except Exception:
        logger.exception("No se pudo quitar el fondo")
        recortada = image.convert("RGBA")
        ok = False

    bbox = recortada.getchannel("A").getbbox()
    if bbox:
        recortada = recortada.crop(bbox)

    lado = int(size * (1 - 2 * margen))
    ratio = min(lado / recortada.width, lado / recortada.height)
    nueva = recortada.resize(
        (max(1, int(recortada.width * ratio)), max(1, int(recortada.height * ratio))),
        Image.LANCZOS,
    )
    lienzo = Image.new("RGB", (size, size), "white")
    lienzo.paste(nueva, ((size - nueva.width) // 2, (size - nueva.height) // 2), nueva)
    return lienzo, ok


# ───────────────────────── Gemini ─────────────────────────
def gemini_json(contents):
    resp = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.1),
    )
    return json.loads(resp.text)


def gemini_text(contents, temperature=0.8):
    resp = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(temperature=temperature),
    )
    return (resp.text or "").strip()


def identificar_producto(image):
    default = {"nombre": "Aromatizante Saphirus", "marca": "Saphirus", "fragancia": "", "tipo": "aromatizante", "tamano": ""}
    if not client:
        return default
    try:
        data = gemini_json([
            shrink(image, 1024),
            "Mirá la etiqueta del producto y devolvé un JSON con las claves: "
            '"marca", "fragancia", "tipo" (ej: aromatizante ambiental, difusor, sahumerio), '
            '"tamano" (solo si se lee, si no ""), y "nombre" (marca + fragancia + tipo, '
            "ideal para buscar en Google). Usá solo lo que se lee en la etiqueta, sin inventar.",
        ])
        if isinstance(data, list) and data:
            data = data[0]
        nombre = str(data.get("nombre", "")).strip()
        if len(nombre) < 3:
            return default
        return {
            "nombre": nombre,
            "marca": str(data.get("marca", "")).strip(),
            "fragancia": str(data.get("fragancia", "")).strip(),
            "tipo": str(data.get("tipo", "")).strip(),
            "tamano": str(data.get("tamano", "")).strip(),
        }
    except Exception:
        logger.exception("Gemini no pudo identificar el producto")
        return default


def generar_descripcion(producto, notas):
    if not client:
        return f"✨ {producto['nombre']} ✨\n\n{CONTACT}".strip()
    prompt = f"""Sos el community manager de "{BRAND}". Escribí un post para Instagram y Facebook, en español rioplatense, cálido y cercano.

Producto (leído de la etiqueta): {json.dumps(producto, ensure_ascii=False)}
Indicaciones del dueño (precio, promo, tamaño, etc.): {notas or "ninguna"}
Contacto: {CONTACT or "no incluir"}

Reglas:
- Empezá con un gancho corto y atractivo (con 1-2 emojis).
- 3 a 5 líneas sobre la sensación, el ambiente y cuándo usarlo. Mencioná la fragancia.
- NO inventes datos concretos (precio, tamaño, duración, ingredientes) que no estén en las indicaciones ni en la etiqueta.
- Cerrá con un llamado a la acción y el contacto (si hay).
- Terminá con 8 a 10 hashtags relevantes.
- Devolvé solo el texto del post, sin comillas ni explicaciones."""
    try:
        return gemini_text(prompt)
    except Exception:
        logger.exception("Gemini no pudo generar la descripción")
        return f"✨ {producto['nombre']} ✨\n\n{CONTACT}".strip()


# ───────────────────────── Búsqueda web ─────────────────────────
def _get_ddgs():
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS
    return DDGS


def _descargar(url):
    try:
        r = HTTP.get(url, timeout=(4, 8))
        r.raise_for_status()
        data = r.content
        if not (8_000 <= len(data) <= 8_000_000):
            return None
        img = get_image_from_bytes(data)
        if min(img.size) < 400:
            return None
        return img
    except Exception:
        return None


def buscar_imagenes_web(producto, referencia):
    """Busca fotos del producto con distintos fondos y deja solo las que Gemini confirma como el mismo producto.
    Devuelve (lista_de_imágenes_PIL, coincidencia_exacta)."""
    nombre = producto["nombre"]
    queries = [
        f"{nombre} fondo blanco",
        f"{nombre} png",
        f"{nombre} ambiente decoración hogar",
        f"{nombre} comprar",
    ]

    urls, seen = [], set()
    DDGS = _get_ddgs()
    for q in queries:
        try:
            with DDGS() as ddgs:
                for item in ddgs.images(q, max_results=8):
                    url = item.get("image")
                    if url and url not in seen:
                        seen.add(url)
                        urls.append(url)
        except Exception:
            logger.warning("Búsqueda fallida: %s", q, exc_info=True)

    with ThreadPoolExecutor(max_workers=8) as pool:
        imagenes = list(pool.map(_descargar, urls[:32]))

    candidatas, hashes = [], set()
    for img in imagenes:
        if img is None:
            continue
        h = hashlib.md5(img.resize((16, 16)).convert("L").tobytes()).hexdigest()
        if h in hashes:
            continue
        hashes.add(h)
        candidatas.append(img)
        if len(candidatas) >= 12:
            break

    if not candidatas:
        return [], False
    if not client:
        return candidatas[:4], False

    try:
        contents = [
            f"Producto de referencia: {nombre}. Abajo está mi foto de referencia y {len(candidatas)} imágenes "
            "numeradas desde 0. Devolvé JSON {\"coinciden\": [indices]} con las imágenes que muestran claramente "
            "ESE MISMO producto (misma marca, fragancia y envase). Descartá otros productos, packs mezclados y logos sueltos.",
            "Referencia:", shrink(referencia, 384),
        ]
        for i, img in enumerate(candidatas):
            contents += [f"Imagen {i}:", shrink(img, 384)]
        idx = gemini_json(contents).get("coinciden", [])
        idx = [i for i in idx if isinstance(i, int) and 0 <= i < len(candidatas)]
        if idx:
            return [candidatas[i] for i in idx[:6]], True
    except Exception:
        logger.exception("Falló el filtro con Gemini")
    return candidatas[:3], False


# ───────────────────────── Publicación ─────────────────────────
def _drive_service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_info(
        {
            "type": "service_account",
            "client_email": GOOGLE_CLIENT_EMAIL,
            "private_key": GOOGLE_PRIVATE_KEY,
            "token_uri": "https://oauth2.googleapis.com/token",
        },
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def guardar_en_drive(images, nombre):
    from googleapiclient.http import MediaIoBaseUpload

    service = _drive_service()
    links = []
    for i, data in enumerate(images, 1):
        body = {"name": f"vilo_{nombre}_{uuid.uuid4().hex[:4]}_{i}.jpg"}
        if GOOGLE_DRIVE_FOLDER_ID:
            body["parents"] = [GOOGLE_DRIVE_FOLDER_ID]
        media = MediaIoBaseUpload(io.BytesIO(data), mimetype="image/jpeg")
        f = service.files().create(body=body, media_body=media, fields="id,webViewLink", supportsAllDrives=True).execute()
        links.append(f.get("webViewLink", ""))
    return links


def publicar_facebook(images, caption):
    base = f"https://graph.facebook.com/{FB_API_VERSION}/{FB_PAGE_ID}"
    ids = []
    for data in images:
        r = HTTP.post(
            f"{base}/photos",
            data={"published": "false", "access_token": FB_PAGE_TOKEN},
            files={"source": ("foto.jpg", data, "image/jpeg")},
            timeout=90,
        )
        r.raise_for_status()
        ids.append(r.json()["id"])
    payload = {"message": caption, "access_token": FB_PAGE_TOKEN}
    for i, pid in enumerate(ids):
        payload[f"attached_media[{i}]"] = json.dumps({"media_fbid": pid})
    r = HTTP.post(f"{base}/feed", data=payload, timeout=90)
    r.raise_for_status()
    return r.json().get("id", "")


# ───────────────────────── Telegram ─────────────────────────
def autorizado(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if ALLOWED and (not user or user.id not in ALLOWED):
            if update.callback_query:
                await update.callback_query.answer("No autorizado", show_alert=True)
            elif update.message:
                await update.message.reply_text("⛔ No tenés acceso a este bot.")
            return
        return await func(update, context)
    return wrapper


def main_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Buscar fotos en la web", callback_data="web")],
        [
            InlineKeyboardButton("✍️ Descripción", callback_data="desc"),
            InlineKeyboardButton("📦 Ver post", callback_data="pack"),
        ],
        [
            InlineKeyboardButton("📢 Publicar", callback_data="pub"),
            InlineKeyboardButton("🗑 Reiniciar", callback_data="reset"),
        ],
    ])


async def enviar_pack(bot, chat_id, imgs, caption):
    caption = caption or ""
    corto = len(caption) <= 1024
    if len(imgs) == 1:
        await bot.send_photo(chat_id, imgs[0], caption=caption if corto else None)
    else:
        media = [InputMediaPhoto(b, caption=(caption if (i == 0 and corto) else None)) for i, b in enumerate(imgs)]
        await bot.send_media_group(chat_id, media)
    if caption and not corto:
        await bot.send_message(chat_id, caption)


@autorizado
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        f"👋 ¡Hola! Soy el asistente de <b>{html.escape(BRAND)}</b>.\n\n"
        "1️⃣ Mandame una foto de tu producto\n"
        "2️⃣ Te la devuelvo con fondo blanco\n"
        "3️⃣ Busco fotos del mismo producto en la web (otros fondos)\n"
        "4️⃣ Armo la descripción y publicás\n\n"
        "💡 Después de la foto podés escribirme indicaciones (precio, promo, tamaño) y regenero el texto.",
        parse_mode="HTML",
    )


@autorizado
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud = context.user_data
    if "producto" not in ud:
        await update.message.reply_text("📸 Primero mandame la foto de tu producto.")
        return
    ud["notas"] = update.message.text.strip()
    msg = await update.message.reply_text("✍️ Regenerando descripción con tus indicaciones…")
    ud["caption"] = await asyncio.to_thread(generar_descripcion, ud["producto"], ud["notas"])
    await msg.delete()
    await update.message.reply_text(ud["caption"], reply_markup=main_kb())


@autorizado
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    estado = await msg.reply_text("🔎 Leyendo el producto y quitando el fondo…")
    try:
        tg_file = await (msg.photo[-1] if msg.photo else msg.document).get_file()
        original = get_image_from_bytes(bytes(await tg_file.download_as_bytearray()))

        producto, (blanca, ok) = await asyncio.gather(
            asyncio.to_thread(identificar_producto, original),
            asyncio.to_thread(fondo_blanco, original),
        )
        blanca_bytes = to_jpeg(blanca, 1080, 92)

        ud = context.user_data
        ud.clear()
        ud.update(producto=producto, blanca=blanca_bytes, images=[blanca_bytes], web=[], notas="", caption="")

        aviso = "" if ok else "\n⚠️ No pude quitar el fondo (revisá los logs)."
        await estado.delete()
        await msg.reply_photo(
            blanca_bytes,
            caption=f"✅ Producto: {producto['nombre']}{aviso}",
            reply_markup=main_kb(),
        )
    except Exception:
        logger.exception("Error procesando foto")
        await estado.edit_text("⚠️ Ocurrió un error al procesar la imagen.")


def _destinos_kb():
    rows = []
    if TELEGRAM_CHANNEL_ID:
        rows.append([InlineKeyboardButton("📣 Canal de Telegram", callback_data="pub_tg")])
    if FB_PAGE_ID and FB_PAGE_TOKEN:
        rows.append([InlineKeyboardButton("📘 Página de Facebook", callback_data="pub_fb")])
    if GOOGLE_CLIENT_EMAIL and GOOGLE_PRIVATE_KEY:
        rows.append([InlineKeyboardButton("☁️ Guardar en Drive", callback_data="pub_drive")])
    return InlineKeyboardMarkup(rows) if rows else None


@autorizado
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id
    bot = context.bot
    data = query.data
    ud = context.user_data

    if data == "reset":
        ud.clear()
        await query.answer("Listo")
        await bot.send_message(chat_id, "🗑 Reiniciado. Mandame una nueva foto.")
        return

    if "producto" not in ud:
        await query.answer("Se perdió la sesión, mandame la foto de nuevo 📸", show_alert=True)
        return

    try:
        if data == "web":
            await query.answer()
            estado = await bot.send_message(chat_id, "🌐 Buscando y verificando imágenes (puede tardar ~30 s)…")
            referencia = get_image_from_bytes(ud["blanca"])
            results, exacto = await asyncio.to_thread(buscar_imagenes_web, ud["producto"], referencia)
            await estado.delete()
            if not results:
                await bot.send_message(chat_id, "⚠️ No encontré imágenes. Probá de nuevo en un rato.")
                return
            ud["web"] = [to_jpeg(img) for img in results]
            if not exacto:
                await bot.send_message(chat_id, "ℹ️ No pude confirmar coincidencia exacta; estas son las más parecidas.")
            for i, b in enumerate(ud["web"]):
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("➕ Agregar", callback_data=f"add_{i}"),
                    InlineKeyboardButton("⚪ Fondo blanco + agregar", callback_data=f"wb_{i}"),
                ]])
                await bot.send_photo(chat_id, b, caption=f"🖼️ Opción {i + 1}", reply_markup=kb)

        elif data.startswith(("add_", "wb_")):
            kind, idx = data.split("_")
            idx = int(idx)
            if not (0 <= idx < len(ud["web"])):
                await query.answer("Opción no disponible", show_alert=True)
                return
            if len(ud["images"]) >= MAX_POST_IMAGES:
                await query.answer("Máximo 10 imágenes por post", show_alert=True)
                return
            if kind == "add":
                ud["images"].append(ud["web"][idx])
                await query.answer(f"Agregada ✅ ({len(ud['images'])} en el post)")
            else:
                await query.answer("Quitando fondo…")
                img = get_image_from_bytes(ud["web"][idx])
                blanca, _ = await asyncio.to_thread(fondo_blanco, img)
                b = to_jpeg(blanca, 1080, 92)
                ud["images"].append(b)
                await bot.send_photo(chat_id, b, caption=f"⚪ Agregada con fondo blanco ({len(ud['images'])} en el post)")

        elif data == "desc":
            await query.answer("Escribiendo…")
            ud["caption"] = await asyncio.to_thread(generar_descripcion, ud["producto"], ud["notas"])
            await bot.send_message(chat_id, ud["caption"], reply_markup=main_kb())

        elif data == "pack":
            await query.answer()
            if not ud["caption"]:
                ud["caption"] = await asyncio.to_thread(generar_descripcion, ud["producto"], ud["notas"])
            await enviar_pack(bot, chat_id, ud["images"], ud["caption"])
            await bot.send_message(chat_id, "📦 Este es tu post. ¿Lo publico?", reply_markup=main_kb())

        elif data == "pub":
            await query.answer()
            kb = _destinos_kb()
            if not kb:
                await bot.send_message(
                    chat_id,
                    "No hay destinos configurados. Usá «📦 Ver post» y reenviá las fotos y el texto a tus redes.",
                )
                return
            if not ud["caption"]:
                ud["caption"] = await asyncio.to_thread(generar_descripcion, ud["producto"], ud["notas"])
            await bot.send_message(chat_id, f"¿Dónde publico las {len(ud['images'])} foto(s)?", reply_markup=kb)

        elif data == "pub_tg":
            await query.answer("Publicando…")
            await enviar_pack(bot, TELEGRAM_CHANNEL_ID, ud["images"], ud["caption"])
            await bot.send_message(chat_id, "✅ Publicado en el canal de Telegram.")

        elif data == "pub_fb":
            await query.answer("Publicando…")
            post_id = await asyncio.to_thread(publicar_facebook, ud["images"], ud["caption"])
            await bot.send_message(chat_id, f"✅ Publicado en Facebook (id {post_id}).")

        elif data == "pub_drive":
            await query.answer("Subiendo…")
            nombre = "".join(c if c.isalnum() else "_" for c in ud["producto"]["nombre"])[:25]
            links = await asyncio.to_thread(guardar_en_drive, ud["images"], nombre)
            await bot.send_message(chat_id, "✅ Guardado en Drive:\n" + "\n".join(links))

    except Exception:
        logger.exception("Error en callback %s", data)
        await bot.send_message(chat_id, "⚠️ Algo falló. Revisá los logs o probá de nuevo.")


async def log_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info("Update recibido de %s (%s)", user.id if user else "?", type(update.effective_message).__name__)


async def post_init(app):
    me = await app.bot.get_me()
    logger.info("Conectado como @%s", me.username)


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    if isinstance(context.error, Conflict):
        logger.error("CONFLICT: hay OTRA instancia usando este mismo TELEGRAM_TOKEN. Apagá la otra.")
        return
    logger.error("Error no controlado", exc_info=context.error)


def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("Falta TELEGRAM_TOKEN.")
    if not ALLOWED:
        logger.warning("ALLOWED_USER_IDS vacío: cualquiera puede usar el bot y publicar en tus redes.")

    threading.Thread(target=run_web_server, daemon=True).start()
    threading.Thread(target=keep_alive, daemon=True).start()

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .concurrent_updates(True)  # una foto lenta no bloquea los demás mensajes
        .post_init(post_init)
        .build()
    )
    app.add_handler(TypeHandler(Update, log_update), group=-1)
    app.add_handler(CommandHandler(["start", "nuevo"], start))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_error_handler(on_error)

    logger.info("Bot iniciado correctamente.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
