from pathlib import Path

code = r'''"""
Auto Post Facebook Fanpage
- Streamlit dashboard: xem nội dung AI và tối đa 4 ảnh từ Google Sheets.
- Gemini: tạo nội dung bài đăng.
- Telegram: gửi nút Duyệt / Từ chối.
- Facebook Graph API: đăng bài kèm nhiều ảnh.
- APScheduler: chạy hàng ngày lúc 09:00, múi giờ Việt Nam.

Google Sheets schema mặc định:
A: (không dùng) | B: Chủ đề | C-F: URL ảnh 1-4 | G: Trạng thái
H: Nội dung AI | I: (không dùng) | J: Ngày đăng

Secrets cần khai báo trong .streamlit/secrets.toml hoặc Streamlit Cloud:
TELEGRAM_BOT_TOKEN = "..."
TELEGRAM_CHAT_ID = "..."
FB_PAGE_ID = "..."
FB_PAGE_ACCESS_TOKEN = "..."
GEMINI_API_KEY = "..."
SHEET_ID = "Google Spreadsheet ID"
[gcp_service_account]
... thông tin service account JSON ...
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime
from typing import Any

import gspread
import pytz
import requests
import streamlit as st
from google import genai
from google.oauth2.service_account import Credentials
from apscheduler.schedulers.background import BackgroundScheduler
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    ContextTypes,
)

# -------------------- CẤU HÌNH --------------------

TIMEZONE = pytz.timezone("Asia/Ho_Chi_Minh")
FB_GRAPH_VERSION = "v23.0"  # Có thể đổi sang phiên bản Graph API được app của bạn hỗ trợ.
HOTLINE = "0348861186"
PHOTO_COLUMNS = [3, 4, 5, 6]  # C-F theo chỉ số cột gspread (1-based)
COL_TOPIC = 2
COL_STATUS = 7
COL_AI_CONTENT = 8
COL_POSTED_AT = 10

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


def get_secret(name: str, default: Any = None) -> Any:
    """Đọc secret an toàn để dashboard vẫn hiện hướng dẫn khi chưa cấu hình."""
    try:
        return st.secrets[name]
    except Exception:
        return default


def get_required_secret(name: str) -> str:
    value = get_secret(name)
    if value is None or not str(value).strip():
        raise RuntimeError(
            f"Thiếu cấu hình {name}. Hãy thêm vào Streamlit secrets."
        )
    return str(value).strip()


def get_gspread_client():
    service_account_info = get_secret("gcp_service_account")
    if not service_account_info:
        raise RuntimeError(
            "Thiếu [gcp_service_account] trong Streamlit secrets."
        )

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    credentials = Credentials.from_service_account_info(
        dict(service_account_info), scopes=scopes
    )
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
    return {
        "row_idx": row_idx,
        "topic": safe_cell(row, COL_TOPIC),
        "photos": [safe_cell(row, col) for col in PHOTO_COLUMNS],
        "status": safe_cell(row, COL_STATUS),
        "content": safe_cell(row, COL_AI_CONTENT),
        "posted_at": safe_cell(row, COL_POSTED_AT),
        "raw": row,
    }


def write_status(sheet, row_idx: int, status: str, content: str | None = None):
    # Cột G: trạng thái
    sheet.update_cell(row_idx, COL_STATUS, status)
    # Cột H: nội dung AI
    if content is not None:
        sheet.update_cell(row_idx, COL_AI_CONTENT, content)
    # Cột J: thời gian đăng
    if status == STATUS_POSTED:
        sheet.update_cell(row_idx, COL_POSTED_AT, now_string())


# -------------------- GEMINI --------------------

def generate_post_content(topic: str) -> str:
    api_key = get_required_secret("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)

    prompt = f"""
Bạn là chuyên gia sáng tạo nội dung cho Facebook Fanpage.
Hãy viết bài đăng bằng tiếng Việt về chủ đề: "{topic}".

Yêu cầu:
- Tiêu đề hấp dẫn nhưng không giật tít sai sự thật.
- Nội dung hữu ích, dễ đọc trên điện thoại, chia đoạn rõ ràng.
- Dùng emoji vừa phải.
- Có lời kêu gọi hành động (CTA) rõ ràng.
- Cuối bài có thông tin liên hệ và hotline: {HOTLINE}.
- Thêm 3-5 hashtag liên quan ở cuối bài.
- Không tự bịa giá, khuyến mãi, địa chỉ hoặc thông tin chưa được cung cấp.
- Chỉ trả về nội dung bài đăng hoàn chỉnh, không giải thích quy trình.
"""
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
    )
    text = getattr(response, "text", None)
    if not text or not text.strip():
        raise RuntimeError("Gemini không trả về nội dung văn bản.")
    return text.strip()


# -------------------- FACEBOOK --------------------

def post_to_facebook(content: str, photo_links: list[str]) -> dict[str, Any]:
    """
    Đăng một bài kèm ảnh.
    Yêu cầu mặc định: phải có đủ 4 URL ảnh và tải thành công cả 4 ảnh.
    Trả về {"ok": bool, "post_id": str|None, "error": str|None}.
    """
    page_id = get_required_secret("FB_PAGE_ID")
    access_token = get_required_secret("FB_PAGE_ACCESS_TOKEN")

    photos = [url.strip() for url in photo_links if url and url.strip()]
    if len(photos) != 4:
        return {
            "ok": False,
            "post_id": None,
            "error": f"Cần đủ 4 URL ảnh hợp lệ; hiện có {len(photos)} URL.",
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
        except (requests.RequestException, ValueError) as exc:
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
        import json
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
    except (requests.RequestException, ValueError) as exc:
        return {
            "ok": False,
            "post_id": None,
            "error": f"Lỗi kết nối khi đăng feed: {exc}",
        }


# -------------------- TELEGRAM CALLBACK --------------------

def telegram_bot_token() -> str:
    return get_required_secret("TELEGRAM_BOT_TOKEN")


def expected_chat_id() -> str:
    return get_required_secret("TELEGRAM_CHAT_ID")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return

    # Chỉ cho phép người chat đã cấu hình duyệt bài.
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
                    f"Bài dòng {row_idx} không còn ở trạng thái chờ duyệt "
                    f"(hiện tại: {item['status'] or 'trống'})."
                )
                return
            write_status(sheet, row_idx, STATUS_REJECTED)
            await query.edit_message_text(f"❌ Đã từ chối bài dòng {row_idx}.")
            return

        if action != "approve":
            await query.edit_message_text("Hành động không được hỗ trợ.")
            return

        # Kiểm tra trạng thái trước khi đăng, giảm nguy cơ đăng trùng.
        if item["status"] != STATUS_WAITING:
            await query.edit_message_text(
                f"Không đăng bài dòng {row_idx}: trạng thái hiện tại là "
                f"'{item['status'] or 'trống'}'."
            )
            return

        if not item["content"]:
            await query.edit_message_text("Bài viết chưa có nội dung AI.")
            return

        # Ghi trạng thái trước khi gọi API để ngăn hai lần bấm gần nhau.
        write_status(sheet, row_idx, STATUS_POSTING)
        await query.edit_message_text(
            f"⏳ Đang đăng bài dòng {row_idx} lên Fanpage..."
        )

        result = await asyncio.to_thread(
            post_to_facebook, item["content"], item["photos"]
        )
        if result["ok"]:
            write_status(sheet, row_idx, STATUS_POSTED)
            await query.message.reply_text(
                f"🎉 Đăng thành công bài dòng {row_idx}.\n"
                f"Facebook Post ID: {result['post_id']}"
            )
        else:
            write_status(sheet, row_idx, STATUS_ERROR)
            await query.message.reply_text(
                f"❌ Đăng bài dòng {row_idx} thất bại.\n"
                f"Lỗi: {result['error']}\n"
                "Kiểm tra lại ảnh/quyền Facebook API trước khi thử lại."
            )
    except Exception as exc:
        logger.exception("Lỗi xử lý callback Telegram")
        try:
            await query.message.reply_text(f"❌ Có lỗi xử lý duyệt bài: {exc}")
        except Exception:
            pass


async def send_telegram_approval(
    row_idx: int, topic: str, content: str, photo_links: list[str]
):
    from telegram import Bot

    bot = Bot(token=telegram_bot_token())
    photo_text = "\n".join(
        f"{i}. {url}" for i, url in enumerate(photo_links, start=1) if url
    )
    message_text = (
        f"🔔 YÊU CẦU DUYỆT BÀI — Dòng {row_idx}\n\n"
        f"📌 Chủ đề: {topic}\n\n"
        f"📝 Nội dung Gemini:\n{content}\n\n"
        f"🖼 Link ảnh:\n{photo_text or '(Chưa có link ảnh)'}"
    )
    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(
                "✅ Duyệt & Đăng ngay", callback_data=f"approve:{row_idx}"
            ),
            InlineKeyboardButton(
                "❌ Từ chối", callback_data=f"reject:{row_idx}"
            ),
        ]]
    )
    # Không dùng Markdown để tránh lỗi parse khi AI tạo ký tự đặc biệt.
    await bot.send_message(
        chat_id=expected_chat_id(),
        text=message_text[:4000],
        reply_markup=keyboard,
    )


# -------------------- JOB HÀNG ĐỢI --------------------

def find_next_pending_row(sheet) -> tuple[int, dict[str, Any]] | None:
    records = sheet.get_all_values()
    for row_idx, row in enumerate(records[1:], start=2):
        topic = safe_cell(row, COL_TOPIC)
        status = safe_cell(row, COL_STATUS)
        if topic and status in ("", STATUS_PENDING):
            item = get_row_data(sheet, row_idx)
            return row_idx, item
    return None


def daily_job():
    """Tạo nội dung và gửi duyệt cho tối đa một bài mỗi lần chạy."""
    logger.info("Bắt đầu quét hàng đợi")
    try:
        sheet = get_sheet()
        pending = find_next_pending_row(sheet)
        if not pending:
            logger.info("Không có bài đang chờ xử lý")
            return

        row_idx, item = pending
        # Đánh dấu trước khi gọi AI để tránh job khác lấy cùng hàng.
        write_status(sheet, row_idx, STATUS_POSTING)
        try:
            content = generate_post_content(item["topic"])
            write_status(sheet, row_idx, STATUS_WAITING, content)
            asyncio.run(
                send_telegram_approval(
                    row_idx, item["topic"], content, item["photos"]
                )
            )
            logger.info("Đã gửi bài dòng %s sang Telegram duyệt", row_idx)
        except Exception:
            logger.exception("Không tạo/gửi được bài dòng %s", row_idx)
            write_status(sheet, row_idx, STATUS_ERROR)
    except Exception:
        logger.exception("daily_job thất bại")


# -------------------- DỊCH VỤ NỀN --------------------

def telegram_thread_main(stop_event: threading.Event):
    """Chạy Telegram polling trong event loop riêng, không chặn Streamlit."""
    async def runner():
        app: Application = ApplicationBuilder().token(
            telegram_bot_token()
        ).build()
        app.add_handler(CallbackQueryHandler(handle_callback))

        await app.initialize()
        await app.start()
        if app.updater is None:
            raise RuntimeError("Telegram updater chưa được khởi tạo.")
        await app.updater.start_polling(drop_pending_updates=False)
        logger.info("Telegram listener đã khởi chạy")

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
        logger.exception("Telegram listener dừng do lỗi")


@st.cache_resource
def start_background_services():
    """
    Streamlit cache_resource giúp tránh khởi tạo lại service trên mỗi lần rerun
    trong cùng một process. Với nhiều worker/replica, hãy chạy worker riêng.
    """
    scheduler = BackgroundScheduler(timezone=TIMEZONE)
    scheduler.add_job(
        daily_job,
        trigger="cron",
        hour=9,
        minute=0,
        id="daily_fanpage_post",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    scheduler.start()

    stop_event = threading.Event()
    bot_thread = threading.Thread(
        target=telegram_thread_main,
        args=(stop_event,),
        name="telegram-listener",
        daemon=True,
    )
    bot_thread.start()
    return scheduler, stop_event, bot_thread


# -------------------- DASHBOARD --------------------

st.set_page_config(
    page_title="Auto Post Fanpage",
    page_icon="🚀",
    layout="wide",
)
st.title("🚀 Hệ thống tự động soạn & đăng bài Fanpage")
st.caption(
    "Dashboard xem chủ đề, nội dung AI, trạng thái và 4 ảnh trước khi duyệt."
)

with st.expander("⚙️ Trạng thái cấu hình", expanded=False):
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
        st.info(
            "Thêm các giá trị còn thiếu vào Streamlit Cloud → Settings → Secrets "
            "hoặc file .streamlit/secrets.toml."
        )
    else:
        st.success("Đã tìm thấy các khóa cấu hình bắt buộc.")

try:
    scheduler, telegram_stop_event, telegram_thread = start_background_services()
    st.success(
        "Scheduler đã khởi động: 09:00 mỗi ngày (Asia/Ho_Chi_Minh). "
        "Telegram listener chạy trong luồng nền của ứng dụng."
    )
except Exception as exc:
    scheduler = None
    st.error(f"Không thể khởi động dịch vụ nền: {exc}")

top1, top2, top3 = st.columns(3)
top1.metric("Giờ hệ thống", datetime.now(TIMEZONE).strftime("%H:%M:%S"))
top2.metric("Ngày", datetime.now(TIMEZONE).strftime("%d/%m/%Y"))
top3.metric(
    "Telegram listener",
    "Đang chạy" if "telegram_thread" in locals() and telegram_thread.is_alive()
    else "Chưa xác nhận",
)

left, right = st.columns([1, 1])
with left:
    st.subheader("🧪 Điều khiển")
    if st.button("▶️ Chạy quy trình ngay", type="primary", use_container_width=True):
        with st.spinner("Đang tìm bài, tạo nội dung và gửi Telegram..."):
            daily_job()
        st.success("Đã thực thi job. Hãy kiểm tra trạng thái bên dưới và Telegram.")
        st.rerun()

    if st.button("🔄 Tải lại dữ liệu", use_container_width=True):
        st.rerun()

with right:
    st.subheader("🕘 Lịch tự động")
    st.write("Mỗi ngày lúc **09:00**, hệ thống xử lý tối đa một bài trong hàng đợi.")
    st.write(
        "Bài được gửi sang Telegram ở trạng thái **Chờ duyệt Telegram**. "
        "Chỉ khi bấm duyệt, hệ thống mới gọi Facebook API."
    )

st.divider()
st.subheader("📋 Dữ liệu Google Sheets")

try:
    sheet = get_sheet()
    all_values = sheet.get_all_values()
    if not all_values:
        st.warning("Google Sheets chưa có dữ liệu.")
    else:
        headers = all_values[0]
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
            import pandas as pd
            df = pd.DataFrame(data_rows)
            status_options = ["Tất cả"] + sorted({
                str(x) for x in df["Trạng thái"].tolist() if str(x).strip()
            })
            selected_status = st.selectbox("Lọc theo trạng thái", status_options)
            filtered = df if selected_status == "Tất cả" else df[
                df["Trạng thái"] == selected_status
            ]
            st.dataframe(
                filtered[["Dòng", "Chủ đề", "Trạng thái", "Ngày đăng"]],
                use_container_width=True,
                hide_index=True,
            )

            row_options = {
                f"Dòng {int(row['Dòng'])} — {row['Chủ đề'] or '(Không có chủ đề)'}":
                int(row["Dòng"])
                for _, row in filtered.iterrows()
            }
            if row_options:
                selected_label = st.selectbox(
                    "Chọn bài để xem toàn bộ nội dung và ảnh",
                    list(row_options.keys()),
                )
                selected_row_idx = row_options[selected_label]
                selected = get_row_data(sheet, selected_row_idx)

                st.markdown(f"### 📝 {selected['topic'] or 'Chưa có chủ đề'}")
                st.caption(
                    f"Dòng {selected_row_idx} · Trạng thái: "
                    f"{selected['status'] or 'Chưa xử lý'}"
                )

                st.markdown("#### Nội dung bài đăng")
                if selected["content"]:
                    st.text_area(
                        "Nội dung đầy đủ",
                        value=selected["content"],
                        height=300,
                        key=f"content_preview_{selected_row_idx}",
                    )
                    st.button(
                        "📋 Sao chép nội dung",
                        key=f"copy_placeholder_{selected_row_idx}",
                        help="Chọn nội dung trong ô phía trên rồi sao chép bằng Ctrl+C.",
                        disabled=True,
                    )
                else:
                    st.info("Bài này chưa có nội dung AI. Chạy quy trình để tạo nội dung.")

                st.markdown("#### 🖼️ 4 ảnh của bài viết")
                image_cols = st.columns(4)
                for i, (col, url) in enumerate(
                    zip(image_cols, selected["photos"]), start=1
                ):
                    with col:
                        st.markdown(f"**Ảnh {i}**")
                        if url:
                            try:
                                st.image(url, use_container_width=True)
                                st.caption(url)
                            except Exception:
                                st.warning("Không thể hiển thị ảnh này.")
                                st.markdown(f"[Mở link ảnh]({url})")
                        else:
                            st.info("Chưa có URL ảnh")

                if st.button(
                    "📨 Gửi lại nội dung này sang Telegram",
                    key=f"resend_{selected_row_idx}",
                ):
                    if not selected["topic"] or not selected["content"]:
                        st.error("Bài cần có chủ đề và nội dung AI trước khi gửi.")
                    elif selected["status"] != STATUS_WAITING:
                        st.warning(
                            "Chỉ gửi lại bài đang ở trạng thái Chờ duyệt Telegram "
                            "để tránh tạo yêu cầu duyệt ngoài luồng."
                        )
                    else:
                        try:
                            asyncio.run(send_telegram_approval(
                                selected_row_idx,
                                selected["topic"],
                                selected["content"],
                                selected["photos"],
                            ))
                            st.success("Đã gửi lại yêu cầu duyệt qua Telegram.")
                        except Exception as exc:
                            st.error(f"Gửi Telegram thất bại: {exc}")
        else:
            st.info("Chưa có hàng dữ liệu nào ngoài dòng tiêu đề.")
except Exception as exc:
    st.error(f"Không đọc được Google Sheets: {exc}")
    st.info(
        "Kiểm tra SHEET_ID, quyền chia sẻ bảng tính cho service account "
        "và cấu trúc cột A-J."
    )

st.divider()
st.caption(
    "Lưu ý vận hành: Streamlit Cloud có thể khởi động lại/ngủ theo chính sách dịch vụ. "
    "Đối với hệ thống cần chạy 24/7, nên tách Scheduler và Telegram Bot thành worker "
    "riêng hoặc dùng webhook/worker trên dịch vụ luôn hoạt động. Không triển khai nhiều "
    "replica cùng chạy job nếu chưa có cơ chế khóa phân tán."
)
'''
path = Path("/mnt/data/auto_post_fanpage_upgraded.py")
path.write_text(code, encoding="utf-8")

requirements = """streamlit>=1.37
gspread>=6.1
google-auth>=2.30
google-genai>=1.0
pytz>=2024.1
requests>=2.32
APScheduler>=3.10,<4
python-telegram-bot>=21.0,<23
pandas>=2.2
"""
req_path = Path("/mnt/data/requirements_auto_post.txt")
req_path.write_text(requirements, encoding="utf-8")

print(f"Created: {path} ({path.stat().st_size} bytes)")
print(f"Created: {req_path} ({req_path.stat().st_size} bytes)")
