import os
import logging
import time
from flask import Flask, request, jsonify
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, ContextTypes
from telegram.ext.filters import TEXT as TEXT_FILTER
import google.generativeai as genai
import pdfplumber
import asyncio

#    -
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# --- การดึง Bot Token และ Gemini API Key จาก Environment Variables ---
# **สำคัญ:** เราจะดึงค่าเหล่านี้จาก Environment Variables บน Render.com เพื่อความปลอดภัย
# สำหรับการทดสอบบนเครื่องตัวเอง คุณสามารถตั้งค่าได้ชั่วคราว
# แต่แนะนำให้ใช้ Environment Variable จริงๆ เมื่อ Deploy
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not BOT_TOKEN:
    logger.error("!!! ERROR: BOT_TOKEN environment variable is not set. Please set it. !!!")
    # ในโหมดพัฒนา สามารถตั้งค่าตรงนี้ได้ชั่วคราว เช่น BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN_HERE"
    exit(1)
if not GEMINI_API_KEY:
    logger.error("!!! ERROR: GEMINI_API_KEY environment variable is not set. Please set it. !!!")
    # ในโหมดพัฒนา สามารถตั้งค่าตรงนี้ได้ชั่วคราว เช่น GEMINI_API_KEY = "YOUR_GEMINI_API_KEY_HERE"
    exit(1)

# --- ตั้งค่า Gemini API ---
genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel('gemini-2.0-flash')

def read_pdf_text(file_path):
    text = ""
    try:
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                text += page.extract_text()
    except Exception as e:
        logging.error(f"Error reading PDF file: {e}")
        return None
    return text

# อ่านเนื้อหาจากไฟล์ PDF ตั้งแต่เริ่มต้น
PDF_CONTEXT_TEXT = read_pdf_text("dataNVC.pdf")

# (โค้ดส่วนอื่น ๆ เช่น log, flask app)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # (โค้ดสำหรับดึงข้อความจากผู้ใช้)
    user_message = update.message.text
    chat_id = update.effective_chat.id

    # สร้าง prompt_parts สำหรับส่งให้ Gemini
    prompt_parts = [
        # เพิ่มข้อมูลบริบทจาก PDF เข้าไปใน Prompt
        f"ข้อมูลการศึกษาต่อวิทยาลัยอาชีวศึกษานครศรีธรรมราช:\n{PDF_CONTEXT_TEXT}\n\n"
        f"จากข้อมูลข้างต้น, โปรดตอบคำถามนี้:\n{user_message}",
    ]



# --- สร้าง Application instance สำหรับ Telegram Bot ---
application = Application.builder().token(BOT_TOKEN).build()

# --- Handler สำหรับคำสั่ง /start ---
async def start_command(update: Update, context):
    start_time = time.time()
    user_name = update.message.from_user.first_name if update.message.from_user else "ผู้ใช้งาน"
    chat_id = update.message.chat_id
    logger.info(f"Received /start command from {user_name} ({chat_id}) at {time.strftime('%H:%M:%S', time.localtime(start_time))}")

    response_text = f"สวัสดีครับคุณ {user_name}! ผมคือบอทผู้ช่วยข้อมูลวิทยาลัยอาชีวศึกษานครศรีธรรมราชครับ\n" \
                    "ผมสามารถตอบคำถามเกี่ยวกับ **หลักสูตร, การรับสมัคร, ที่ตั้ง, ช่องทางการติดต่อ และข้อมูลอื่นๆ** ของวิทยาลัยฯ ได้ครับ\n" \
                    "คุณอยากสอบถามเรื่องอะไรเป็นพิเศษไหมครับ?"
    
    try:
        await context.bot.send_message(chat_id=chat_id, text=response_text)
        end_time = time.time()
        logger.info(f"Sent /start response to {user_name} ({chat_id}). Total processing time: {end_time - start_time:.4f} seconds")
    except Exception as e:
        logger.error(f"Error sending start response to {chat_id}: {e}")

# --- Handler สำหรับข้อความทั่วไป (Core Logic พร้อม Gemini API) ---
async def handle_message(update: Update, context):
    start_time = time.time()
    if update.message and update.message.text:
        user_message = update.message.text
        chat_id = update.message.chat_id
        user_name = update.message.from_user.first_name if update.message.from_user else "ผู้ใช้งาน"

        logger.info(f"Received message from {user_name} ({chat_id}): \"{user_message}\" at {time.strftime('%H:%M:%S', time.localtime(start_time))}")

        try:
            # สร้าง Prompt สำหรับ Gemini API
            # เราจะรวมข้อมูล Context ของวิทยาลัยฯ และคำถามของผู้ใช้เข้าด้วยกัน #COLLEGE_CONTEXT
            gemini_prompt = f"""
            คุณคือผู้ช่วยให้ข้อมูลเกี่ยวกับวิทยาลัยอาชีวศึกษานครศรีธรรมราช
            โปรดตอบคำถามต่อไปนี้โดยอ้างอิงจากข้อมูลที่ให้มาเท่านั้น หากคำถามไม่เกี่ยวกับข้อมูลที่ให้มา หรือคุณไม่พบคำตอบในข้อมูลที่ให้มา
            ให้ตอบว่า "ขออภัยครับ ผมไม่สามารถให้ข้อมูลในเรื่องนี้ได้ในขณะนี้"

            ข้อมูลวิทยาลัยอาชีวศึกษานครศรีธรรมราช:
            {PDF_CONTEXT_TEXT}

            คำถามของผู้ใช้:
            {user_message}

            คำตอบ:
            """
            
            # ส่ง Prompt ไปยัง Gemini API และรับคำตอบ
            gemini_response = gemini_model.generate_content(gemini_prompt)
            response_text = gemini_response.text
            
            # ตรวจสอบ response_text หาก Gemini ไม่ให้ข้อความกลับมา
            if not response_text:
                response_text = "ขออภัยครับ เกิดข้อผิดพลาดในการประมวลผลคำถามของคุณ กรุณาลองใหม่อีกครั้งครับ"

            await context.bot.send_message(chat_id=chat_id, text=response_text)
            
            end_time = time.time()
            logger.info(f"Sent response to {user_name} ({chat_id}). Total processing time: {end_time - start_time:.4f} seconds")

        except Exception as e:
            logger.error(f"Error processing message with Gemini API for {chat_id}: {e}")
            # ส่งข้อความ Error กลับไปหาผู้ใช้
            await context.bot.send_message(chat_id=chat_id, text="ขออภัยครับ เกิดข้อผิดพลาดในการประมวลผลคำถามของคุณ กรุณาลองใหม่อีกครั้งครับ")
    else:
        logger.info("Received an update without a text message.")


# --- การเพิ่ม Handler เข้าสู่ Application ---
application.add_handler(CommandHandler("start", start_command))
application.add_handler(MessageHandler(TEXT_FILTER, handle_message))


# --- Webhook Endpoint ของ Flask ---
@app.route(f'/{BOT_TOKEN}', methods=['POST'])
async def webhook():
    start_time_webhook = time.time()
    if request.method == "POST":
        json_data = request.get_json(force=True)
        logger.info(f"Received webhook data: {json_data}")
        
        try:
            async with application:
                update = Update.de_json(json_data, application.bot)
                await application.process_update(update)
            logger.info("Update processed successfully within webhook.")
            return jsonify({"status": "ok"})
        except Exception as e:
            end_time_webhook = time.time()
            logger.error(f"Error processing update in webhook: {e}. Total webhook processing time: {end_time_webhook - start_time_webhook:.4f} seconds")
            return jsonify({"status": "error", "message": str(e)}), 400
            
    logger.warning(f"Received non-POST request to webhook endpoint. Method: {request.method}")
    return jsonify({"status": "method not allowed"}), 405


# --- ส่วนสำหรับรัน Flask App ---
if __name__ == '__main__':
    logger.info("Starting Flask app...")
    # เราจะไม่รัน app.run() โดยตรงบน Render.com แต่จะใช้ Gunicorn
    # โค้ดนี้สำหรับการทดสอบบน Local PC เท่านั้น
    # เพื่อให้รันได้บนเครื่องตัวเองชั่วคราว ให้ตั้งค่า FLASK_ENV=development ก่อนรัน
    if os.getenv("FLASK_ENV") == "development":
        logger.info("Running Flask in development mode (local testing).")
        app.run(host='0.0.0.0', port=5000, debug=True)
    else:
        logger.info("Running in production mode (for Render.com), Gunicorn will handle the app.")