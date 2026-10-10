import streamlit as st
import datetime
import pytz
import requests
import json
import gspread
from google.oauth2.service_account import Credentials
from google import genai
from apscheduler.schedulers.background import BackgroundScheduler
import asyncio
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Bot, Update
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, ContextTypes

# ==================== CẤU HÌNH & KHỞI TẠO ====================
TIMEZONE = pytz.timezone("Asia/Ho_Chi_Minh")

# Lấy cấu hình bảo mật từ st.secrets (Streamlit Cloud Settings)
TELEGRAM_BOT_TOKEN = st.secrets["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = st.secrets["TELEGRAM_CHAT_ID"]
FB_PAGE_ID = st.secrets["FB_PAGE_ID"]
FB_PAGE_ACCESS_TOKEN = st.secrets["FB_PAGE_ACCESS_TOKEN"]
GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]
SHEET_ID_OR_NAME = st.secrets["SHEET_NAME"]
GOOGLE_CREDENTIALS = st.secrets["gcp_service_account"]

# Khởi tạo Gemini Client
gemini_client = genai.Client(api_key=GEMINI_API_KEY)


def get_gspread_client():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = Credentials.from_service_account_info(GOOGLE_CREDENTIALS, scopes=scopes)
    return gspread.authorize(creds)


# ==================== 1. TẠO NỘI DUNG VỚI GEMINI ====================
def generate_post_content(topic: str) -> str:
    prompt = f"""
    Bạn là chuyên gia sáng tạo nội dung mạng xã hội chuyên nghiệp.
    Hãy viết một bài đăng Facebook Fanpage thật hấp dẫn về chủ đề: "{topic}".
    
    Yêu cầu:
    - Tiêu đề giật tít, thu hút người đọc ngay 3 giây đầu.
    - Nội dung chia sẻ hữu ích, bố cục rõ ràng, sử dụng icon/emoji sinh động.
    - Kêu gọi hành động (CTA) tương tác mạnh mẽ.
    - Cuối bài BẮT BUỘC có thông tin liên hệ và số điện thoại hotline: 0348861186.
    - Kèm các hashtag liên quan ở cuối bài.
    """
    response = gemini_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )
    return response.text.strip()


# ==================== 2. ĐĂNG BÀI LÊN FACEBOOK (4 ẢNH) ====================
def post_to_facebook(content: str, photo_links: list) -> bool:
    """
    Đăng 4 ảnh cùng 1 status lên Facebook Fanpage.
    Cách hoạt động: Đăng từng ảnh dạng unpublished -> tạo feed post kèm danh sách media_fbid.
    """
    attached_media = []
    
    # Upload từng ảnh ở chế độ tạm (published=False)
    for url in photo_links:
        if not url:
            continue
        upload_url = f"https://graph.facebook.com/v19.0/{FB_PAGE_ID}/photos"
        params = {
            "url": url.strip(),
            "published": "false",
            "access_token": FB_PAGE_ACCESS_TOKEN
        }
        res = requests.post(upload_url, data=params)
        if res.status_code == 200:
            media_id = res.json().get("id")
            attached_media.append({"media_fbid": media_id})
        else:
            print(f"Lỗi upload ảnh {url}: {res.text}")

    # Đăng bài viết hoàn chỉnh kèm danh sách ảnh
    feed_url = f"https://graph.facebook.com/v19.0/{FB_PAGE_ID}/feed"
    payload = {
        "message": content,
        "access_token": FB_PAGE_ACCESS_TOKEN
    }
    
    for i, media in enumerate(attached_media):
        payload[f"attached_media[{i}]"] = json.dumps(media)

    feed_res = requests.post(feed_url, data=payload)
    return feed_res.status_code == 200


# ==================== 3. CẬP NHẬT GOOGLE SHEET ====================
def update_sheet_status(row_idx: int, status: str, content: str = None):
    gc = get_gspread_client()
    sheet = gc.open(SHEET_ID_OR_NAME).sheet1
    
    now_str = datetime.datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    
    # Cột G: Trạng thái (Cột 7)
    sheet.update_cell(row_idx, 7, status)
    
    # Cột H: Nội dung AI (Cột 8)
    if content:
        sheet.update_cell(row_idx, 8, content)
        
    # Cột J: Ngày đăng (Cột 10)
    if status == "Đã đăng":
        sheet.update_cell(row_idx, 10, now_str)


# ==================== 4. GỬI DUYỆT BÀI QUA TELEGRAM ====================
def send_telegram_approval(row_idx: int, topic: str, content: str, photo_links: list):
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    
    # Callback data lưu số dòng (row) để xử lý khi bấm nút
    keyboard = [
        [
            InlineKeyboardButton("✅ Duyệt & Đăng ngay", callback_data=f"approve_{row_idx}"),
            InlineKeyboardButton("❌ Hủy bỏ", callback_data=f"reject_{row_idx}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    message_text = (
        f"🔔 **YÊU CẦU DUYỆT BÀI ĐĂNG (Dòng {row_idx})**\n\n"
        f"📌 **Chủ đề:** {topic}\n\n"
        f"📝 **Nội dung do Gemini soạn:**\n{content}\n\n"
        f"🖼 **Link 4 ảnh:**\n" + "\n".join([f"- {url}" for url in photo_links if url])
    )

    asyncio.run(bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=message_text,
        reply_markup=reply_markup,
        parse_mode="Markdown"
    ))


# ==================== 5. TIẾN TRÌNH KÍCH HOẠT LÚC 9 GIỜ SÁNG ====================
def daily_job():
    print(f"[{datetime.datetime.now(TIMEZONE)}] Bắt đầu quét hàng đợi lúc 9:00 AM...")
    gc = get_gspread_client()
    sheet = gc.open(SHEET_ID_OR_NAME).sheet1
    records = sheet.get_all_values()

    # Duyệt từ dòng 2 (bỏ qua tiêu đề)
    for idx, row in enumerate(records[1:], start=2):
        topic = row[1] if len(row) > 1 else ""
        photo_1 = row[2] if len(row) > 2 else ""
        photo_2 = row[3] if len(row) > 3 else ""
        photo_3 = row[4] if len(row) > 4 else ""
        photo_4 = row[5] if len(row) > 5 else ""
        status = row[6] if len(row) > 6 else ""

        # Lấy bài chưa được đăng hoặc chưa duyệt
        if topic and (not status or status.strip() == ""):
            photos = [photo_1, photo_2, photo_3, photo_4]
            
            # Tạo nội dung bằng Gemini
            ai_content = generate_post_content(topic)
            
            # Cập nhật vào Google Sheet trạng thái 'Chờ duyệt' và cột 'Nội dung AI'
            update_sheet_status(idx, "Chờ duyệt Telegram", ai_content)
            
            # Gửi tin nhắn duyệt đến Telegram
            send_telegram_approval(idx, topic, ai_content, photos)
            
            # Mỗi sáng xử lý 1 bài
            break


# ==================== 6. XỬ LÝ SỰ KIỆN DUYỆT BÀI TELEGRAM ====================
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    action, row_idx_str = query.data.split("_")
    row_idx = int(row_idx_str)

    gc = get_gspread_client()
    sheet = gc.open(SHEET_ID_OR_NAME).sheet1
    row = sheet.row_values(row_idx)
    
    content = row[7]   # Cột H: Nội dung AI
    photos = [row[2], row[3], row[4], row[5]]  # Cột C, D, E, F: 4 ảnh

    if action == "approve":
        await query.edit_message_text(text=f"⏳ Đang tiến hành đăng bài lên Fanpage (Dòng {row_idx})...")
        success = post_to_facebook(content, photos)
        if success:
            update_sheet_status(row_idx, "Đã đăng")
            await query.message.reply_text(f"🎉 Đã đăng thành công bài dòng {row_idx} lên Facebook Fanpage!")
        else:
            await query.message.reply_text(f"❌ Có lỗi xảy ra khi gọi Facebook API.")
    elif action == "reject":
        update_sheet_status(row_idx, "Đã từ chối")
        await query.edit_message_text(text=f"❌ Bạn đã từ chối bài viết dòng {row_idx}.")


def run_telegram_listener():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.run_polling(drop_pending_updates=True)


# ==================== GIAO DIỆN STREAMLIT & KHỞI CHẠY ====================
st.set_page_config(page_title="Hệ thống Auto Post Fanpage", layout="wide")
st.title("🚀 Hệ thống Tự động Soạn & Đăng bài Fanpage")

# Khởi chạy Scheduler ngầm một lần duy nhất
if "scheduler_started" not in st.session_state:
    scheduler = BackgroundScheduler(timezone=TIMEZONE)
    # Lên lịch chạy vào đúng 9:00 AM hàng ngày
    scheduler.add_job(daily_job, 'cron', hour=9, minute=0)
    scheduler.start()
    st.session_state["scheduler_started"] = True

st.success("✅ Hệ thống hẹn giờ đang chạy ngầm: Tự động kích hoạt lúc **09:00 AM** mỗi ngày.")

st.markdown("""
### Quy trình hoạt động:
1. **09:00 AM**: Hệ thống đọc dòng tiếp theo chưa đăng trong Google Sheets.
2. **Gemini AI**: Soạn nội dung bán hàng/quảng bá kèm hotline `0348861186`.
3. **Telegram**: Gửi thông báo kèm nút **[Duyệt]** hoặc **[Hủy]**.
4. **Facebook Fanpage**: Tải lên 4 ảnh trong hàng và đăng bài sau khi bấm duyệt.
""")

col1, col2 = st.columns(2)
with col1:
    if st.button("🧪 Chạy thử thủ công ngay (Test Run)"):
        with st.spinner("Đang chạy thử quy trình..."):
            daily_job()
            st.success("Đã hoàn tất! Vui lòng kiểm tra tin nhắn bot Telegram để duyệt bài.")

with col2:
    if st.button("🔄 Đọc dữ liệu Google Sheets"):
        gc = get_gspread_client()
        sheet = gc.open(SHEET_ID_OR_NAME).sheet1
        data = sheet.get_all_records()
        st.dataframe(data)
