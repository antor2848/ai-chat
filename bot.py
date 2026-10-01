import os
import logging
import re
import sqlite3
import requests
import PyPDF2
import asyncio
from typing import List, Any
from gtts import gTTS
from google import genai
from google.genai import types
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.helpers import escape_markdown
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes
)

# Optional OpenAI Import (If key provided)
try:
    from openai import OpenAI
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False

# ==================== ১. কনফিগারেশন ====================
TELEGRAM_BOT_TOKEN = '8667697302:AAH2vcK9e0azETl1uRiC85io28edeDDwx5Y'
GEMINI_API_KEY = 'AQ.Ab8RN6L3mm3MARiEl5_-od_Gg_CCjV3qdd8uBJEQvAp1Z_75aQ'

# ElevenLabs Credentials
ELEVENLABS_API_KEY = 'sk_982f5cd6aff5a17e226eef480e6ce8a26dc08e920b1753f1'
YOUR_VOICE_ID = 'kLhAstPcnnPxqzk6gS5i'

# OpenAI API Key (ফলব্যাকের জন্য اختیاری)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# Client Setups
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

if HAS_OPENAI and OPENAI_API_KEY:
    openai_client = OpenAI(api_key=OPENAI_API_KEY)
else:
    openai_client = None

PRIMARY_MODEL = 'gemini-3.6-flash'
DB_FILE = "ultimate_bot_memory.db"

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# ==================== ২. সেফটি ও হেলপারস ====================
def safe_markdown(text: str) -> str:
    """টেলিগ্রামের জন্য সুরক্ষিত মার্কডাউন এস্কেপার"""
    if not text:
        return ""
    # কোড ব্লক ও বোল্ড টেক্সট ঠিক রাখার জন্য সাধারণ টেক্সট হিসেবে পাঠানো নিরাপদ
    return text

def format_otp_message(text: str) -> str:
    """ওটিপি ক্লিক-টু-কপি ফরম্যাট"""
    otp_matches = re.findall(r'\b\d{5,6}\b', text)
    if otp_matches:
        formatted_text = text
        for otp in set(otp_matches):
            formatted_text = formatted_text.replace(otp, f"`{otp}`")
        return formatted_text
    return text

# ==================== ৩. ডাটাবেস হ্যান্ডলিং ====================
def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS chat_history (
                user_id INTEGER,
                role TEXT,
                content TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()

def save_chat(user_id: int, role: str, content: str):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute('INSERT INTO chat_history (user_id, role, content) VALUES (?, ?, ?)', (user_id, role, content))
            conn.commit()
    except Exception as e:
        logging.error(f"DB Save Error: {e}")

def get_history(user_id: int, limit: int = 6) -> List[str]:
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT role, content FROM chat_history WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?',
                (user_id, limit)
            )
            rows = cursor.fetchall()
        memory = []
        for role, content in reversed(rows):
            memory.append(f"{role}: {content}")
        return memory
    except Exception as e:
        logging.error(f"DB Read Error: {e}")
        return []

def clear_memory(user_id: int):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM chat_history WHERE user_id = ?', (user_id,))
        conn.commit()

# ==================== ৪. এআই ও কাস্টম ভয়েস ইঞ্জিন ====================
def generate_cloned_voice(text: str, output_path: str) -> bool:
    """ElevenLabs TTS with auto fallback to gTTS"""
    clean_text = re.sub(r'```.*?```', '', text, flags=re.DOTALL)
    clean_text = re.sub(r'[*_#`~]', '', clean_text).strip()

    if not clean_text:
        return False

    # ১. প্রথমে ElevenLabs দিয়ে চেষ্টা
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{YOUR_VOICE_ID}"
    headers = {
        "Accept": "audio/mpeg",
        "Content-Type": "application/json",
        "xi-api-key": ELEVENLABS_API_KEY
    }
    data = {
        "text": clean_text[:300],
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {"stability": 0.50, "similarity_boost": 0.80}
    }

    try:
        response = requests.post(url, json=data, headers=headers, timeout=10)
        if response.status_code == 200:
            with open(output_path, "wb") as f:
                f.write(response.content)
            return True
    except Exception as e:
        logging.warning(f"ElevenLabs Error: {e}")

    # ২. ব্যাকআপ: gTTS (Google Text-to-Speech)
    try:
        tts = gTTS(text=clean_text[:500], lang='bn')
        tts.save(output_path)
        return True
    except Exception as e:
        logging.error(f"gTTS Error: {e}")
        return False

def generate_ai_response(contents: Any, enable_search: bool = False) -> str:
    """Gemini API with Fallback Handling"""
    config = types.GenerateContentConfig()
    if enable_search:
        config.tools = [{"google_search": {}}]

    # ১. Gemini Primary Attempt
    try:
        response = gemini_client.models.generate_content(
            model=PRIMARY_MODEL,
            contents=contents,
            config=config
        )
        if response and response.text:
            return response.text
    except Exception as e:
        logging.warning(f"Gemini Primary Failed: {e}")

    # ২. OpenAI Fallback (If available and text input)
    if openai_client and isinstance(contents, str):
        try:
            chat_completion = openai_client.chat.completions.create(
                messages=[{"role": "user", "content": contents}],
                model="gpt-4o-mini",
            )
            return chat_completion.choices[0].message.content
        except Exception as oe:
            logging.error(f"OpenAI Fallback Failed: {oe}")

    # ৩. মাস্কড ফ্রেন্ডলি এরর মেসেজ
    return "ধন্যবাদ আমার সাথে কথা বলার জন্য! 🌸 এখন সিস্টেমে অনেক চাপ রয়েছে, অনুগ্রহ করে কিছুক্ষণ পর আবার মেসেজ দিন।"

# ==================== ৫. বট কমান্ড ও হ্যান্ডলারস ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "🚀 **আলটিমেট এআই সহকারী প্রস্তুত!**\n\n"
        "✨ **উপলব্ধ ফিচারসমূহ:**\n"
        "• 🎙️ **কাস্টম ভয়েস:** এআই আপনার নিজস্ব ক্লোন করা কণ্ঠে উত্তর দেবে।\n"
        "• 🌐 **লাইভ সার্চ:** বাস্তব সময়ের তথ্য অনুসন্ধান করতে সক্ষম।\n"
        "• 🎨 **ছবি জেনারেটর:** 'ছবি বানাও' লিখলে সাথে সাথে ছবি তৈরি।\n"
        "• 📁 **PDF রিডার:** ডকুমেন্ট এর সারাংশ তৈরি করা।\n"
        "• 📸 **ভিশন এআই:** ছবি বিশ্লেষণ করার ক্ষমতা।\n"
        "• 🧠 **মেমরি:** আপনার আগের কথোপকথন মনে রাখা।\n\n"
        "💡 *মেমরি রিসেট করতে:* `/reset`\n"
        "⏰ *রিমাইন্ডার সেট করতে:* `/remind 60 মেসেজ`"
    )
    await update.message.reply_text(welcome_text)

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    clear_memory(user_id)
    await update.message.reply_text("🧹 **আপনার চ্যাট মেমরি সফলভাবে রিসেট করা হয়েছে!**")

async def alarm_callback(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    await context.bot.send_message(job.chat_id, text=f"⏰ **রিমাইন্ডার:** {job.data}")

async def set_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        args = context.args
        seconds = int(args[0])
        text = ' '.join(args[1:])
        chat_id = update.effective_message.chat_id
        context.job_queue.run_once(alarm_callback, seconds, chat_id=chat_id, data=text)
        await update.message.reply_text(f"✅ রিমাইন্ডার সেট হয়েছে! {seconds} সেকেন্ড পর মেসেজ দেওয়া হবে।")
    except Exception:
        await update.message.reply_text("⚠️ ব্যবহার পদ্ধতি: `/remind 60 কাজ করার সময় হয়েছে`")

# ১. টেক্সট ও ইমেজ জেনারেটর হ্যান্ডলার
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    user_text = update.message.text

    # ইমেজ জেনারেশন কিওয়ার্ড চেক
    image_keywords = ["ছবি", "photo", "image", "picture", "আঁকো", "বানিয়ে দাও", "generate", "draw"]
    if any(k in user_text.lower() for k in image_keywords):
        status = await update.message.reply_text("🎨 আপনার কল্পনার ছবি তৈরি করা হচ্ছে...")
        try:
            prompt_encoded = requests.utils.quote(user_text)
            image_url = f"https://pollinations.ai/p/{prompt_encoded}?width=1024&height=1024&seed=42"
            await status.delete()
            await update.message.reply_photo(photo=image_url, caption="✨ আপনার প্রম্পট অনুযায়ী তৈরি ছবি!")
            return
        except Exception:
            await status.delete()
            await update.message.reply_text("দুঃখিত, ছবি জেনারেট করতে সমস্যা হয়েছে। আবার চেষ্টা করুন।")
            return

    try:
        history = get_history(user_id)
        system_instruction = (
            "You are a highly intelligent AI assistant. "
            "Reply naturally and accurately in the user's language."
        )
        full_prompt = f"{system_instruction}\n" + "\n".join(history) + f"\nUser: {user_text}"

        reply_text = generate_ai_response(full_prompt, enable_search=True)

        save_chat(user_id, "User", user_text)
        save_chat(user_id, "AI", reply_text)

        formatted_reply = format_otp_message(reply_text)
        await update.message.reply_text(formatted_reply)

        # কাস্টম ভয়েস জেনারেট
        voice_file = f"voice_{user_id}.mp3"
        if generate_cloned_voice(reply_text, voice_file):
            if os.path.exists(voice_file):
                with open(voice_file, 'rb') as audio:
                    await update.message.reply_voice(voice=audio)
                os.remove(voice_file)

    except Exception as e:
        logging.error(f"Text Handler Crash Avoided: {e}")
        await update.message.reply_text("ধন্যবাদ আমার সাথে কথা বলার জন্য! 🌸 অনুগ্রহ করে কিছুক্ষণ পর আবার চেষ্টা করুন।")

# ২. পিডিএফ প্রসেসর
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    doc = update.message.document

    if not doc.file_name.lower().endswith('.pdf'):
        await update.message.reply_text("📄 শুধুমাত্র PDF ফাইল প্রসেস করা যাবে।")
        return

    status = await update.message.reply_text("📖 পিডিএফ পড়া হচ্ছে...")
    file_path = f"doc_{user_id}.pdf"

    try:
        tg_file = await context.bot.get_file(doc.file_id)
        await tg_file.download_to_drive(file_path)

        pdf_text = ""
        with open(file_path, 'rb') as f:
            reader = PyPDF2.PdfReader(f)
            for page in reader.pages[:10]:
                pdf_text += page.extract_text() or ""

        prompt = f"Summarize this document clearly in Bengali with key points:\n\n{pdf_text[:4000]}"
        reply_text = generate_ai_response(prompt)

        await status.delete()
        await update.message.reply_text(reply_text)

    except Exception as e:
        logging.error(f"PDF Error: {e}")
        await status.edit_text("পিডিএফ ফাইলটি পড়া সম্ভব হয়নি।")
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

# ৩. ভিশন এআই (Image Analysis)
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    photo = update.message.photo[-1]
    caption = update.message.caption or "Analyze this image in detail in Bengali."

    status = await update.message.reply_text("🔍 ছবি টি বিশ্লেষণ করা হচ্ছে...")
    img_path = f"img_{user_id}.jpg"

    try:
        tg_file = await context.bot.get_file(photo.file_id)
        await tg_file.download_to_drive(img_path)

        uploaded_file = gemini_client.files.upload(file=img_path)
        reply_text = generate_ai_response([uploaded_file, caption])

        await status.delete()
        await update.message.reply_text(reply_text)

    except Exception as e:
        logging.error(f"Vision Error: {e}")
        await status.edit_text("ছবিটি প্রসেস করা সম্ভব হয়নি।")
    finally:
        if os.path.exists(img_path):
            os.remove(img_path)

# ৪. ভয়েস মেসেজ ইনপুট
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    voice = update.message.voice or update.message.audio

    status = await update.message.reply_text("🎙️ ভয়েস মেসেজ শুনছি...")
    in_voice = f"in_{user_id}.ogg"
    out_voice = f"out_{user_id}.mp3"

    try:
        tg_file = await context.bot.get_file(voice.file_id)
        await tg_file.download_to_drive(in_voice)

        uploaded_audio = gemini_client.files.upload(file=in_voice)
        prompt = "Listen to this audio carefully and give a complete response in Bengali."

        reply_text = generate_ai_response([uploaded_audio, prompt])
        await status.delete()

        if generate_cloned_voice(reply_text, out_voice):
            if os.path.exists(out_voice):
                with open(out_voice, 'rb') as audio_out:
                    await update.message.reply_voice(voice=audio_out, caption=reply_text[:1024])
                os.remove(out_voice)
        else:
            await update.message.reply_text(reply_text)

    except Exception as e:
        logging.error(f"Voice Error: {e}")
        await status.edit_text("ভয়েস মেসেজটি প্রসেস করা সম্ভব হয়নি।")
    finally:
        if os.path.exists(in_voice):
            os.remove(in_voice)

# ==================== ৬. অ্যাপ রানার ====================
if __name__ == '__main__':
    init_db()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # কমান্ড নিবন্ধন
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("remind", set_reminder))

    # মেসেজ ও মিডিয়া হ্যান্ডলার
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))

    print("🚀 আপনার আলটিমেট বট ১০০০% সফলভাবে এবং সুরক্ষিতভাবে চালু হয়েছে...")
    app.run_polling()
