import os
import logging
import time
import asyncio
import re
# Import เครื่องมือสำหรับสร้างเว็บเซิร์ฟเวอร์และจัดการ JSON
from flask import Flask, request, jsonify
# Import เครื่องมือสำหรับ Telegram Bot (รับข้อความ, ส่งรูป, สร้างปุ่ม)
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, InputMediaPhoto
from telegram.ext import Application, MessageHandler, CommandHandler, ContextTypes
from telegram.ext.filters import TEXT as TEXT_FILTER
# Import เครื่องมือ AI และ PDF
import google.generativeai as genai
import pdfplumber
# Import เครื่องมือฐานข้อมูล
from supabase import create_client, Client
# Import เครื่องมือโหลดค่า Config ในเครื่อง (ไม่ใช้บน Server จริง)
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone

# 1. โหลดค่าความลับจากไฟล์ .env (ทำงานเฉพาะตอนรันบนคอมพิวเตอร์)
load_dotenv() 

# 2. ตั้งค่าระบบ Logging (เพื่อดูสถานะการทำงานและ Error ใน Terminal)
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# 3. สร้างแอป Flask (เว็บเซิร์ฟเวอร์)
app = Flask(__name__)

# 4. ดึงค่า Config จาก Environment Variables (กุญแจลับต่างๆ)
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# --- ตรวจสอบความถูกต้องของกุญแจลับ (Safety Check) ---
if not BOT_TOKEN:
    logger.critical("!!! CRITICAL ERROR: BOT_TOKEN not set. Exiting. !!!")
    exit(1) # หยุดทำงานทันทีถ้าไม่มี Token
if not GEMINI_API_KEY:
    logger.critical("!!! CRITICAL ERROR: GEMINI_API_KEY not set. Exiting. !!!")
    exit(1)

# 5. ตั้งค่า Gemini AI (สมองของบอท)
try:
    genai.configure(api_key=GEMINI_API_KEY)
    gemini_model = genai.GenerativeModel('gemini-2.0-flash-live') # ใช้รุ่น Flash เพื่อความเร็ว
    logger.info("Gemini API configured successfully.")
except Exception as e:
    logger.critical(f"!!! CRITICAL ERROR: Gemini Config Failed: {e}. Exiting. !!!")
    exit(1)

# 6. เชื่อมต่อฐานข้อมูล Supabase (ความจำระยะยาว)
supabase: Client | None = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        logger.info("Supabase client created successfully.")
    except Exception as e:
        logger.error(f"Error connecting to Supabase: {e}. Supabase features disabled.")
        supabase = None
else:
    logger.warning("Supabase credentials not found. Features disabled.")

# --- ส่วนฟังก์ชันช่วยเหลือ (Helper Functions) ---

def save_chat_history(chat_id: int, sender: str, message: str, username: str = None):
    """
    บันทึกข้อความลงในตาราง chat_history ของ Supabase
    sender: 'user' (ผู้ใช้) หรือ 'bot' (บอทตอบ)
    """
    if not supabase: return
    try:
        data = {
            "chat_id": str(chat_id),
            "sender": sender,
            "message": message,
            "username": username
        }
        supabase.table('chat_history').insert(data).execute()
    except Exception as e:
        logger.error(f"Error saving chat history: {e}")

def get_chat_history(chat_id: int, limit: int = 6) -> str:
    """
    ดึงประวัติการแชทล่าสุด 6 ข้อความ (3 คู่สนทนา) เพื่อส่งให้ Gemini
    ช่วยให้ AI จำบริบทการคุยต่อเนื่องได้
    """
    if not supabase: return ""
    try:
        response = supabase.table('chat_history').select('sender, message') \
            .eq('chat_id', str(chat_id)) \
            .order('created_at', desc=True) \
            .limit(limit) \
            .execute()
        
        if not response.data: return ""

        # จัดรูปแบบข้อความย้อนหลัง (เรียงจากเก่า -> ใหม่)
        formatted_history = "\n--- Chat History (Oldest to Newest) ---\n"
        for item in reversed(response.data): 
            formatted_history += f"[{item['sender'].upper()}]: {item['message']}\n"
        formatted_history += "--- End Chat History ---\n"
        return formatted_history
    except Exception as e:
        logger.error(f"Error fetching history: {e}")
        return ""


def get_cached_response(message: str):
    """ค้นหาคำตอบใน Cache (ที่มีอายุไม่เกิน 24 ชั่วโมง)"""
    if not supabase: return None
    
    try:
        clean_message = message.strip()
        
        # 1. คำนวณเวลาเส้นตาย (ปัจจุบัน - 24 ชั่วโมง)
        # คุณสามารถเปลี่ยน hours=24 เป็น minutes=30 ได้ตามต้องการ
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=24) 
        
        # 2. แปลงเวลาเป็น format ที่ Supabase เข้าใจ (ISO 8601)
        cutoff_str = cutoff_time.isoformat()

        response = supabase.table('response_cache') \
            .select('bot_response') \
            .eq('user_message', clean_message) \
            .gt('created_at', cutoff_str) \
            .limit(1) \
            .execute()
            # .gt() ย่อมาจาก Greater Than (มากกว่า/ใหม่กว่า)
            
        if response.data:
            logger.info(f"Cache HIT (Fresh) for: {clean_message}")
            return response.data[0]['bot_response']
        else:
            logger.info(f"Cache MISS (Expired or Not Found) for: {clean_message}")
            return None # ถ้าไม่เจอ หรือเจอแต่เก่าเกินไป จะส่งกลับเป็น None (ให้ Gemini คิดใหม่)
            
    except Exception as e:
        logger.error(f"Error checking cache: {e}")
        return None
    

def save_to_cache(message: str, response: str):
    """
    ระบบ Cache: บันทึกคำถามใหม่และคำตอบลงฐานข้อมูล
    เพื่อใช้ตอบคนอื่นในอนาคต
    """
    if not supabase: return
    try:
        clean_message = message.strip()
        # ไม่บันทึกถ้าข้อความสั้นเกินไป หรือยาวเกินไป
        if len(clean_message) < 2 or len(clean_message) > 200: return

        data = {"user_message": clean_message, "bot_response": response}
        supabase.table('response_cache').insert(data).execute()
        logger.info(f"Saved to cache: {clean_message}")
    except Exception as e:
        logger.error(f"Error saving to cache: {e}")

def read_txt_context(file_path):
    """
    อ่านข้อมูลบริบทจากไฟล์ .txt (เช่น ข้อมูลวิทยาลัย)
    """
    if not os.path.exists(file_path):
        logger.error(f"Context file not found: {file_path}")
        return "ไม่พบข้อมูลบริบท"
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        logger.error(f"Error reading context file: {e}")
        return "เกิดข้อผิดพลาดในการอ่านข้อมูลบริบท"

# --- ข้อมูลและคำสั่ง (Configuration Data) ---

# คลังรูปภาพ: เชื่อมโยง 'แท็ก' (เช่น building_1) กับ 'URL รูปภาพ'
IMAGE_LOOKUP = {
    'map': ('https://squqrsinrzpqbvbnirzw.supabase.co/storage/v1/object/public/nvc_images/map.png', 'แผนที่วิทยาลัย'),
    'pang': ('https://squqrsinrzpqbvbnirzw.supabase.co/storage/v1/object/public/nvc_images/pang.png', 'ผังอาคาร'),
    'pp': ('https://squqrsinrzpqbvbnirzw.supabase.co/storage/v1/object/public/nvc_images/pp.jpg', 'การผ่อนผันทหาร'),
    'QU': ('https://squqrsinrzpqbvbnirzw.supabase.co/storage/v1/object/public/nvc_images/QU.jpg', 'ประกาศรับสมัคร ป.ตรี'),


    'DBT': ('https://squqrsinrzpqbvbnirzw.supabase.co/storage/v1/object/public/nvc_images/department/DBT.png', 'บุคลากรแผนกวิชาเทคโนโลยีธุรกิจดิจิทัล'),
    'DeoGl': ('https://squqrsinrzpqbvbnirzw.supabase.co/storage/v1/object/public/nvc_images/department/DeoGl.png', 'บุคลากรแผนกวิชาสามัญ'),
    'DeoF': ('https://squqrsinrzpqbvbnirzw.supabase.co/storage/v1/object/public/nvc_images/department/DeoF.png', 'บุคลากรแผนกอาหารและโภชนาการ'),
    'DeoHEc': ('https://squqrsinrzpqbvbnirzw.supabase.co/storage/v1/object/public/nvc_images/department/DeoHEc.png', 'บุคลากรแผนกวิชาคหกรรมศาสตร์'),
    'DeoFaAT': ('https://squqrsinrzpqbvbnirzw.supabase.co/storage/v1/object/public/nvc_images/department/DeoFaAT.png', 'บุคลากรแผนกวิชาเทคโนโลยีแฟชั่นและเครื่องแต่งกาย'),
    'Ac': ('https://squqrsinrzpqbvbnirzw.supabase.co/storage/v1/object/public/nvc_images/department/Ac.png', 'บุคลากรแผนกวิชาการบัญชี'),
    'MkD': ('https://squqrsinrzpqbvbnirzw.supabase.co/storage/v1/object/public/nvc_images/department/MkD.png', 'บุคลากรแผนกวิชาการตลาด'),
    'Desom': ('https://squqrsinrzpqbvbnirzw.supabase.co/storage/v1/object/public/nvc_images/department/Desom.png', 'บุคลากรแผนกวิชาการจัดการสำนักงานดิจิทัล'),
    'DeoLaSCM': ('https://squqrsinrzpqbvbnirzw.supabase.co/storage/v1/object/public/nvc_images/department/DeoLaSCM.png', 'บุคลากรแผนกวิชาการจัดการธุรกิจ/แผนกวิชาการจัดการโลจิสติกส์และซัพพลายเซน'),
    'HDe': ('https://squqrsinrzpqbvbnirzw.supabase.co/storage/v1/object/public/nvc_images/department/HDe.png', 'บุคลากรแผนกวิชาการโรงแรม'),
    'DeoTBM': ('https://squqrsinrzpqbvbnirzw.supabase.co/storage/v1/object/public/nvc_images/department/DeoTBM.png', 'บุคลากรแผนกวิชาการจัดการธุรกิจท่องเที่ยว'),
    'DeoTBMa': ('https://squqrsinrzpqbvbnirzw.supabase.co/storage/v1/object/public/nvc_images/department/DeoTBMa.png', 'บุคลากรแผนกวิชาภาษาต่างประเทศ'),
    'ITD': ('https://squqrsinrzpqbvbnirzw.supabase.co/storage/v1/object/public/nvc_images/department/ITD.png', 'บุคลากรแผนกวิชาเทคโนโลยีสารสนเทศ'),
    'DeoLI': ('https://squqrsinrzpqbvbnirzw.supabase.co/storage/v1/object/public/nvc_images/department/DeoLI.png', 'บุคลากรแผนกวิชาอุตสาหกรรมโลจิสติกส์'),

    # ตัวอย่างการส่งหลายรูป (Album)
    'quota_round_1': (
        [
            'https://squqrsinrzpqbvbnirzw.supabase.co/storage/v1/object/public/nvc_images/QOU1.jpg',
            'https://squqrsinrzpqbvbnirzw.supabase.co/storage/v1/object/public/nvc_images/QOU2.jpg'
        ], 
        'รายละเอียดโควตารอบ 1'
    ),
    'quota_round_2': (
        [
            'https://squqrsinrzpqbvbnirzw.supabase.co/storage/v1/object/public/nvc_images/QOU1.jpg',
            'https://squqrsinrzpqbvbnirzw.supabase.co/storage/v1/object/public/nvc_images/QU.jpg'
        ], 
        '📝 การรับสมัคร โควตากรณีพิเศษ (รอบที่ 1) ปีการศึกษา 2569'
    ),

    # ... (เพิ่มรายการอื่นๆ ที่นี่)
    'building_1': ('https://squqrsinrzpqbvbnirzw.supabase.co/storage/v1/object/public/nvc_images/1.png', 'นี่คือภาพอาคาร 1 ครับ'),
    'building_2': ('https://squqrsinrzpqbvbnirzw.supabase.co/storage/v1/object/public/nvc_images/2.png', 'นี่คือภาพอาคาร 2 ครับ'),
    'building_3': ('https://squqrsinrzpqbvbnirzw.supabase.co/storage/v1/object/public/nvc_images/3.png', 'นี่คือภาพอาคาร 3 ครับ'),
    'building_4': ('https://squqrsinrzpqbvbnirzw.supabase.co/storage/v1/object/public/nvc_images/4.png', 'นี่คือภาพอาคาร 4 ครับ'),
    'building_5': ('https://squqrsinrzpqbvbnirzw.supabase.co/storage/v1/object/public/nvc_images/5.png', 'นี่คือภาพอาคาร 5 ครับ'),
    'building_6': ('https://squqrsinrzpqbvbnirzw.supabase.co/storage/v1/object/public/nvc_images/6.png', 'นี่คือภาพอาคาร 6 ครับ'),
    'building_7': ('https://squqrsinrzpqbvbnirzw.supabase.co/storage/v1/object/public/nvc_images/7.png', 'นี่คือภาพอาคาร 7 ครับ'),
    'building_8': ('https://squqrsinrzpqbvbnirzw.supabase.co/storage/v1/object/public/nvc_images/8.png', 'นี่คือภาพอาคาร 8 ครับ'),
    'building_6_632': ('https://squqrsinrzpqbvbnirzw.supabase.co/storage/v1/object/public/nvc_images/IMG_20251117_132117.jpg', 'นี่คือห้อง 632 ครับ'),
    # ...
}

# คำสั่งพิเศษ (Prompt) เพื่อสอนให้ Gemini รู้จักแท็กรูปภาพ
IMAGE_PROMPT_INSTRUCTIONS = """
    ### 🖼️ คำสั่งพิเศษเกี่ยวกับรูปภาพ
    นอกจากการตอบคำถามแล้ว คุณสามารถแนะนำรูปภาพประกอบได้
    หากคำถามของผู้ใช้หรือคำตอบของคุณเกี่ยวข้องกับรายการใดต่อไปนี้ ให้คุณ **เพิ่มแท็ก** พิเศษต่อท้ายคำตอบของคุณ:

    -   เกี่ยวกับ "📍 แผนที่วิทยาลัย", "ที่ตั้ง", หรือ "การเดินทาง" ไปยังวิทยาลัย: ให้เพิ่มแท็ก `[IMAGE:map]`
    -   เกี่ยวกับ "ผัง" หรือ "ผังอาคาร": ให้เพิ่มแท็ก `[IMAGE:pang]`
    -   เกี่ยวกับ "นักศึกษาใหม่ ระดับปริญญาตรี รอบโควตาทั่วไป ประจำปีการศึกษา 2569": ให้เพิ่มแท็ก `[IMAGE:QU]`
    -   เกี่ยวกับ "เปิดรับสมัครแล้ว โควตากรณีพิเศษ (รอบที่ 1) ปีการศึกษา 2569": ให้เพิ่มแท็ก `[IMAGE:quota_round_1]`
    -   เกี่ยวกับ "📝 การรับสมัคร": ให้เพิ่มแท็ก `[IMAGE:quota_round_2]`
    -   เกี่ยวกับ "การผ่อนผันเข้ารับราชการทหาร ประจำปีการศึกษา 2569": ให้เพิ่มแท็ก `[IMAGE:pp]`


    -   เกี่ยวกับ "ครูแผนกเทคโนโลยีธุรกิจดิจิทัล",หรือ "บุคลากรแผนกวิชาเทคโนโลยีธุรกิจดิจิทัล": ให้เพิ่มแท็ก `[IMAGE:DBT]`
    -   เกี่ยวกับ "ครูแผนกสามัญ",หรือ "บุคลากรแผนกวิชาสามัญ": ให้เพิ่มแท็ก `[IMAGE:DeoGl]`
    -   เกี่ยวกับ "ครูแผนกอาหารและโภชนาการ",หรือ "บุคลากรแผนกวิชาอาหารและโภชนาการ": ให้เพิ่มแท็ก `[IMAGE:DeoF]`
    -   เกี่ยวกับ "ครูแผนกวิชาคหกรรมศาสตร์",หรือ "บุคลากรแผนกวิชาคหกรรมศาสตร์": ให้เพิ่มแท็ก `[IMAGE:DeoHEc]`
    -   เกี่ยวกับ "ครูแผนกวิชาเทคโนโลยีแฟชั่นและเครื่องแต่งกาย",หรือ "บุคลากรแผนกวิชาเทคโนโลยีแฟชั่นและเครื่องแต่งกาย": ให้เพิ่มแท็ก `[IMAGE:DeoFaAT]`
    -   เกี่ยวกับ "ครูแผนกวิชาการบัญชี",หรือ "บุคลากรแผนกวิชาการบัญชี": ให้เพิ่มแท็ก `[IMAGE:Ac]`
    -   เกี่ยวกับ "ครูแผนกกวิชาการตลาด",หรือ "บุคลากรแผนกวิชาการตลาด": ให้เพิ่มแท็ก `[IMAGE:MkD]`
    -   เกี่ยวกับ "ครูแผนกวิชาการจัดการสำนักงานดิจิทัล",หรือ "บุคลากรแผนกวิชาการจัดการสำนักงานดิจิทัล": ให้เพิ่มแท็ก `[IMAGE:Desom]`
    -   เกี่ยวกับ "ครูแผนกวิชาการจัดการธุรกิจ",หรือ "บุคลากรแผนกวิชาการจัดการธุรกิจ",หรือ "บุคลากรแผนกวิชาการจัดการโลจิสติกส์และซัพพลายเซน": ให้เพิ่มแท็ก `[IMAGE:DeoLaSCM]`
    -   เกี่ยวกับ "ครูแผนกวิชาการโรงแรม",หรือ "บุคลากรแผนกวิชาการโรงแรม": ให้เพิ่มแท็ก `[IMAGE:HDe]`
    -   เกี่ยวกับ "ครูแผนกวิชาการจัดการธุรกิจท่องเที่ยว",หรือ "บุคลากรแผนกวิชาการจัดการธุรกิจท่องเที่ยว": ให้เพิ่มแท็ก `[IMAGE:DeoTBM]`
    -   เกี่ยวกับ "ครูแผนกวิชาภาษาต่างประเทศ",หรือ "บุคลากรแผนกวิชาภาษาต่างประเทศ": ให้เพิ่มแท็ก `[IMAGE:DeoTBMa]`
    -   เกี่ยวกับ "ครูแผนกวิชาเทคโนโลยีสารสนเทศ",หรือ "บุคลากรแผนกวิชาเทคโนโลยีสารสนเทศ": ให้เพิ่มแท็ก `[IMAGE:ITD]`
    -   เกี่ยวกับ "ครูแผนกวิชาอุตสาหกรรมโลจิสติกส์",หรือ "บุคลากรแผนกวิชาอุตสาหกรรมโลจิสติกส์": ให้เพิ่มแท็ก `[IMAGE:DeoLI]`






    -   เกี่ยวกับ "อาคาร 1": หรือ "อาคารอำนวยการ": ให้เพิ่มแท็ก `[IMAGE:building_1]`
    -   เกี่ยวกับ "อาคาร 2": ให้เพิ่มแท็ก `[IMAGE:building_2]`
    -   เกี่ยวกับ "อาคาร 3": ให้เพิ่มแท็ก `[IMAGE:building_3]`
    -   เกี่ยวกับ "อาคาร 4": ให้เพิ่มแท็ก `[IMAGE:building_4]`
    -   เกี่ยวกับ "อาคาร 5": ให้เพิ่มแท็ก `[IMAGE:building_5]`
    -   เกี่ยวกับ "อาคาร 6": ให้เพิ่มแท็ก `[IMAGE:building_6]`
    -   เกี่ยวกับ "อาคาร 7": ให้เพิ่มแท็ก `[IMAGE:building_7]`
    -   เกี่ยวกับ "อาคาร 8": ให้เพิ่มแท็ก `[IMAGE:building_8]`
    -   เกี่ยวกับ "ห้อง 632": ให้เพิ่มแท็ก `[IMAGE:building_6_632]`
    """

# --- ส่วนจัดการการตอบโต้ (Telegram Handlers) ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    ทำงานเมื่อผู้ใช้กด /start
    แสดงข้อความต้อนรับ + ปุ่มเมนูลัด (Quick Reply Keyboard)
    """
    user = update.message.from_user
    user_name = user.first_name if user.first_name else "ผู้ใช้งาน"
    chat_id = update.message.chat_id
    username = user.username if user.username else user.first_name 

    # สร้างปุ่มเมนูลัดด้านล่างจอ
    keyboard = [
        [KeyboardButton("📚 หลักสูตรที่เปิดสอน"), KeyboardButton("📖 แผนกวิชาทั้งหมด")], 
        [KeyboardButton("📝 การรับสมัคร"), KeyboardButton("📍 แผนที่วิทยาลัย")],
        [KeyboardButton("☎️ ติดต่อเรา")] 
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)

    response_text = f"สวัสดีครับคุณ {user_name}! ผมคือบอทผู้ช่วยข้อมูลวิทยาลัยอาชีวศึกษานครศรีธรรมราชครับ ยินดีให้บริการครับ"

    try:
        # ส่งข้อความพร้อมปุ่ม
        await context.bot.send_message(chat_id=chat_id, text=response_text, reply_markup=reply_markup)
        # บันทึกประวัติว่าเริ่มใช้งาน
        save_chat_history(chat_id, 'user', '/start', username)
        save_chat_history(chat_id, 'bot', response_text, username)
    except Exception as e:
        logger.error(f"Error in start_command: {e}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    ฟังก์ชันหลักสำหรับประมวลผลข้อความของผู้ใช้
    ลำดับการทำงาน: เช็ค Cache -> ถ้าไม่มี ถาม Gemini -> เช็คแท็กรูป -> ส่งข้อความ -> ส่งรูป -> บันทึก Cache/History
    """
    start_time = time.time()
    
    if not update.message or not update.message.text:
        return

    user_message = update.message.text
    chat_id = update.message.chat_id
    user = update.message.from_user
    username = user.username if user.username else user.first_name 
    
    logger.info(f"Message from {username} ({chat_id}): {user_message}")

    try:
        # 1. เช็ค Cache ก่อน (เพื่อความเร็ว)
        cached_answer = get_cached_response(user_message)
        response_text = ""
        is_cached = False

        if cached_answer:
            response_text = cached_answer
            is_cached = True
            logger.info("✅ Used Cache")
        else:
            # 2. ถ้าไม่เจอใน Cache ให้ถาม Gemini
            save_chat_history(chat_id, 'user', user_message, username) # บันทึกคำถาม
            chat_history_text = get_chat_history(chat_id, limit=8) # ดึงประวัติเก่า
            pdf_text = read_txt_context("dataNVC.txt") # โหลดข้อมูลวิทยาลัย (Lazy Load)

            # สร้างคำสั่ง (Prompt) ส่งให้ Gemini
            gemini_prompt = f"""
            คุณคือแชทบอทผู้เชี่ยวชาญด้านข้อมูลของวิทยาลัยอาชีวศึกษานครศรีธรรมราช (NVC Assistant)
            ***
            ### 🎯 Persona
            สุภาพ, เป็นมิตร, ช่วยเหลือ, ตอบเป็นธรรมชาติ

            ### 📝 Formatting
            ใช้ตัวหนา, รายการ, เว้นวรรคให้อ่านง่าย

            ### 🚨 Safety
            ตอบเฉพาะข้อมูลในบริบท ถ้าไม่มีให้แนะนำติดต่อวิทยาลัย

            ***
            ### 📘 Context (ข้อมูลวิทยาลัย)
            {pdf_text}

            ### 💬 History (ประวัติการคุย)
            {chat_history_text}

            ### ❓ Question (คำถามล่าสุด)
            {user_message}

            ***
            {IMAGE_PROMPT_INSTRUCTIONS}

            ### Answer
            """
            
            # ส่งไปถาม Gemini
            gemini_response = gemini_model.generate_content(gemini_prompt)
            
            if gemini_response and gemini_response.text:
                response_text = gemini_response.text.strip()
            
            if not response_text:
                response_text = "ขออภัยครับ ระบบไม่สามารถประมวลผลคำตอบได้ในขณะนี้"

        # 3. ตรวจสอบแท็กรูปภาพจากคำตอบ (เช่น [IMAGE:map])
        image_tag = None
        cleaned_response = response_text
        match = re.search(r'\[IMAGE:([\w_]+)\]', response_text)
        if match:
            image_tag = match.group(1) # เก็บชื่อแท็กไว้
            cleaned_response = response_text.replace(match.group(0), "").strip() # ลบแท็กออกจากข้อความที่จะส่งจริง

        # 4. ส่งคำตอบที่เป็นข้อความกลับไป
        if cleaned_response:
            await context.bot.send_message(chat_id=chat_id, text=cleaned_response)

        # 5. ส่งรูปภาพตามไป (ถ้ามีแท็ก)
        final_log_response = cleaned_response
        if image_tag and image_tag in IMAGE_LOOKUP:
            image_data, caption = IMAGE_LOOKUP[image_tag]
            try:
                if isinstance(image_data, list): # กรณีเป็นอัลบั้มหลายรูป
                    media = [InputMediaPhoto(url, caption=caption if i==0 else "") for i, url in enumerate(image_data)]
                    await context.bot.send_media_group(chat_id=chat_id, media=media)
                else: # กรณีเป็นรูปเดียว
                    await context.bot.send_photo(chat_id=chat_id, photo=image_data, caption=caption)
                
                final_log_response += f"\n(Sent Image: {image_tag})" # บันทึกลง Log ว่าส่งรูปแล้ว
            except Exception as e:
                logger.error(f"Error sending image {image_tag}: {e}")

        # 6. บันทึกประวัติและ Cache (เฉพาะกรณีไม่ได้ดึงมาจาก Cache)
        if not is_cached:
            save_chat_history(chat_id, 'bot', final_log_response, username)
            # บันทึก Cache เฉพาะคำตอบที่มีคุณภาพ (ยาวพอสมควร)
            if cleaned_response and len(cleaned_response) > 5:
                # บันทึก *full* response (รวม tag) ลง cache เพื่อให้ครั้งหน้าแสดงรูปได้ด้วย
                save_to_cache(user_message, response_text)

        logger.info(f"Processed in {time.time() - start_time:.4f}s")

    except Exception as e:
        logger.error(f"Unhandled error: {e}", exc_info=True)
        await context.bot.send_message(chat_id=chat_id, text="เกิดข้อผิดพลาดทางเทคนิค กรุณาลองใหม่อีกครั้งครับ")

# --- ตั้งค่า Application ของ Telegram ---
# เพิ่ม Timeout เพื่อป้องกัน Error เวลาเน็ตช้า
application = (
    Application.builder()
    .token(BOT_TOKEN)
    .read_timeout(30)
    .write_timeout(30)
    .build()
)

# ลงทะเบียน Handler
application.add_handler(CommandHandler("start", start_command))
application.add_handler(MessageHandler(TEXT_FILTER, handle_message))

# --- Webhook Route (จุดรับข้อมูลจาก Telegram) ---
@app.route(f'/{BOT_TOKEN}', methods=['POST'])
async def webhook():
    if request.method == "POST":
        try:
            # ต้องมีการ initialize และ shutdown สำหรับ v20+ ในโหมด Webhook
            await application.initialize()
            update = Update.de_json(request.get_json(force=True), application.bot)
            await application.process_update(update)
            await application.shutdown()
            return jsonify({"status": "ok"})
        except Exception as e:
            logger.error(f"Webhook error: {e}")
            return jsonify({"status": "error", "message": str(e)}), 400
    return jsonify({"status": "method not allowed"}), 405

# --- Main Entry Point ---
if __name__ == '__main__':
    if os.getenv("FLASK_ENV") == "development":
        # รันบนเครื่อง Local
        app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False)
    else:
        # รันบน Render (Production) จะใช้ Gunicorn
        logger.info("Running in production mode.")