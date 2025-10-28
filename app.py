import os
import logging
import time
import asyncio
from flask import Flask, request, jsonify
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, ContextTypes
from telegram.ext.filters import TEXT as TEXT_FILTER
import google.generativeai as genai
import pdfplumber
from supabase import create_client, Client
from dotenv import load_dotenv # เพิ่มการใช้ dotenv เพื่อการพัฒนาบน Local ที่ง่ายขึ้น

# โหลด Environment Variables จากไฟล์ .env (สำหรับการพัฒนาบน Local เท่านั้น)
# บน Render.com หรือ Production จะอ่านจาก Environment Variables โดยตรง
load_dotenv() 

# --- การตั้งค่า Logging ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# --- ดึง Bot Token, Gemini API Key, Supabase URL/Key จาก Environment Variables ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# ตรวจสอบว่า Environment Variables สำคัญถูกตั้งค่าหรือไม่
if not BOT_TOKEN:
    logger.critical("!!! CRITICAL ERROR: BOT_TOKEN environment variable is not set. Exiting. !!!")
    exit(1)
if not GEMINI_API_KEY:
    logger.critical("!!! CRITICAL ERROR: GEMINI_API_KEY environment variable is not set. Exiting. !!!")
    exit(1)

# --- ตั้งค่า Gemini API ---
try:
    genai.configure(api_key=GEMINI_API_KEY)
    gemini_model = genai.GenerativeModel('gemini-2.0-flash') # 'gemini-1.5-flash-latest' หรือ 'gemini-2.0-flash'
    logger.info("Gemini API configured successfully with 'gemini-2.0-flash' model.")
except Exception as e:
    logger.critical(f"!!! CRITICAL ERROR: Failed to configure Gemini API: {e}. Exiting. !!!")
    exit(1)

# --- เชื่อมต่อ Supabase ---
supabase: Client | None = None # กำหนด Type Hint ให้ชัดเจน
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        logger.info("Supabase client created successfully.")
    except Exception as e:
        logger.error(f"Error connecting to Supabase: {e}. Supabase features disabled.")
        supabase = None
else:
    logger.warning("SUPABASE_URL or SUPABASE_KEY not found. Supabase features disabled.")

# --- Supabase Helper Functions ---
def save_chat_history(chat_id: int, sender: str, message: str):
    """บันทึกข้อความลงใน Supabase chat_history table."""
    if not supabase:
        logger.debug("Supabase not initialized, skipping chat history save.")
        return
    try:
        data = {
            "chat_id": str(chat_id),
            "sender": sender,
            "message": message
        }
        supabase.table('chat_history').insert(data).execute()
        logger.debug(f"Saved chat history for chat_id {chat_id}, sender {sender}.")
    except Exception as e:
        logger.error(f"Error saving chat history to Supabase for chat_id {chat_id}: {e}")

def get_chat_history(chat_id: int, limit: int = 6) -> str:
    """ดึงประวัติการแชทล่าสุดจาก Supabase และจัดรูปแบบ."""
    if not supabase:
        logger.debug("Supabase not initialized, returning empty chat history.")
        return ""
    try:
        response = supabase.table('chat_history').select('sender, message') \
            .eq('chat_id', str(chat_id)) \
            .order('created_at', desc=True) \
            .limit(limit) \
            .execute()
        
        history_list = response.data
        if not history_list:
            logger.debug(f"No chat history found for chat_id {chat_id}.")
            return ""

        # จัดรูปแบบประวัติการแชทให้ Gemini เข้าใจ (เรียงจากเก่าไปใหม่)
        formatted_history = "\n--- Chat History (Oldest to Newest) ---\n"
        for item in reversed(history_list): 
            formatted_history += f"[{item['sender'].upper()}]: {item['message']}\n"
        formatted_history += "--- End Chat History ---\n"
        
        logger.debug(f"Fetched chat history for chat_id {chat_id}.")
        return formatted_history
    except Exception as e:
        logger.error(f"Error fetching chat history from Supabase for chat_id {chat_id}: {e}")
        return ""

# --- ดึง Prompt Context จาก PDF ---
def read_pdf_text(file_path):
    """อ่านข้อความจากไฟล์ PDF ที่กำหนด."""
    text = ""
    if not os.path.exists(file_path):
        logger.critical(f"!!! CRITICAL ERROR: PDF file not found at {file_path}. Exiting. !!!")
        exit(1)
    try:
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                text += page.extract_text() or "" # เพิ่ม or "" เพื่อป้องกัน None
        logger.info(f"Successfully read context from {file_path}.")
    except Exception as e:
        logger.critical(f"!!! CRITICAL ERROR: Error reading PDF file {file_path}: {e}. Exiting. !!!")
        exit(1) # หากอ่าน PDF ไม่ได้ ถือว่าเป็นข้อผิดพลาดร้ายแรงสำหรับบอทนี้
    return text

PDF_CONTEXT_TEXT = read_pdf_text("dataNVC.pdf")

# --- Telegram Bot Handlers ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ตอบกลับเมื่อผู้ใช้ส่งคำสั่ง /start."""
    start_time = time.time()
    user_name = update.message.from_user.first_name if update.message.from_user else "ผู้ใช้งาน"
    chat_id = update.message.chat_id
    logger.info(f"Received /start command from {user_name} ({chat_id})")

    response_text = (
        f"สวัสดีครับคุณ {user_name}! ผมคือบอทผู้ช่วยข้อมูลวิทยาลัยอาชีวศึกษานครศรีธรรมราชครับ\n"
        "ผมสามารถตอบคำถามเกี่ยวกับ **หลักสูตร, การรับสมัคร, ที่ตั้ง, ช่องทางการติดต่อ และข้อมูลอื่นๆ** ของวิทยาลัยฯ ได้ครับ\n"
        "คุณอยากสอบถามเรื่องอะไรเป็นพิเศษไหมครับ?"
    )
    
    try:
        await context.bot.send_message(chat_id=chat_id, text=response_text)
        logger.info(f"Sent /start response to {user_name} ({chat_id}). Time: {time.time() - start_time:.4f}s")
    except Exception as e:
        logger.error(f"Error sending start response to {chat_id}: {e}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """จัดการข้อความทั่วไปจากผู้ใช้ ประมวลผลด้วย Gemini API และบันทึกประวัติการแชท."""
    start_time = time.time()
    if not update.message or not update.message.text:
        logger.info("Received an update without a text message, ignoring.")
        return

    user_message = update.message.text
    chat_id = update.message.chat_id
    user_name = update.message.from_user.first_name if update.message.from_user else "ผู้ใช้งาน"

    logger.info(f"Processing message from {user_name} ({chat_id}): \"{user_message}\"")

    try:
        # 1. บันทึกข้อความผู้ใช้
        save_chat_history(chat_id, 'user', user_message)

        # 2. ดึงประวัติการแชทและข้อมูล PDF
        chat_history_text = get_chat_history(chat_id, limit=8) # อาจเพิ่ม limit เป็น 8 หรือ 10 เพื่อให้ได้บริบทที่ยาวขึ้น
        
        # 3. สร้าง Prompt สำหรับ Gemini API
        # ปรับปรุง Prompt ให้ชัดเจนขึ้นและเน้นการอ้างอิงข้อมูลที่ให้มาเท่านั้น
        gemini_prompt = f"""
        คุณคือแชทบอทผู้เชี่ยวชาญด้านข้อมูลของวิทยาลัยอาชีวศึกษานครศรีธรรมราช
        งานของคุณคือตอบคำถามของผู้ใช้เกี่ยวกับการศึกษาต่อ หลักสูตร การรับสมัคร และข้อมูลทั่วไปของวิทยาลัยฯ โดยยึดตาม "ข้อมูลบริบทของวิทยาลัย" ที่ให้มาเท่านั้น
        หากคำถามของผู้ใช้ไม่เกี่ยวข้องกับ "ข้อมูลบริบทของวิทยาลัย" หรือคุณไม่พบคำตอบในข้อมูลที่ให้มา
        ให้ตอบว่า "ขออภัยครับ ผมไม่สามารถให้ข้อมูลในเรื่องนี้ได้ในขณะนี้" และแนะนำให้ติดต่อวิทยาลัยโดยตรงหากเป็นเรื่องเฉพาะทาง.

        --- ข้อมูลบริบทของวิทยาลัย ---
        {PDF_CONTEXT_TEXT}

        --- ประวัติการสนทนา (เพื่อทำความเข้าใจบริบทต่อเนื่อง) ---
        {chat_history_text if chat_history_text else "ไม่มีประวัติการสนทนาก่อนหน้า."}

        --- คำถามใหม่ของผู้ใช้ ---
        {user_message}

        คำตอบ:
        """
        
        # 4. ส่ง Prompt ไปยัง Gemini API และรับคำตอบ
        gemini_response = gemini_model.generate_content(gemini_prompt)
        
        # ตรวจสอบ Response จาก Gemini อย่างละเอียด
        response_text = ""
        if gemini_response and gemini_response.text:
            response_text = gemini_response.text
        elif gemini_response and gemini_response.parts:
            # บางครั้ง Gemini อาจคืนเป็น parts ถ้า .text ไม่ได้ถูกตั้งค่า
            response_text = "".join(part.text for part in gemini_response.parts if hasattr(part, 'text'))
        
        if not response_text.strip(): # ตรวจสอบอีกครั้งว่ามีข้อความหรือไม่
            response_text = "ขออภัยครับ เกิดข้อผิดพลาดในการประมวลผลคำถามของคุณ หรือ Gemini ไม่สามารถสร้างคำตอบที่เหมาะสมได้ในขณะนี้ กรุณาลองใหม่อีกครั้งครับ"
            logger.warning(f"Gemini returned empty or invalid response for chat_id {chat_id}.")

        await context.bot.send_message(chat_id=chat_id, text=response_text)
        
        # 5. บันทึกคำตอบของบอท
        save_chat_history(chat_id, 'bot', response_text)

        logger.info(f"Responded to {user_name} ({chat_id}). Time: {time.time() - start_time:.4f}s")

    except genai.types.BlockedPromptException as e:
        # จัดการกรณีที่ Gemini บล็อก Prompt (เช่น มีเนื้อหาที่ไม่เหมาะสม)
        logger.warning(f"Gemini BlockedPromptException for chat_id {chat_id}: {e}")
        await context.bot.send_message(chat_id=chat_id, text="ขออภัยครับ คำถามของคุณอาจมีเนื้อหาที่ไม่เหมาะสม ผมไม่สามารถประมวลผลได้ครับ")
    except Exception as e:
        logger.error(f"Unhandled error in handle_message for chat_id {chat_id}: {e}", exc_info=True)
        await context.bot.send_message(chat_id=chat_id, text="ขออภัยครับ เกิดข้อผิดพลาดทางเทคนิค กรุณาลองใหม่อีกครั้งครับ")


# --- สร้าง Application instance สำหรับ Telegram Bot ---
application = Application.builder().token(BOT_TOKEN).build()

# --- เพิ่ม Handler เข้าสู่ Application ---
application.add_handler(CommandHandler("start", start_command))
application.add_handler(MessageHandler(TEXT_FILTER, handle_message))


# --- Webhook Endpoint ของ Flask ---
@app.route(f'/{BOT_TOKEN}', methods=['POST'])
async def webhook():
    """รับและประมวลผล Webhook จาก Telegram."""
    start_time_webhook = time.time()
    if request.method != "POST":
        logger.warning(f"Received non-POST request to webhook endpoint. Method: {request.method}")
        return jsonify({"status": "method not allowed"}), 405
    
    json_data = request.get_json(force=True)
    logger.debug(f"Received webhook data: {json_data}") # เปลี่ยนเป็น debug เพื่อลด log verbosity

    try:
        # ใช้ application.update_queue.put(update) สำหรับการจัดการ Asynchronous ที่ดีขึ้น
        update = Update.de_json(json_data, application.bot)
        await application.process_update(update)
        logger.info(f"Webhook processed successfully. Time: {time.time() - start_time_webhook:.4f}s")
        return jsonify({"status": "ok"})
    except Exception as e:
        logger.error(f"Error processing update in webhook: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 400


# --- ส่วนสำหรับรัน Flask App ---
if __name__ == '__main__':
    logger.info("Starting Flask app...")
    if os.getenv("FLASK_ENV") == "development":
        logger.info("Running Flask in development mode (local testing).")
        # ควรใช้ app.run ใน development เท่านั้น
        # เพิ่ม use_reloader=False เพื่อป้องกันการรันสองครั้งเมื่อมีการเปลี่ยนแปลงโค้ด
        app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False) 
    else:
        logger.info("Running in production mode. Gunicorn will handle the app.")