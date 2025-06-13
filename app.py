import os
import logging
import time
from flask import Flask, request, jsonify
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler
from telegram.ext.filters import TEXT as TEXT_FILTER
import google.generativeai as genai

# --- การตั้งค่า Logging ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# --- การดึง Bot Token และ Gemini API Key จาก Environment Variables ---
# **สำคัญ:** เราจะดึงค่าเหล่านี้จาก Environment Variables บน Render.com เพื่อความปลอดภัย
# สำหรับการทดสอบบนเครื่องตัวเอง คุณสามารถตั้งค่าได้ชั่วคราว
# หรือจะใช้ os.environ.get("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN_HERE") ไปก่อนได้
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
gemini_model = genai.GenerativeModel('gemini 2.0 Flash')

# --- เตรียมข้อมูล Context ของวิทยาลัยฯ สำหรับ Gemini ---
# **สำคัญ:** คุณสามารถขยายข้อมูลนี้ให้ละเอียดและครอบคลุมมากขึ้นได้เลย
# ยิ่งข้อมูลละเอียดและจัดระเบียบดีเท่าไหร่ Gemini ก็จะยิ่งตอบได้ตรงประเด็นมากขึ้น
COLLEGE_CONTEXT = """
วิทยาลัยอาชีวศึกษานครศรีธรรมราช ตั้งอยู่ที่ 123 ถนนราชดำเนิน ตำบลคลัง อำเภอเมืองนครศรีธรรมราช นครศรีธรรมราช 80000
เว็บไซต์หลัก: www.nvc.ac.th
Facebook Page: วิทยาลัยอาชีวศึกษานครศรีธรรมราช Official (หรือชื่อที่คุณจะระบุ)
เบอร์โทรศัพท์ติดต่อ: 075-XXXXXXX (เปลี่ยนเป็นเบอร์จริง)

วิทยาลัยฯ เปิดสอน 2 หลักสูตรหลักคือ:
1.  **หลักสูตรประกาศนียบัตรวิชาชีพ (ปวช.)**
    * สาขาที่เปิดสอน: การบัญชี, การตลาด, คอมพิวเตอร์ธุรกิจ, คอมพิวเตอร์กราฟิก, อาหารและโภชนาการ, แฟชั่นและสิ่งทอ, คหกรรม, ช่างเชื่อม
2.  **หลักสูตรประกาศนียบัตรวิชาชีพชั้นสูง (ปวส.)**
    * สาขาที่เปิดสอน: การบัญชี, การตลาด, คอมพิวเตอร์ธุรกิจ, เทคโนโลยีสารสนเทศ, การจัดการสำนักงาน, การโรงแรม, การท่องเที่ยว

**การรับสมัครนักศึกษาใหม่:**
* เปิดรับสมัครประมาณช่วงเดือน มกราคม - มีนาคม ของทุกปี
* โปรดติดตามประกาศและรายละเอียดเงื่อนไขการรับสมัครที่เว็บไซต์ www.nvc.ac.th หรือ Facebook Page ของวิทยาลัยฯ
* มีคุณสมบัติเบื้องต้น เช่น จบ ม.3 สำหรับ ปวช. และ จบ ปวช. หรือ ม.6 สำหรับ ปวส.

**ข้อมูลเพิ่มเติม:**
* วิทยาลัยฯ มีห้องสมุด, ศูนย์คอมพิวเตอร์, โรงอาหาร, สนามกีฬา
* มีกิจกรรมชมรมหลากหลาย เช่น ชมรมดนตรี, ชมรมกีฬา, ชมรมอาสาพัฒนา
* ภารกิจหลักคือการผลิตและพัฒนากำลังคนด้านอาชีวศึกษา
"""

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
            # เราจะรวมข้อมูล Context ของวิทยาลัยฯ และคำถามของผู้ใช้เข้าด้วยกัน
            gemini_prompt = f"""
            คุณคือผู้ช่วยให้ข้อมูลเกี่ยวกับวิทยาลัยอาชีวศึกษานครศรีธรรมราช
            โปรดตอบคำถามต่อไปนี้โดยอ้างอิงจากข้อมูลที่ให้มาเท่านั้น หากคำถามไม่เกี่ยวกับข้อมูลที่ให้มา หรือคุณไม่พบคำตอบในข้อมูลที่ให้มา
            ให้ตอบว่า "ขออภัยครับ ผมไม่สามารถให้ข้อมูลในเรื่องนี้ได้ในขณะนี้"

            ข้อมูลวิทยาลัยอาชีวศึกษานครศรีธรรมราช:
            {COLLEGE_CONTEXT}

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

