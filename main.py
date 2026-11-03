"""Bot de Vilo Aromas: buscar publicación ML, elegir foto, adaptar formato y redactar copy."""
from __future__ import annotations

import asyncio
import difflib
import io
import json
import logging
import os
import re
import signal
import threading
import time
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import requests
from PIL import Image, ImageOps, ImageFilter
try:
    from google import genai
    from google.genai import types
except ImportError:  # Permite arrancar el bot sin IA si el paquete todavía no se instaló.
    genai = None
    types = None
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)

# ───────────────────────── Configuración ─────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
POS_API_URL = os.getenv("POS_API_URL", "https://app.viloaromas.com.ar/api_stock.php").strip()
POS_API_KEY = os.getenv("POS_API_KEY", "").strip()
ML_PROXY_URL = os.getenv("ML_PROXY_URL", "https://app.viloaromas.com.ar/mercadolibre.php").strip()
ML_PROXY_API_KEY = os.getenv("ML_PROXY_API_KEY", "").strip()
PORT = int(os.getenv("PORT", "10000"))
ALLOWED = {int(x) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip().isdigit()}
BRAND = os.getenv("BRAND_NAME", "Vilo Aromas").strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("vilo_bot")

HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "ViloAromasTelegramBot/1.0"})
GEMINI = genai.Client(api_key=GEMINI_API_KEY) if (GEMINI_API_KEY and genai) else None

# Dimensiones en píxeles. Feed conserva el 4:5; Story y portada de Reel usan 9:16.
FORMATOS = {
    "feed":  {"nombre": "Feed", "size": (1080, 1350), "max_chars": 2200,
              "instruccion": "Redactá un copy atractivo para publicación de Feed."},
    "story": {"nombre": "Historia", "size": (1080, 1920), "max_chars": 125,
              "instruccion": "Redactá una frase breve para colocar sobre una Historia; máximo 125 caracteres."},
    "reel":  {"nombre": "Reel (portada)", "size": (1080, 1920), "max_chars": 2200,
              "instruccion": "Redactá el texto de descripción/caption para acompañar un Reel; no generes un video."},
}


def _norm(value: Any) -> str:
    s = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _http_json(method: str, url: str, *, headers: dict, params: dict | None = None,
               json_body: dict | None = None, timeout: int = 35) -> dict:
    response = HTTP.request(method, url, headers=headers, params=params, json=json_body, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("El servidor devolvió una respuesta inesperada.")
    return data


def obtener_catalogo() -> list[dict]:
    if not POS_API_KEY:
        raise RuntimeError("Falta configurar POS_API_KEY en las variables del bot.")
    data = _http_json(
        "GET", POS_API_URL,
        headers={"X-API-Key": POS_API_KEY},
        params={"accion": "ml-listar-vinculos"},
    )
    if not data.get("ok"):
        raise RuntimeError(data.get("error") or "No se pudo leer el catálogo del POS.")
    rows = data.get("productos") or []
    return [x for x in rows if x.get("item_id") and x.get("codigo")]


def buscar_candidatos(query: str, catalogo: list[dict], limit: int = 6) -> list[dict]:
    q = _norm(query)
    if not q:
        return []
    q_tokens = set(q.split())
    # Si el usuario escribió exactamente un ID, SKU o código interno, no mezclar
    # ese acierto con coincidencias débiles de otros productos.
    exactos = []
    for p in catalogo:
        for raw in (p.get("item_id"), p.get("codigo"), p.get("sku")):
            if raw and _norm(raw) == q:
                exactos.append(p)
                break
    if exactos:
        return exactos[:limit]

    scored = []
    for p in catalogo:
        fields = [p.get("item_id"), p.get("codigo"), p.get("sku"), p.get("detalle"),
                  p.get("marca"), p.get("nombre_publicacion"), p.get("rubro")]
        texts = [_norm(v) for v in fields if v]
        joined = " ".join(texts)
        seq = max((difflib.SequenceMatcher(None, q, t).ratio() for t in texts), default=0.0)
        overlap = len(q_tokens & set(joined.split())) / max(1, len(q_tokens))
        phrase = 0.18 if q in joined else 0.0
        score = min(max(seq, overlap * 0.86) + phrase, 1.0)
        if score >= 0.22:
            scored.append((score, p))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in scored[:limit]]


def _json_from_text(text: str) -> dict:
    raw = (text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I)
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except Exception:
        match = re.search(r"\{.*\}", raw, flags=re.S)
        if match:
            try:
                value = json.loads(match.group(0))
                return value if isinstance(value, dict) else {}
            except Exception:
                pass
    return {}


def identificar_foto(data: bytes) -> str:
    if not GEMINI:
        raise RuntimeError("La búsqueda por foto necesita GEMINI_API_KEY. También podés buscar escribiendo el nombre.")
    with Image.open(io.BytesIO(data)) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        image.thumbnail((1100, 1100), Image.Resampling.LANCZOS)
    prompt = (
        "Analizá la foto de un producto comercial. Leé únicamente lo que sea visible en la etiqueta. "
        "Identificá marca, nombre/línea, aroma o variante, tipo de producto y palabras legibles. "
        "No inventes información. Respondé SOLO JSON válido con las claves: marca, producto, variante, "
        "tipo, texto_visible, consulta. En consulta combiná los términos más útiles para encontrar la "
        "publicación exacta en el catálogo, sin frases genéricas como 'producto de limpieza'."
    )
    response = GEMINI.models.generate_content(
        model=GEMINI_MODEL,
        contents=[image, prompt],
        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.1),
    )
    obj = _json_from_text(response.text or "")
    parts = [obj.get(k, "") for k in ("marca", "producto", "variante", "tipo", "texto_visible", "consulta")]
    query = " ".join(str(x).strip() for x in parts if str(x or "").strip())
    if not query:
        raise RuntimeError("No pude leer con claridad el nombre o la etiqueta. Probá enviando una foto más nítida o escribí el nombre.")
    return query


def obtener_fotos_publicacion(item_id: str) -> dict:
    if not ML_PROXY_API_KEY:
        raise RuntimeError("Falta configurar ML_PROXY_API_KEY en las variables del bot.")
    data = _http_json(
        "GET", ML_PROXY_URL,
        headers={"X-API-Key": ML_PROXY_API_KEY},
        params={"action": "imagenes_publicacion", "itemId": item_id},
    )
    if not data.get("ok"):
        raise RuntimeError(data.get("error") or "No se pudieron recuperar las fotos de Mercado Libre.")
    return data


def descargar_imagen(url: str, max_side: int | None = None, quality: int = 88) -> bytes:
    r = HTTP.get(url, timeout=(8, 30), stream=True)
    r.raise_for_status()
    data = bytearray()
    for chunk in r.iter_content(65536):
        data.extend(chunk)
        if len(data) > 15_000_000:
            raise RuntimeError("La imagen supera el tamaño permitido para procesarla.")
    with Image.open(io.BytesIO(data)) as im:
        image = ImageOps.exif_transpose(im).convert("RGB")
        if max_side:
            image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=quality, optimize=True)
        return out.getvalue()


def adaptar_formato(data: bytes, size: tuple[int, int]) -> bytes:
    with Image.open(io.BytesIO(data)) as im:
        original = ImageOps.exif_transpose(im).convert("RGB")
        W, H = size
        # Fondo derivado de la misma foto, suavemente desenfocado; el producto no se recorta.
        background = ImageOps.fit(original, (W, H), method=Image.Resampling.LANCZOS)
        background = background.filter(ImageFilter.GaussianBlur(radius=max(16, int(min(W, H) * 0.035))))
        overlay = ImageOps.contain(original, (int(W * 0.94), int(H * 0.94)), method=Image.Resampling.LANCZOS)
        x, y = (W - overlay.width) // 2, (H - overlay.height) // 2
        background.paste(overlay, (x, y))
        out = io.BytesIO()
        background.save(out, format="JPEG", quality=93, optimize=True)
        return out.getvalue()


def generar_descripcion(producto: dict, formato: str) -> str:
    if not GEMINI:
        raise RuntimeError("Falta GEMINI_API_KEY para redactar la descripción con IA.")
    spec = FORMATOS[formato]
    info = {
        "publicacion": producto.get("nombre_publicacion", ""),
        "producto_en_pos": producto.get("detalle", ""),
        "marca": producto.get("marca", ""),
        "sku": producto.get("sku", ""),
        "codigo_interno": producto.get("codigo", ""),
        "rubro": producto.get("rubro", ""),
    }
    prompt = f"""Sos redactor publicitario de {BRAND}. Escribí en español rioplatense natural, atractivo y vendedor.
Datos reales disponibles del producto: {json.dumps(info, ensure_ascii=False)}
Formato elegido: {spec['nombre']}.
Objetivo: {spec['instruccion']}
Límite absoluto: {spec['max_chars']} caracteres, contando espacios y saltos de línea.
Reglas: no inventes ingredientes, propiedades, duración, tamaño, beneficios, precios ni promociones. No repitas datos que no estén confirmados. Usá un inicio que llame la atención, tono cálido y un llamado a la acción breve. Evitá hashtags genéricos en exceso. Devolvé solamente el texto final, sin explicar nada."""
    response = GEMINI.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(temperature=0.75),
    )
    text = (response.text or "").strip()
    if not text:
        raise RuntimeError("La IA no devolvió una descripción.")
    # Recorte defensivo en límite de palabra, por si el modelo excede lo pedido.
    limit = spec["max_chars"]
    if len(text) > limit:
        clipped = text[:limit]
        if " " in clipped:
            clipped = clipped.rsplit(" ", 1)[0]
        text = clipped.rstrip(" ,.;:!\n")
    return text


# ───────────────────────── UI Telegram ─────────────────────────
def menu_principal() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Buscar publicación por nombre / SKU", callback_data="buscar_nombre")],
        [InlineKeyboardButton("📸 Buscar publicación enviando una foto", callback_data="buscar_foto")],
        [InlineKeyboardButton("🆕 Nueva búsqueda", callback_data="nuevo")],
    ])


def _titulo_producto(p: dict) -> str:
    return (p.get("nombre_publicacion") or p.get("detalle") or p.get("item_id") or "Publicación")[:180]


def teclado_productos(productos: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for i, p in enumerate(productos):
        label = f"{_titulo_producto(p)} · {p.get('item_id', '')}"
        rows.append([InlineKeyboardButton(label[:60], callback_data=f"prod:{i}")])
    rows.append([InlineKeyboardButton("↩️ Menú principal", callback_data="menu")])
    return InlineKeyboardMarkup(rows)


def teclado_formatos() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟦 Feed · 4:5", callback_data="formato:feed")],
        [InlineKeyboardButton("📱 Historia · 9:16", callback_data="formato:story")],
        [InlineKeyboardButton("🎬 Reel · portada 9:16", callback_data="formato:reel")],
        [InlineKeyboardButton("↩️ Menú principal", callback_data="menu")],
    ])


def teclado_final() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Regenerar descripción", callback_data="regenerar")],
        [InlineKeyboardButton("📐 Cambiar formato", callback_data="cambiar_formato")],
        [InlineKeyboardButton("🖼 Elegir otra foto", callback_data="otra_foto")],
        [InlineKeyboardButton("🆕 Nueva búsqueda", callback_data="nuevo")],
    ])


def autorizado(update: Update) -> bool:
    user = update.effective_user
    return not ALLOWED or bool(user and user.id in ALLOWED)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not autorizado(update):
        await update.effective_message.reply_text("No tenés autorización para usar este bot.")
        return
    context.user_data.clear()
    await update.effective_message.reply_text(
        f"👋 ¡Hola! Soy el asistente de {BRAND}.\n\n"
        "Puedo buscar una publicación de tu cuenta de Mercado Libre usando el catálogo vinculado a tu POS. "
        "Después te muestro las fotos disponibles para que elijas una, adapto la imagen al formato y preparo su descripción.",
        reply_markup=menu_principal(),
    )


async def buscar_por_texto(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str) -> None:
    msg = await update.effective_message.reply_text("🔎 Buscando en las publicaciones vinculadas de tu POS…")
    try:
        catalogo = await asyncio.to_thread(obtener_catalogo)
        if not catalogo:
            await msg.edit_text("Todavía no hay publicaciones asociadas disponibles en el catálogo del POS.")
            return
        candidatos = await asyncio.to_thread(buscar_candidatos, query, catalogo)
        if not candidatos:
            await msg.edit_text("No encontré coincidencias. Probá con el nombre de la publicación, marca, SKU o código del POS.")
            return
        context.user_data["matches"] = candidatos
        context.user_data["state"] = "select_product"
        await msg.edit_text(
            f"Encontré {len(candidatos)} coincidencia(s). Elegí la publicación correcta:",
            reply_markup=teclado_productos(candidatos),
        )
    except Exception as exc:
        logger.exception("Error buscando en el catálogo POS")
        await msg.edit_text(f"⚠️ No pude consultar el catálogo del POS: {str(exc)[:300]}")


async def procesar_foto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not autorizado(update):
        await update.effective_message.reply_text("No tenés autorización para usar este bot.")
        return
    status = await update.effective_message.reply_text("📸 Leyendo la etiqueta y buscando el producto en tu catálogo…")
    try:
        tg_file = await update.effective_message.photo[-1].get_file()
        photo_bytes = bytes(await tg_file.download_as_bytearray())
        query = await asyncio.to_thread(identificar_foto, photo_bytes)
        catalogo = await asyncio.to_thread(obtener_catalogo)
        candidatos = await asyncio.to_thread(buscar_candidatos, query, catalogo)
        if not candidatos:
            await status.edit_text(
                "Pude leer algunos datos de la foto, pero no encontré una publicación vinculada que coincida.\n\n"
                f"Lo que pude identificar: {query[:350]}\nPodés probar buscando por nombre o SKU desde el menú.",
                reply_markup=menu_principal(),
            )
            return
        context.user_data["matches"] = candidatos
        context.user_data["state"] = "select_product"
        await status.edit_text(
            f"🔎 Leí la foto y encontré {len(candidatos)} posible(s) publicación(es). "
            "Elegí cuál corresponde al producto que enviaste:",
            reply_markup=teclado_productos(candidatos),
        )
    except Exception as exc:
        logger.exception("Error identificando producto desde foto")
        await status.edit_text(f"⚠️ No pude identificar el producto: {str(exc)[:300]}", reply_markup=menu_principal())


async def mostrar_fotos(update: Update, context: ContextTypes.DEFAULT_TYPE, producto: dict) -> None:
    query = update.callback_query
    await query.answer("Buscando fotos de la publicación…")
    status = await query.message.reply_text("🖼 Consultando las fotos originales de Mercado Libre…")
    try:
        data = await asyncio.to_thread(obtener_fotos_publicacion, producto["item_id"])
        fotos = data.get("fotos") or []
        if not fotos:
            await status.edit_text("Esta publicación no tiene fotos disponibles para recuperar.")
            return
        context.user_data["selected_product"] = producto
        context.user_data["photos"] = fotos[:12]
        context.user_data["state"] = "select_photo"
        await status.edit_text(
            f"✅ Publicación: {_titulo_producto(producto)}\n"
            f"Encontré {len(fotos)} foto(s). Elegí una:",
        )
        for i, foto in enumerate(context.user_data["photos"]):
            try:
                preview = await asyncio.to_thread(descargar_imagen, foto["url"], 900, 82)
                await query.message.reply_photo(
                    photo=preview,
                    caption=f"Foto {i + 1} de {len(context.user_data['photos'])}",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(f"✅ Elegir foto {i + 1}", callback_data=f"foto:{i}")
                    ]]),
                )
            except Exception:
                logger.exception("No se pudo mostrar la foto %s del item %s", i, producto.get("item_id"))
        await query.message.reply_text("Después de elegir una foto, te voy a preguntar el formato.")
    except Exception as exc:
        logger.exception("Error recuperando fotos ML")
        await status.edit_text(f"⚠️ No pude recuperar las fotos de Mercado Libre: {str(exc)[:300]}")


async def terminar_imagen(update: Update, context: ContextTypes.DEFAULT_TYPE, formato: str) -> None:
    query = update.callback_query
    spec = FORMATOS[formato]
    await query.answer("Adaptando imagen y redactando descripción…")
    status = await query.message.reply_text(f"📐 Adaptando a {spec['nombre']}…")
    try:
        url = context.user_data.get("selected_photo_url")
        producto = context.user_data.get("selected_product") or {}
        if not url or not producto:
            await status.edit_text("Se perdió la selección. Empezá una nueva búsqueda desde el menú.")
            return
        original = await asyncio.to_thread(descargar_imagen, url, None, 92)
        final = await asyncio.to_thread(adaptar_formato, original, spec["size"])
        descripcion = await asyncio.to_thread(generar_descripcion, producto, formato)
        context.user_data["selected_image"] = final
        context.user_data["format"] = formato
        context.user_data["state"] = "final"
        await status.delete()
        await query.message.reply_photo(
            photo=final,
            caption=f"✅ Imagen adaptada para {spec['nombre']} ({spec['size'][0]}×{spec['size'][1]}).",
        )
        # Telegram limita el texto de la leyenda de fotos; el copy se envía aparte.
        await query.message.reply_text(
            f"📝 Descripción sugerida · {len(descripcion)}/{spec['max_chars']} caracteres\n\n{descripcion}",
            reply_markup=teclado_final(),
        )
    except Exception as exc:
        logger.exception("Error preparando imagen o descripción")
        await status.edit_text(f"⚠️ No pude terminar la imagen: {str(exc)[:300]}")


async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not autorizado(update):
        await query.answer("No autorizado", show_alert=True)
        return
    data = query.data or ""
    try:
        if data in ("menu", "nuevo"):
            context.user_data.clear()
            await query.answer()
            await query.message.reply_text("¿Cómo querés buscar la publicación?", reply_markup=menu_principal())
        elif data == "buscar_nombre":
            await query.answer()
            context.user_data["state"] = "waiting_query"
            await query.message.reply_text("Escribime el nombre, marca, SKU o código del producto.")
        elif data == "buscar_foto":
            await query.answer()
            context.user_data["state"] = "waiting_photo"
            await query.message.reply_text("Mandame una foto clara del producto, preferentemente donde se lea la etiqueta.")
        elif data.startswith("prod:"):
            idx = int(data.split(":", 1)[1])
            matches = context.user_data.get("matches") or []
            if idx < 0 or idx >= len(matches):
                await query.answer("Esa selección ya venció. Buscá de nuevo.", show_alert=True)
                return
            await mostrar_fotos(update, context, matches[idx])
        elif data.startswith("foto:"):
            idx = int(data.split(":", 1)[1])
            photos = context.user_data.get("photos") or []
            if idx < 0 or idx >= len(photos):
                await query.answer("Esa foto ya no está disponible. Buscá de nuevo.", show_alert=True)
                return
            context.user_data["selected_photo_url"] = photos[idx]["url"]
            context.user_data["state"] = "select_format"
            await query.answer("Foto elegida")
            await query.message.reply_text("¿Para qué formato querés preparar esta imagen?", reply_markup=teclado_formatos())
        elif data.startswith("formato:"):
            formato = data.split(":", 1)[1]
            if formato not in FORMATOS:
                await query.answer("Formato no válido", show_alert=True)
                return
            await terminar_imagen(update, context, formato)
        elif data == "regenerar":
            formato = context.user_data.get("format")
            producto = context.user_data.get("selected_product") or {}
            if formato not in FORMATOS or not producto:
                await query.answer("Empezá una nueva búsqueda", show_alert=True)
                return
            await query.answer("Generando otra versión…")
            text = await asyncio.to_thread(generar_descripcion, producto, formato)
            await query.message.reply_text(
                f"📝 Descripción sugerida · {len(text)}/{FORMATOS[formato]['max_chars']} caracteres\n\n{text}",
                reply_markup=teclado_final(),
            )
        elif data == "cambiar_formato":
            await query.answer()
            await query.message.reply_text("Elegí el nuevo formato:", reply_markup=teclado_formatos())
        elif data == "otra_foto":
            await query.answer()
            await query.message.reply_text("Elegí otra de las fotos de la publicación anterior:")
            for i, foto in enumerate(context.user_data.get("photos") or []):
                try:
                    preview = await asyncio.to_thread(descargar_imagen, foto["url"], 800, 80)
                    await query.message.reply_photo(photo=preview, caption=f"Foto {i + 1}", reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(f"✅ Elegir foto {i + 1}", callback_data=f"foto:{i}")
                    ]]))
                except Exception:
                    logger.exception("No se pudo volver a mostrar foto %s", i)
        else:
            await query.answer("Opción no reconocida", show_alert=True)
    except Exception:
        logger.exception("Error en callback %s", data)
        await query.message.reply_text("⚠️ Ocurrió un error procesando esa opción. Probá de nuevo desde el menú.", reply_markup=menu_principal())


async def texto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not autorizado(update):
        await update.effective_message.reply_text("No tenés autorización para usar este bot.")
        return
    state = context.user_data.get("state")
    value = (update.effective_message.text or "").strip()
    if state == "waiting_query":
        await buscar_por_texto(update, context, value)
    else:
        await update.effective_message.reply_text("Usá el menú para buscar una publicación.", reply_markup=menu_principal())


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"Vilo Aromas Bot activo"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


def run_health_server() -> None:
    try:
        ThreadingHTTPServer(("0.0.0.0", PORT), HealthHandler).serve_forever()
    except Exception:
        logger.exception("No se pudo iniciar el servidor de salud")


async def ejecutar_aplicacion(app) -> None:
    """Inicia PTB usando asyncio.run, sin depender de get_event_loop() global."""
    detener = asyncio.Event()
    loop = asyncio.get_running_loop()

    # Render envía SIGTERM al detener o reemplazar una instancia.
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, detener.set)
        except (NotImplementedError, RuntimeError):
            # En plataformas sin soporte de señales, asyncio.run sigue
            # gestionando la cancelación normal del proceso.
            pass

    await app.initialize()
    try:
        if app.updater is None:
            raise RuntimeError("La aplicación no tiene Updater habilitado.")

        # Mismo orden de inicio que recomienda python-telegram-bot:
        # initialize -> updater.start_polling -> application.start.
        await app.updater.start_polling(drop_pending_updates=True)
        await app.start()
        logger.info("Bot de Telegram iniciado correctamente; esperando mensajes.")
        await detener.wait()
    finally:
        if app.updater is not None and app.updater.running:
            await app.updater.stop()
        if app.running:
            await app.stop()
        await app.shutdown()


def main() -> None:
    if not TELEGRAM_TOKEN:
        raise RuntimeError("Falta TELEGRAM_TOKEN.")
    if not POS_API_KEY:
        logger.warning("Falta POS_API_KEY: no se podrán consultar los productos vinculados.")
    if not ML_PROXY_API_KEY:
        logger.warning("Falta ML_PROXY_API_KEY: no se podrán consultar fotos de las publicaciones.")
    if not GEMINI_API_KEY:
        logger.warning("Falta GEMINI_API_KEY: la búsqueda por foto y las descripciones IA no estarán disponibles.")

    threading.Thread(target=run_health_server, daemon=True).start()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler(["start", "nuevo"], start))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler(filters.PHOTO, procesar_foto))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, texto))

    # asyncio.run crea, ejecuta y cierra un bucle de eventos propio. Esto evita
    # el RuntimeError de Python 3.14 al usar Application.run_polling().
    asyncio.run(ejecutar_aplicacion(app))


if __name__ == "__main__":
    main()
