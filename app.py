from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
from datetime import datetime
from typing import Any

# Đảm bảo mã hóa UTF-8 cho môi trường chạy
os.environ["PYTHONUTF8"] = "1"

import gspread
import pandas as pd
import pytz
import requests
import streamlit as st
from google import genai
from google.oauth2.service_account import Credentials
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    ContextTypes,
)

# -------------------- CẤU HÌNH HỆ THỐNG --------------------

TIMEZONE = pytz.timezone("Asia/Ho_Chi_Minh")
FB_GRAPH_VERSION = "v19.0"
HOTLINE = "0348861186"

# Vị trí các cột trên Google Sheets (1-based index)
COL_TOPIC = 2          # B: Chủ đề
PHOTO_COLUMNS = [3, 4, 5, 6]  # C, D, E, F: 4 link ảnh
COL_STATUS = 7         # G: Trạng thái
COL_AI_CONTENT = 8     # H: Nội dung AI
COL_POSTED_AT = 10     # J: Ngày đăng

STATUS_PENDING = "Chờ xử lý"
STATUS_WAITING = "Chờ duyệt Telegram"
STATUS_POSTING = "Đang đăng"
STATUS_POSTED = "Đã đăng"
STATUS_REJECTED = "Đã từ chối"
STATUS_ERROR = "Lỗi cần xử lý"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(threadName)s | %(message)s",
)
logger = logging.getLogger("fanpage_autopost")


# -------------------- TIỆN ÍCH BÓC TÁCH LINK GOOGLE DRIVE --------------------

def convert_to_direct_image_url(url: str) -> str:
    """
    Tự động chuyển đổi link xem trước Google Drive hoặc chuỗi ID ảnh 
    thành đường dẫn trực tiếp (Direct Link) để Facebook Graph API tải ảnh về.
    """
    if not url:
        return ""
    url = str(url).strip()
    
    # 1. Khớp link dạng drive.google.com/file/d/{ID}/view hoặc id={ID}
    match = re.search(r'(?:/d/|id=)([a-zA-Z0-9_-]{25,})', url)
    if match:
        drive_id = match.group(1)
        return f"https://lh3.googleusercontent.com/d/{drive_id}"
    
    # 2. Khớp chuỗi ID thuần được dán trực tiếp vào ô
    if re.match(r'^[a-zA-Z0-9_-]{25,}$', url):
        return f"https://lh3.googleusercontent.com/d/{url}"
        
    return url


# -------------------- QUẢN LÝ SECRETS & KẾT NỐI --------------------

def get_secret(name: str, default: Any = None) -> Any:
    try:
        return st.secrets[name]
    except Exception:
        return default


def get_required_secret(name: str) -> str:
    value = get_secret(name)
    if value is None or not str(value).strip():
        err_msg = f"Thiếu cấu hình '{name}'. Vui lòng bổ sung vào Secrets."
        raise RuntimeError(err_msg)
    return str(value).strip()


def get_gspread_client():
    service_account_info = get_secret("gcp_service_account")
    if not service_account_info:
        raise RuntimeError("Thiếu mục [gcp_service_account] trong Secrets.")

    info = dict(service_account_info)
    if "private_key" in info and isinstance(info["private_key"], str):
        info["private_key"] = info["private_key"].replace("\\n", "\n")

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    credentials = Credentials.from_service_account_info(info, scopes=scopes)
    return gspread.authorize(credentials)


def get_sheet():
    spreadsheet_id = get_required_secret("SHEET_ID")
    gc = get_gspread_client()
    return gc.open_by_key(spreadsheet_id).sheet1


def now_string() -> str:
    return datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")


def safe_cell(row: list[str], index_1based: int) -> str:
    idx = index_1based - 1
    return row[idx].strip() if len(row) > idx and row[idx] else ""


def get_row_data(sheet, row_idx: int) -> dict[str, Any]:
    row = sheet.row_values(row_idx)
    raw_photos = [safe_cell(row, col) for col in PHOTO_COLUMNS]
    direct_photos = [convert_to_direct_image_url(p) for p in raw_photos]
    
    return {
        "row_idx": row_idx,
        "topic": safe_cell(row, COL_TOPIC),
        "photos": direct_photos,
        "raw_photos": raw_photos,
        "status": safe_cell(row, COL_STATUS),
        "content": safe_cell(row, COL_AI_CONTENT),
        "posted_at": safe_cell(row, COL_POSTED_AT),
        "raw": row,
    }


def write_status(sheet, row_idx: int, status: str, content: str | None = None):
    """
    Cập nhật trạng thái và nội dung bằng phương thức dải ô liên tục (G đến J)
    để tối ưu số lần gọi API Google Sheets.
    """
    # Lấy dữ liệu hiện tại của hàng để giữ nguyên các ô không thay đổi
    row_vals = sheet.row_values(row_idx)
    current_content = safe_cell(row_vals, COL_AI_CONTENT)
    current_posted_at = safe_cell(row_vals, COL_POSTED_AT)

    new_content = content if content is not None else current_content
    new_posted_at = now_string() if status == STATUS_POSTED else current_posted_at
    col_i_val = safe_cell(row_vals, 9)  # Cột I (nếu có dữ liệu)

    # Cập nhật một lần cho dải ô G{row_idx}:J{row_idx}
    sheet.update(
        range_name=f"G{row_idx}:J{row_idx}",
        values=[[status, new_content, col_i_val, new_posted_at]]
    )


# -------------------- SOẠN NỘI DUNG VỚI GEMINI --------------------

def generate_post_content(topic: str) -> str:
    api_key = get_required_secret("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)

    prompt = f"""
Bạn là chuyên gia sáng tạo nội dung cho Facebook Fanpage.
Hãy viết một bài đăng bằng tiếng Việt về chủ đề: "{topic}".

Yêu cầu chi tiết:
- Tiêu đề thu hút, lôi cuốn nhưng chuẩn xác.
- Nội dung mang lại giá trị thực tế, bố cục rõ ràng, dễ đọc trên điện thoại.
- Sử dụng biểu tượng emoji sinh động, hợp lý.
- Có lời kêu gọi hành động (Call To Action - CTA) mạnh mẽ.
- BẮT BUỘC ở cuối bài viết có thông tin liên hệ và số điện thoại hotline: {HOTLINE}.
- Thêm 3-5 hashtag liên quan ở cuối bài.
- Không tự bịa thông tin khuyến mãi sai sự thật.
- Chỉ trả về duy nhất nội dung bài đăng hoàn chỉnh.
"""

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        text = getattr(response, "text", None)
        if not text or not text.strip():
            raise RuntimeError("Gemini không trả về nội dung.")
        return text.strip()
        
    except Exception:
        # Dự phòng gửi dạng Binary Part nếu môi trường gặp vấn đề encode
        from google.genai import types
        blob_prompt = types.Part.from_bytes(
            data=prompt.encode("utf-8", errors="surrogateescape"),
            mime_type="text/plain"
        )
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[blob_prompt],
        )
        text = getattr(response, "text", None)
        if not text or not text.strip():
            raise RuntimeError("Gemini không trả về nội dung.")
        return text.strip()


# -------------------- ĐĂNG BÀI LÊN FACEBOOK (4 ẢNH) --------------------

def post_to_facebook(content: str, photo_links: list[str]) -> dict[str, Any]:
    page_id = get_required_secret("FB_PAGE_ID")
    access_token = get_required_secret("FB_PAGE_ACCESS_TOKEN")

    photos = [convert_to_direct_image_url(url) for url in photo_links if url and url.strip()]
    if len(photos) != 4:
        return {
            "ok": False,
            "post_id": None,
            "error": f"Cần đúng 4 URL ảnh hợp lệ; hiện tại có {len(photos)} URL.",
        }

    attached_media: list[dict[str, str]] = []
    upload_url = f"https://graph.facebook.com/{FB_GRAPH_VERSION}/{page_id}/photos"

    for number, url in enumerate(photos, start=1):
        try:
            response = requests.post(
                upload_url,
                data={
                    "url": url,
                    "published": "false",
                    "access_token": access_token,
                },
                timeout=45,
            )
            data = response.json()
            if response.status_code != 200 or not data.get("id"):
                return {
                    "ok": False,
                    "post_id": None,
                    "error": (
                        f"Tải ảnh {number}/4 thất bại: "
                        f"{data.get('error', {}).get('message', response.text)}"
                    ),
                }
            attached_media.append({"media_fbid": str(data["id"])})
        except Exception as exc:
            return {
                "ok": False,
                "post_id": None,
                "error": f"Lỗi khi tải ảnh {number}/4: {exc}",
            }

    feed_url = f"https://graph.facebook.com/{FB_GRAPH_VERSION}/{page_id}/feed"
    payload: dict[str, str] = {
        "message": content,
        "access_token": access_token,
    }
    for i, media in enumerate(attached_media):
        payload[f"attached_media[{i}]"] = json.dumps(media)

    try:
        response = requests.post(feed_url, data=payload, timeout=60)
        data = response.json()
        if response.status_code == 200 and data.get("id"):
            return {"ok": True, "post_id": str(data["id"]), "error": None}
        return {
            "ok": False,
            "post_id": None,
            "error": data.get("error", {}).get("message", response.text),
        }
    except Exception as exc:
        return {
            "ok": False,
            "post_id": None,
            "error": f"Lỗi kết nối khi đăng feed: {exc}",
        }


# -------------------- HÀM THỰC THI ĐĂNG BÀI --------------------

def execute_publish_row(sheet, row_idx: int, custom_content: str | None = None) -> dict[str, Any]:
    item = get_row_data(sheet, row_idx)
    topic = item["topic"]
    if not topic:
        return {"ok": False, "error": "Dòng này không có chủ đề."}

    write_status(sheet, row_idx, STATUS_POSTING)

    content = (custom_content or item["content"] or "").strip()
    if not content:
        try:
            content = generate_post_content(topic)
            sheet.update_cell(row_idx, COL_AI_CONTENT, content)
        except Exception as e:
            write_status(sheet, row_idx, STATUS_ERROR)
            return {"ok": False, "error": f"Lỗi khi Gemini tạo nội dung: {e}"}

    result = post_to_facebook(content, item["photos"])
    if result["ok"]:
        write_status(sheet, row_idx, STATUS_POSTED, content)
    else:
        write_status(sheet, row_idx, STATUS_ERROR)

    return result


# -------------------- DUYỆT BÀI QUA TELEGRAM BOT --------------------

def telegram_bot_token() -> str:
    return get_required_secret("TELEGRAM_BOT_TOKEN")


def expected_chat_id() -> str:
    return get_required_secret("TELEGRAM_CHAT_ID")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return

    configured_chat = expected_chat_id()
    message_chat_id = str(query.message.chat_id) if query.message else ""
    if message_chat_id != configured_chat:
        await query.answer("Bạn không có quyền duyệt bài này.", show_alert=True)
        return

    await query.answer()
    try:
        action, row_text = query.data.split(":", 1)
        row_idx = int(row_text)
    except (ValueError, TypeError):
        await query.edit_message_text("Dữ liệu nút bấm không hợp lệ.")
        return

    try:
        sheet = get_sheet()
        item = get_row_data(sheet, row_idx)

        if action == "reject":
            if item["status"] != STATUS_WAITING:
                await query.edit_message_text(
                    f"Bài dòng {row_idx} không còn ở trạng thái chờ duyệt (hiện tại: {item['status'] or 'trống'})."
                )
                return
            write_status(sheet, row_idx, STATUS_REJECTED)
            await query.edit_message_text(f"❌ Đã từ chối bài dòng {row_idx}.")
            return

        if action != "approve":
            await query.edit_message_text("Hành động không hợp lệ.")
            return

        if item["status"] != STATUS_WAITING:
            await query.edit_message_text(
                f"Không thể đăng bài dòng {row_idx}: trạng thái hiện tại là '{item['status'] or 'trống'}'."
            )
            return

        if not item["content"]:
            await query.edit_message_text("Bài viết chưa có nội dung AI.")
            return

        write_status(sheet, row_idx, STATUS_POSTING)
        await query.edit_message_text(f"⏳ Đang tiến hành đăng bài dòng {row_idx} lên Fanpage...")

        result = await asyncio.to_thread(
            post_to_facebook, item["content"], item["photos"]
        )
        if result["ok"]:
            write_status(sheet, row_idx, STATUS_POSTED)
            await query.message.reply_text(
                f"🎉 Đã đăng thành công bài dòng {row_idx} lên Fanpage!\n"
                f"Mã bài viết (Post ID): {result['post_id']}"
            )
        else:
            write_status(sheet, row_idx, STATUS_ERROR)
            await query.message.reply_text(
                f"❌ Đăng bài dòng {row_idx} thất bại.\n"
                f"Chi tiết lỗi: {result['error']}\n"
                "Vui lòng kiểm tra lại quyền Fanpage hoặc link 4 ảnh."
            )
    except Exception as exc:
        logger.exception("Lỗi khi xử lý duyệt trên Telegram")
        try:
            await query.message.reply_text(f"❌ Xảy ra lỗi hệ thống: {exc}")
        except Exception:
            pass


async def send_telegram_approval(row_idx: int, topic: str, content: str, photo_links: list[str]):
    from telegram import Bot

    bot = Bot(token=telegram_bot_token())
    photo_text = "\n".join(
        f"{i}. {url}" for i, url in enumerate(photo_links, start=1) if url
    )
    message_text = (
        f"🔔 YÊU CẦU DUYỆT BÀI — Dòng {row_idx}\n\n"
        f"📌 Chủ đề: {topic}\n\n"
        f"📝 Nội dung:\n{content}\n\n"
        f"🖼 Danh sách 4 ảnh:\n{photo_text or '(Chưa có link ảnh)'}"
    )
    
    # Cắt an toàn nếu nội dung vượt quá giới hạn 4096 ký tự của Telegram
    if len(message_text) > 4000:
        message_text = message_text[:3990] + "\n\n...(đã rút gọn)..."

    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Duyệt & Đăng ngay", callback_data=f"approve:{row_idx}"),
            InlineKeyboardButton("❌ Từ chối", callback_data=f"reject:{row_idx}"),
        ]]
    )
    await bot.send_message(
        chat_id=expected_chat_id(),
        text=message_text,
        reply_markup=keyboard,
    )


# -------------------- HÀNG ĐỢI XỬ LÝ --------------------

def find_next_pending_row(sheet) -> tuple[int, dict[str, Any]] | None:
    records = sheet.get_all_values()
    for row_idx, row in enumerate(records[1:], start=2):
        topic = safe_cell(row, COL_TOPIC)
        status = safe_cell(row, COL_STATUS)
        if topic and status in ("", STATUS_PENDING):
            item = get_row_data(sheet, row_idx)
            return row_idx, item
    return None


def trigger_approval_job():
    """Hàm tạo nội dung bằng Gemini và đẩy bài chờ sang Telegram để người dùng duyệt."""
    sheet = get_sheet()
    pending = find_next_pending_row(sheet)
    if not pending:
        return False, "Không có bài nào đang ở trạng thái 'Chờ xử lý'."

    row_idx, item = pending
    write_status(sheet, row_idx, STATUS_POSTING)
    try:
        content = generate_post_content(item["topic"])
        write_status(sheet, row_idx, STATUS_WAITING, content)
        asyncio.run(
            send_telegram_approval(row_idx, item["topic"], content, item["photos"])
        )
        return True, f"Đã gửi bài dòng {row_idx} sang Telegram chờ duyệt."
    except Exception as e:
        logger.exception("Lỗi khi tạo nội dung hoặc gửi duyệt bài dòng %s", row_idx)
        write_status(sheet, row_idx, STATUS_ERROR)
        return False, f"Lỗi xử lý dòng {row_idx}: {e}"


# -------------------- DỊCH VỤ TELEGRAM LISTENER --------------------

def telegram_thread_main(stop_event: threading.Event):
    async def runner():
        app: Application = ApplicationBuilder().token(telegram_bot_token()).build()
        app.add_handler(CallbackQueryHandler(handle_callback))

        await app.initialize()
        await app.start()
        if app.updater is None:
            raise RuntimeError("Telegram updater chưa được khởi tạo.")
        await app.updater.start_polling(drop_pending_updates=False)
        logger.info("Telegram listener khởi chạy thành công.")

        try:
            while not stop_event.is_set():
                await asyncio.sleep(1)
        finally:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()

    try:
        asyncio.run(runner())
    except Exception:
        logger.exception("Telegram listener dừng do lỗi.")


@st.cache_resource
def start_telegram_bot():
    stop_event = threading.Event()
    bot_thread = threading.Thread(
        target=telegram_thread_main,
        args=(stop_event,),
        name="telegram-listener",
        daemon=True,
    )
    bot_thread.start()
    return stop_event, bot_thread


# -------------------- GIAO DIỆN STREAMLIT DASHBOARD --------------------

st.set_page_config(
    page_title="Hệ Thống Đăng Bài Fanpage",
    page_icon="🚀",
    layout="wide",
)
st.title("🚀 Quản Lý Đăng Bài Fanpage (Gemini + Telegram + Sheets)")

with st.expander("⚙️ Kiểm tra cấu hình Secrets", expanded=False):
    required_names = [
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "FB_PAGE_ID",
        "FB_PAGE_ACCESS_TOKEN",
        "GEMINI_API_KEY",
        "SHEET_ID",
        "gcp_service_account",
    ]
    missing = [name for name in required_names if not get_secret(name)]
    if missing:
        st.error("Thiếu cấu hình: " + ", ".join(missing))
    else:
        st.success("Tất cả cấu hình bảo mật đã sẵn sàng!")

try:
    telegram_stop_event, telegram_thread = start_telegram_bot()
except Exception as exc:
    st.error(f"Không thể khởi động dịch vụ Telegram: {exc}")

top1, top2, top3 = st.columns(3)
top1.metric("Giờ hệ thống", datetime.now(TIMEZONE).strftime("%H:%M:%S"))
top2.metric("Ngày hiện tại", datetime.now(TIMEZONE).strftime("%d/%m/%Y"))
top3.metric("Telegram Bot Duyệt Bài", "Đang chạy" if "telegram_thread" in locals() and telegram_thread.is_alive() else "Dừng")

left, right = st.columns([1, 1])
with left:
    st.subheader("🧪 Điều khiển tác vụ")
    
    # Nút 1: Đăng trực tiếp bài tiếp theo lên Fanpage
    if st.button("⚡ Đăng trực tiếp bài kế tiếp lên Facebook", type="primary", use_container_width=True):
        with st.spinner("Đang tìm bài chờ và đăng trực tiếp lên Facebook..."):
            s = get_sheet()
            pending = find_next_pending_row(s)
            if not pending:
                st.warning("Không có bài nào đang ở trạng thái 'Chờ xử lý'.")
            else:
                next_row_idx, _ = pending
                res = execute_publish_row(s, next_row_idx)
                if res["ok"]:
                    st.success(f"🎉 Đã đăng thành công dòng {next_row_idx}! Post ID: {res['post_id']}")
                    st.rerun()
                else:
                    st.error(f"❌ Đăng thất bại: {res['error']}")

    # Nút 2: Soạn bài và gửi sang Telegram để người dùng duyệt
    if st.button("📩 Tạo bài & Gửi duyệt qua Telegram", use_container_width=True):
        with st.spinner("Đang viết nội dung và gửi tin nhắn Telegram..."):
            ok, msg = trigger_approval_job()
            if ok:
                st.success(msg)
            else:
                st.warning(msg)
        st.rerun()

    if st.button("🔄 Tải lại dữ liệu Google Sheets", use_container_width=True):
        st.rerun()

with right:
    st.subheader("📌 Hướng dẫn sử dụng")
    st.write("- **Đăng trực tiếp:** Lấy dòng 'Chờ xử lý' đầu tiên, gọi Gemini viết bài và đẩy thẳng lên Facebook Fanpage.")
    st.write("- **Tạo bài & Gửi duyệt:** Gemini sẽ viết bài trước rồi gửi kèm 4 link ảnh sang Telegram. Khi bạn nhấn 'Duyệt & Đăng ngay' trên bot Telegram, bài viết mới được đăng lên Fanpage.")
    st.write("- **Chỉnh sửa thủ công:** Chọn bài viết trong danh sách phía dưới để chỉnh sửa nội dung hoặc xem trước 4 ảnh.")

st.divider()
st.subheader("📋 Bảng quản lý bài đăng")

try:
    sheet = get_sheet()
    all_values = sheet.get_all_values()
    if not all_values:
        st.warning("Google Sheet chưa có dữ liệu.")
    else:
        data_rows = []
        for row_idx, row in enumerate(all_values[1:], start=2):
            item = get_row_data(sheet, row_idx)
            data_rows.append({
                "Dòng": row_idx,
                "Chủ đề": item["topic"],
                "Trạng thái": item["status"],
                "Nội dung AI": item["content"],
                "Ảnh 1": item["photos"][0],
                "Ảnh 2": item["photos"][1],
                "Ảnh 3": item["photos"][2],
                "Ảnh 4": item["photos"][3],
                "Ngày đăng": item["posted_at"],
            })

        if data_rows:
            df = pd.DataFrame(data_rows)
            st.dataframe(
                df[["Dòng", "Chủ đề", "Trạng thái", "Ngày đăng"]],
                use_container_width=True,
                hide_index=True,
            )

            row_options = {
                f"Dòng {int(row['Dòng'])} — {row['Chủ đề'] or '(Chưa có chủ đề)'} [{row['Trạng thái'] or 'Chờ xử lý'}]": int(row["Dòng"])
                for _, row in df.iterrows()
            }
            selected_label = st.selectbox("Chọn bài viết để kiểm tra hoặc đăng ngay:", list(row_options.keys()))
            selected_row_idx = row_options[selected_label]
            selected = get_row_data(sheet, selected_row_idx)

            st.markdown(f"### 📝 {selected['topic']}")
            st.caption(f"Trạng thái: **{selected['status'] or 'Chưa xử lý'}**")

            st.markdown("#### Nội dung soạn thảo")
            post_content = st.text_area(
                "Nội dung bài viết (Có thể chỉnh sửa trước khi nhấn Đăng):",
                value=selected["content"],
                height=220,
                key=f"content_{selected_row_idx}"
            )

            st.markdown("#### 🖼️ Xem trước 4 ảnh")
            image_cols = st.columns(4)
            for i, (col, url) in enumerate(zip(image_cols, selected["photos"]), start=1):
                with col:
                    st.markdown(f"**Ảnh {i}**")
                    if url:
                        try:
                            st.image(url, use_container_width=True)
                            st.caption(url)
                        except Exception:
                            st.warning("Không thể hiển thị ảnh.")
                            st.markdown(f"[Mở link ảnh]({url})")
                    else:
                        st.info("Chưa có URL ảnh")

            st.write("")
            col_btn1, col_btn2 = st.columns([1, 2])
            with col_btn1:
                if st.button(f"🚀 Đăng bài dòng {selected_row_idx} ngay lên Fanpage", type="primary", use_container_width=True):
                    with st.spinner("Đang tiến hành đăng bài lên Fanpage..."):
                        res = execute_publish_row(sheet, selected_row_idx, custom_content=post_content)
                        if res["ok"]:
                            st.success(f"🎉 Đã đăng thành công lên Fanpage! Post ID: {res['post_id']}")
                            st.rerun()
                        else:
                            st.error(f"❌ Đăng thất bại: {res['error']}")
except Exception as exc:
    st.error(f"Lỗi khi đọc Google Sheets: {exc}")
