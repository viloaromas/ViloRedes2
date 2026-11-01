import os
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
import google.generativeai as genai

# Lee las credenciales de forma segura desde las variables de entorno de Render
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Inicializar Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-2.5-flash')

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    chat_id = update.message.chat_id

    await context.bot.send_message(chat_id=chat_id, text="🎨 Creando propuesta para tus redes...")

    try:
        prompt_completo = (
            f"Actúa como un experto en marketing visual y community manager para una tienda "
            f"de aromatización y productos de limpieza. Responde de forma creativa, redactando copys atractivos, "
            f"ideas de fondos visuales o hashtags para esta solicitud: {user_message}"
        )
        
        response = model.generate_content(prompt_completo)
        await context.bot.send_message(chat_id=chat_id, text=response.text)
        
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"Ocurrió un error: {str(e)}")

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    print("¡Bot en marcha en Render!")
    app.run_polling()
