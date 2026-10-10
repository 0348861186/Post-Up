import streamlit as st
import pandas as pd
import requests
import time
import os
from datetime import datetime
import google.generativeai as genai
from google.oauth2.service_account import Credentials
import gspread
from apscheduler.schedulers.background import BackgroundScheduler

# --- CẤU HÌNH BIẾN MÔI TRƯỜNG (LẤY TỪ STREAMLIT SECRETS) ---
# Tránh để lộ token trực tiếp trong code
TELEGRAM_TOKEN = st.secrets.get("TELEGRAM_TOKEN", "YOUR_TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = st.secrets.get("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID")
FB_PAGE_ACCESS_TOKEN = st.secrets.get("FB_PAGE_ACCESS_TOKEN", "YOUR_FB_TOKEN")
FB_PAGE_ID = st.secrets.get("FB_PAGE_ID", "YOUR_PAGE_ID")
GEMINI_API_KEY = st.secrets.get("GEMINI_API_KEY", "YOUR_GEMINI_KEY")
GOOGLE_SHEETS_URL = st.secrets.get("GOOGLE_SHEETS_URL", "YOUR_SHEET_URL")

# Cấu hình Google Credentials từ Streamlit Secrets
google_creds_dict = st.secrets.get("gcp_service_account", None)

# --- KHỞI TẠO CÁC KẾT NỐI API ---
genai.configure(api_key=GEMINI_API_KEY)

def get_google_sheet():
    if not google_creds_dict:
        st.error("Chưa cấu hình tài khoản dịch vụ Google trong Secrets!")
        return None
    scopes = ["https://googleapis.com"]
    creds = Credentials.from_service_account_info(google_creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    sheet = client.open_by_url(GOOGLE_SHEETS_URL).sheet1
    return sheet

# --- HÀM XỬ LÝ CHỨC NĂNG CHÍNH ---

def generate_ai_content(topic):
    """3) Gửi chủ đề sang Gemini AI để soạn nội dung"""
    try:
        model = genai.GenerativeModel("gemini-1.5-flash")
        prompt = f"Hãy viết một bài đăng Facebook hấp dẫn về chủ đề: '{topic}'. Nội dung ngắn gọn, thu hút, có sử dụng emoji phù hợp."
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        return f"Lỗi tạo nội dung AI: {str(e)}"

def send_to_telegram_for_approval(post_id, topic, content, img_urls):
    """4) Gửi lên Telegram để xem qua và duyệt bài"""
    # Tạo các nút bấm Inline để duyệt nhanh qua Telegram
    url = f"https://telegram.org{TELEGRAM_TOKEN}/sendMessage"
    
    # Định dạng tin nhắn gửi đến Admin duyệt
    message_text = (
        f"📝 **YÊU CẦU DUYỆT BÀI ĐĂNG (Mã bài: {post_id})**\n\n"
        f"📌 **Chủ đề:** {topic}\n\n"
        f"✍️ **Nội dung AI soạn:**\n{content}\n\n"
        f"🖼️ **Ảnh đi kèm:** {', '.join([u for u in img_urls if u])}\n"
    )
    
    # Các nút Duyệt / Từ chối (Lưu ý: Bạn cần một webhook/server phụ để xử lý nút bấm này, 
    # ở mức độ đơn giản trên Streamlit Cloud, ta sẽ duyệt thủ công trên giao diện Streamlit hoặc qua lệnh Bot)
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message_text,
        "parse_mode": "Markdown"
    }
    requests.post(url, json=payload)

def publish_to_facebook(content, img_urls):
    """6) Đăng lên Fanpage kèm 4 ảnh và Số điện thoại ở cuối"""
    # Thêm số điện thoại cố định vào cuối bài đăng theo yêu cầu
    final_content = f"{content}\n\n📞 Liên hệ: 0348861186"
    
    try:
        # Bước 1: Upload từng ảnh lên Fanpage dưới dạng tạm ẩn (published=false) để lấy ID ảnh
        media_ids = []
        for url in img_urls:
            if not url or not url.startswith("http"):
                continue
            upload_url = f"https://facebook.com{FB_PAGE_ID}/photos"
            payload = {
                'url': url,
                'published': 'false',
                'access_token': FB_PAGE_ACCESS_TOKEN
            }
            res = requests.post(upload_url, data=payload).json()
            if 'id' in res:
                media_ids.append(res['id'])
        
        if not media_ids:
            return False, "Không có ảnh hợp lệ để đăng."

        # Bước 2: Tạo bài đăng đa ảnh (Multi-photo post) bằng cách nhóm các ID ảnh lại
        feed_url = f"https://facebook.com{FB_PAGE_ID}/feed"
        
        attached_media = [{"media_fbid": m_id} for m_id in media_ids]
        
        payload = {
            'message': final_content,
            'attached_media': str(attached_media).replace("'", '"'), # Định dạng đúng chuẩn JSON array
            'access_token': FB_PAGE_ACCESS_TOKEN
        }
        
        result = requests.post(feed_url, data=payload).json()
        if 'id' in result:
            return True, result['id']
        else:
            return False, str(result)
    except Exception as e:
        return False, str(e)

def process_daily_job():
    """Hàm chạy tự động vào đúng 9:00 AM"""
    sheet = get_google_sheet()
    if not sheet:
        return
    
    records = sheet.get_all_records()
    
    # Tìm bài viết đầu tiên chưa có Trạng thái "Đã đăng" hoặc đang chờ duyệt
    for i, row in enumerate(records, start=2): # Hàng 2 trong Sheets là dòng dữ liệu đầu tiên
        status = str(row.get('Trạng thái', '')).strip()
        
        if status == "" or status.lower() == "chờ duyệt":
            post_id = row.get('PostID')
            topic = row.get('Chủ đề')
            
            # 5) Lấy link ảnh từ 4 cột trên cùng hàng ngang
            img_urls = [
                str(row.get('Ảnh 1', '')).strip(),
                str(row.get('Ảnh 2', '')).strip(),
                str(row.get('Ảnh 3', '')).strip(),
                str(row.get('Ảnh 4', '')).strip()
            ]
            
            # Chạy quy trình soạn bài
            ai_text = generate_ai_content(topic)
            
            # Cập nhật nội dung AI vào Sheet trước để lưu trữ
            sheet.update_cell(i, 8, ai_text) # Cột H (Cột số 8) là 'Nội dung AI'
            sheet.update_cell(i, 7, "Chờ duyệt trên Telegram") # Cột G (Cột số 7) là 'Trạng thái'
            
            # Gửi thông báo qua Telegram
            send_to_telegram_for_approval(post_id, topic, ai_text, img_urls)
            break # Mỗi ngày chỉ kích hoạt 1 bài lúc 9:00 AM

# --- CẤU HÌNH TRÌNH KHỞI CHẠY TỰ ĐỘNG (9:00 AM) ---
if 'scheduler_started' not in st.session_state:
    scheduler = BackgroundScheduler(timezone="Asia/Ho_Chi_Minh")
    # 1) Đúng 9 giờ sáng kích hoạt đăng bài lên fanpage
    scheduler.add_job(process_daily_job, 'cron', hour=9, minute=0)
    scheduler.start()
    st.session_state['scheduler_started'] = True

# --- GIAO DIỆN QUẢN TRỊ TRÊN STREAMLIT ---
st.set_page_config(page_title="Hệ thống đăng bài tự động", layout="wide")
st.title("🤖 Hệ Thống Đăng Bài Fanpage Tự Động 9:00 AM")

if st.button("⚡ Chạy kích hoạt thủ công ngay bây giờ (Test)"):
    with st.spinner("Đang xử lý quy trình..."):
        process_daily_job()
    st.success("Đã chạy thử nghiệm quy trình! Kiểm tra Telegram/Google Sheets.")

st.subheader("📊 Xem trạng thái hàng đợi dữ liệu từ Google Sheets")
try:
    sheet = get_google_sheet()
    if sheet:
        data = pd.DataFrame(sheet.get_all_records())
        st.dataframe(data, use_container_width=True)
except Exception as e:
    st.warning("Điền cấu hình hợp lệ trong Secrets để xem bảng dữ liệu.")
