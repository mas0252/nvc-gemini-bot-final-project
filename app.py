import os
import logging
import time
import asyncio
import re
from flask import Flask, request, jsonify
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, ContextTypes
from telegram.ext.filters import TEXT as TEXT_FILTER
import google.generativeai as genai
import pdfplumber
from supabase import create_client, Client
from dotenv import load_dotenv

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
def save_chat_history(chat_id: int, sender: str, message: str, username: str = None,):
    """บันทึกข้อความลงใน Supabase chat_history table."""
    if not supabase:
        logger.debug("Supabase not initialized, skipping chat history save.")
        return
    try:
        data = {
            "chat_id": str(chat_id),
            "sender": sender,
            "message": message,
            "username": username
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

# --- Image Mapping สำหรับตอบด้วยรูปภาพ ---
# บอทจะมองหาคีย์เวิร์ดในคำถามของผู้ใช้
# หากพบ จะส่งรูปภาพที่ระบุ
IMAGE_LOOKUP = {
    # คีย์เวิร์ด (Key): ('ชื่อไฟล์ในโฟลเดอร์ images/', 'คำบรรยายรูปภาพ')
    "แผนที่": ('https://squqrsinrzpqbvbnirzw.supabase.co/storage/v1/object/public/nvc_images/map.png', 'แผนที่และผังอาคารวิทยาลัย'),
    "ผัง": ('https://squqrsinrzpqbvbnirzw.supabase.co/storage/v1/object/public/nvc_images/pang.png', 'นี่คือผังอาคารวิทยาลัยนครศรีธรรมราชครับ'),
    "อาคาร 1": ('https://squqrsinrzpqbvbnirzw.supabase.co/storage/v1/object/public/nvc_images/1.png', 'นี่คือภาพอาคาร 1 ครับ'),
    "อาคาร 2": ('https://squqrsinrzpqbvbnirzw.supabase.co/storage/v1/object/public/nvc_images/2.png', 'นี่คือภาพอาคาร 2 ครับ'),
    "อาคาร 3": ('https://squqrsinrzpqbvbnirzw.supabase.co/storage/v1/object/public/nvc_images/3.png', 'นี่คือภาพอาคาร 3 ครับ'),
    "อาคาร 4": ('https://squqrsinrzpqbvbnirzw.supabase.co/storage/v1/object/public/nvc_images/4.png', 'นี่คือภาพอาคาร 4 ครับ'),
    "อาคาร 5": ('https://squqrsinrzpqbvbnirzw.supabase.co/storage/v1/object/public/nvc_images/5.png', 'นี่คือภาพอาคาร 5 ครับ'),
    "อาคาร 6": ('https://squqrsinrzpqbvbnirzw.supabase.co/storage/v1/object/public/nvc_images/6.png', 'นี่คือภาพอาคาร 6 ครับ'),
    "อาคาร 7": ('https://squqrsinrzpqbvbnirzw.supabase.co/storage/v1/object/public/nvc_images/7.png', 'นี่คือภาพอาคาร 7 ครับ'),
    "อาคาร 8": ('https://squqrsinrzpqbvbnirzw.supabase.co/storage/v1/object/public/nvc_images/8.png', 'นี่คือภาพอาคาร 8 ครับ'),
    # เพิ่มคีย์เวิร์ดและรูปภาพอื่น ๆ ตามต้องการ
}
IMAGE_PROMPT_INSTRUCTIONS = """
        ### 🖼️ คำสั่งพิเศษเกี่ยวกับรูปภาพ
        นอกจากการตอบคำถามแล้ว คุณมีความสามารถในการแนะนำรูปภาพประกอบ
        หากคำตอบของคุณเกี่ยวข้องกับหัวข้อใดหัวข้อหนึ่งต่อไปนี้ ให้คุณ **เพิ่มแท็ก** พิเศษต่อท้ายคำตอบของคุณ:

        1.  ถ้าคำตอบเกี่ยวกับ "แผนที่", "ที่ตั้ง", หรือ "การเดินทาง" ไปยังวิทยาลัย:
            ให้เพิ่มแท็ก: `[IMAGE:map]`
        2.  ถ้าคำตอบเกี่ยวกับ "อาคาร 1", "ตึก 1", หรือ "อาคารอำนวยการ":
            ให้เพิ่มแท็ก: `[IMAGE:building_1]`
        3.  ถ้าคำตอบเกี่ยวกับ "อาคาร 2", "ตึก 2", หรือ "แผนกช่าง":
            ให้เพิ่มแท็ก: `[IMAGE:building_2]`
        4.  ถ้าคำตอบเกี่ยวกับ "อาคาร 3", "ตึก 3", หรือ "แผนกพณิชยการ":
            ให้เพิ่มแท็ก: `[IMAGE:building_3]`

        ตัวอย่างการตอบ:
        ผู้ใช้: "ตึก 1 อยู่ไหน"
        คำตอบ: "อาคาร 1 คืออาคารอำนวยการครับ ใช้สำหรับติดต่อธุรการและงานทะเบียน [IMAGE:building_1]"

        (หากคำถามไม่เกี่ยวกับรูปภาพเหล่านี้ ก็ไม่ต้องเพิ่มแท็กใดๆ)
        """

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

        
        


# --- Handler สำหรับข้อความทั่วไป (Core Logic พร้อม Gemini API, Supabase และ Image AI Tagging) ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """จัดการข้อความทั่วไป, เรียก Gemini (ให้ Gemini ตัดสินใจส่งรูป), และส่งรูปภาพเสริม (ถ้ามี)"""
    start_time = time.time()

    # 1. ตรวจสอบข้อความเบื้องต้น
    if not update.message or not update.message.text:
        logger.info("Received an update without a text message, ignoring.")
        return

    user_message = update.message.text
    chat_id = update.message.chat_id

    # 2. ดึงข้อมูลผู้ใช้ (สำหรับ logging และ Supabase)
    user = update.message.from_user
    username = user.username if user.username else user.first_name 
    user_name_log = user.first_name if user.first_name else "ผู้ใช้งาน"

    logger.info(f"Received message from {user_name_log} ({chat_id}): \"{user_message}\" at {time.strftime('%H:%M:%S', time.localtime(start_time))}")
    
    
    

    try:
        # 3. 🟢 บันทึกข้อความผู้ใช้ทันที
        save_chat_history(chat_id, 'user', user_message, username) 

        # 4. 🟡 ดึงประวัติการแชท (บริบท)
        chat_history_text = get_chat_history(chat_id, limit=8)

        # 5. ดึงบริบทจากไฟล์ (หรือ PDF)
        pdf_text = PDF_CONTEXT_TEXT 

        # 6. สร้าง Prompt ที่สมบูรณ์สำหรับ Gemini (เพิ่มคำสั่งรูปภาพ)
        gemini_prompt = f"""
        คุณคือแชทบอทผู้เชี่ยวชาญด้านข้อมูลของวิทยาลัยอาชีวศึกษานครศรีธรรมราช (NVC Assistant)
        ***
        ### 🎯 ภารกิจและบุคลิกภาพ (Persona)
        1.  **น้ำเสียง (Tone):** ต้องสุภาพ, เป็นมิตร, และให้ความช่วยเหลืออย่างกระตือรือร้น
        2.  **การตอบ:** ตอบคำถามของผู้ใช้เกี่ยวกับวิทยาลัยฯ โดยยึดตาม **"ข้อมูลบริบทของวิทยาลัย"** ที่ให้มาเท่านั้น
        3.  **ความลื่นไหล:** เรียบเรียงใหม่ให้อ่านง่าย

        ### 📝 รูปแบบการจัดคำตอบ (Formatting)
        1.  **ใช้ Heading และรายการ:** ใช้ **ตัวหนา (`**`)** หรือรายการแบบย่อหน้า (`*`) เพื่อแบ่งข้อมูล
        2.  **เว้นวรรค:** เว้นบรรทัดเพื่อให้ข้อความไม่ติดกันเป็นพรืด

        

        ### 🚨 ข้อจำกัดความปลอดภัย
        1.  หากคำถามของผู้ใช้ **ไม่เกี่ยวข้อง** หรือ **ไม่พบคำตอบ** ในข้อมูลที่ให้มาอย่างชัดเจน:
            * ให้ตอบว่า: "ขออภัยครับ ผมไม่สามารถให้ข้อมูลในเรื่องนี้ได้ในขณะนี้..."
        2.  ห้ามเสริมเติมแต่งข้อมูลที่ไม่ปรากฏในบริบทเด็ดขาด

        ***
        ### 📘 ข้อมูลบริบทของวิทยาลัย (College Context)
        {pdf_text}

        ### 💬 ประวัติการสนทนา (Chat History)
        {chat_history_text}

        ### ❓ คำถามของผู้ใช้ (User's Question)
        {user_message}

        ***
        {IMAGE_PROMPT_INSTRUCTIONS} # ⭐️ (NEW) เพิ่มคำสั่งรูปภาพที่นี่

        ### คำตอบ (Response)
        """

        # 7. 🧠 เรียก Gemini API
        gemini_response = gemini_model.generate_content(gemini_prompt)

        response_text = ""
        if gemini_response and gemini_response.text:
            response_text = gemini_response.text
        # ... (โค้ดตรวจสอบ response.parts ... )

        if not response_text.strip():
            response_text = "ขออภัยครับ เกิดข้อผิดพลาดในการประมวลผลคำถามของคุณ..."
            logger.warning(f"Gemini returned empty response for chat_id {chat_id}.")

        # 8. 🖼️ (NEW) ตรวจสอบและแยกแท็กรูปภาพออกจากคำตอบ
        image_tag = None
        cleaned_response_text = response_text # ข้อความที่จะส่งให้ผู้ใช้

        # ใช้ Regular Expression (re) เพื่อค้นหาแท็ก [IMAGE:...]
        match = re.search(r'\[IMAGE:([\w_]+)\]', response_text) 

        if match:
            image_tag = match.group(1) # ดึง 'map' หรือ 'building_1'
            # ลบแท็กออกจากข้อความที่จะส่งให้ผู้ใช้
            cleaned_response_text = response_text.replace(match.group(0), "").strip()

        # 9. 📤 ส่งคำตอบที่เป็นข้อความ (Text Response)
        await context.bot.send_message(chat_id=chat_id, text=cleaned_response_text)

        # 10. 🖼️ (NEW) ส่งรูปภาพ (ถ้า Gemini สั่ง)
        final_bot_response = cleaned_response_text # ข้อความที่จะบันทึกลง Log

        if image_tag and image_tag in IMAGE_LOOKUP:
            # ⭐️ ถ้าพบแท็กที่ตรงกันในคลังรูปภาพของเรา
            image_url, caption = IMAGE_LOOKUP[image_tag]
            try:
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=image_url, # ส่งด้วย URL จาก Supabase
                    caption=f"นี่คือภาพประกอบครับ: {caption}"
                )
                logger.info(f"Sent supplementary image: {image_url} to {chat_id}")
                final_bot_response = f"{cleaned_response_text}\n(ส่งภาพ: {caption})"
            except Exception as e:
                logger.error(f"Error sending supplementary photo from URL {image_url}: {e}")

        # 11. 🟢 บันทึกคำตอบของบอท (Log Response)
        save_chat_history(chat_id, 'bot', final_bot_response, username)

        end_time = time.time()
        logger.info(f"Responded to {user_name_log} ({chat_id}). Time: {time.time() - start_time:.4f}s")

    # ... (โค้ด except Exception ... )

    except genai.types.BlockedPromptException as e:
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
    start_time_webhook = time.time()
    if request.method == "POST":
        json_data = request.get_json(force=True)
        logger.info(f"Received webhook data: {json_data}")
        
        try:
            # แก้ไข: ต้องเรียกใช้ initialize() และ shutdown() ในโหมด Webhook
            await application.initialize() # <--- ทำให้ Application พร้อมทำงาน
            update = Update.de_json(json_data, application.bot)
            await application.process_update(update)
            await application.shutdown() # <--- ปิดการทำงาน (คืนทรัพยากร)
            
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
    if os.getenv("FLASK_ENV") == "development":
        logger.info("Running Flask in development mode (local testing).")
        # ควรใช้ app.run ใน development เท่านั้น
        # เพิ่ม use_reloader=False เพื่อป้องกันการรันสองครั้งเมื่อมีการเปลี่ยนแปลงโค้ด
        app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False) 
    else:
        logger.info("Running in production mode. Gunicorn will handle the app.")