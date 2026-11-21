import os
import json
import io
import time
import re
import tempfile
from datetime import datetime, timezone, timedelta
import requests
from PIL import Image
from google import genai
from google.genai import types
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

# قراءة الإعدادات من متغيرات البيئة وتنظيف المسافات والسطور الجديدة
raw_pages_config = os.environ["PAGES_CONFIG"].replace("\r", "").replace("\n", "")
PAGES_CONFIG = json.loads(raw_pages_config)

raw_tokens_config = os.environ.get("TOKENS_CONFIG", "{}").replace("\r", "").replace("\n", "").strip()
TOKENS_CONFIG = json.loads(raw_tokens_config) if raw_tokens_config else {}

PENDING_FOLDER_ID = os.environ["PENDING_FOLDER_ID"].strip()
POSTED_FOLDER_ID = os.environ["POSTED_FOLDER_ID"].strip()
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"].strip()
creds_dict = json.loads(os.environ["GDRIVE_CREDENTIALS"])
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"].strip()

# تهيئة عميل Google AI (Gemini)
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# توقيت الجزائر (UTC+1) والفاصل الزمني لكل صفحة (125 دقيقة)
ALGERIA_TZ = timezone(timedelta(hours=1))
INTERVAL_MINUTES = 125

VIDEO_EXTENSIONS = (".mp4", ".mov", ".mkv", ".avi", ".webm")

# البرومت الافتراضي لنص المنشور للصور
DEFAULT_PROMPT = (
    "اكتب منشورا كتابي على صفحة فايسبوك باللغة الانجليزية وباستعمال الإموجي حول محتوى الصورة.\n"
    "بدون مقترحات في الاول والأخير\n"
    "بدون طلب حفظ او مشاركة المنشور او سؤال تفاعلي في اخر المنشور.\n"
    "بدون استعمال الهاشتاغ في الاخير.\n"
    "بدون ذكر المصادر\n"
    "اكتب كنص عادي Plain Text بدون استخدام أي علامات تنسيق مثل ** أو #."
)

# البرومت الافتراضي للفيديوهات القصيرة Reels (عنوان قصير فقط باللغة الإنجليزية)
DEFAULT_VIDEO_PROMPT = (
    "Write ONLY a short, catchy, single-line title in English with 1 or 2 relevant emojis describing the content of this video.\n"
    "Keep it concise (maximum 10 words).\n"
    "Do NOT write a full description, paragraphs, intro, outro, questions, calls to action, or hashtags.\n"
    "Output plain text only without any markdown symbols like ** or #."
)

# الاتصال بـ Google Drive و Google Sheets
SCOPES = [
    'https://www.googleapis.com/auth/drive',
    'https://www.googleapis.com/auth/spreadsheets'
]
creds = service_account.Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
drive_service = build('drive', 'v3', credentials=creds)
sheets_service = build('sheets', 'v4', credentials=creds)


def is_interval_passed(rows, tab_name):
    """التحقق من مرور 125 دقيقة على آخر منشور في هذه الصفحة تحديداً"""
    now_algeria = datetime.now(ALGERIA_TZ).replace(tzinfo=None)

    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        print(f"[{tab_name}] تشغيل يدوي: تجاوز شرط الانتظار.")
        return True

    last_posted_time = None
    for row in rows:
        if len(row) > 3 and row[3].strip():
            try:
                t = datetime.strptime(row[3].strip(), "%Y-%m-%d %H:%M")
                if last_posted_time is None or t > last_posted_time:
                    last_posted_time = t
            except ValueError:
                continue

    if last_posted_time is None:
        return True

    elapsed = (now_algeria - last_posted_time).total_seconds() / 60.0
    if elapsed >= (INTERVAL_MINUTES - 2):
        return True
    else:
        remaining = int(INTERVAL_MINUTES - elapsed)
        print(f"[{tab_name}] مضى {int(elapsed)} دقيقة فقط. المتبقي: {remaining} دقيقة.")
        return False


def get_next_scheduled_row(rows):
    """قراءة الصف التالي من الأعمدة A إلى G"""
    for idx, row in enumerate(rows, start=2):
        media_name = row[0].strip() if len(row) > 0 else ""
        caption = row[1].strip() if len(row) > 1 else ""
        status = row[2].strip().lower() if len(row) > 2 else ""
        custom_prompt = row[4].strip() if len(row) > 4 else ""
        account_name = row[5].strip() if len(row) > 5 else ""
        image_edit_prompt = row[6].strip() if len(row) > 6 else ""

        if media_name and status != "posted":
            return idx, media_name, caption, custom_prompt, account_name, image_edit_prompt
    return None, None, None, None, None, None


def recreate_image_with_gemini(source_image_stream, image_prompt, aspect_ratio="4:5"):
    """إنشاء صورة جديدة اعتماداً على الصورة المرفوعة + البرومت عبر Google AI API المدفوع"""
    source_image_stream.seek(0)
    pil_img = Image.open(source_image_stream)

    image_models = ["gemini-2.5-flash-image", "gemini-3-pro-image-preview"]

    last_error = None
    for model_name in image_models:
        for attempt in range(2):
            try:
                response = gemini_client.models.generate_content(
                    model=model_name,
                    contents=[pil_img, image_prompt],
                    config=types.GenerateContentConfig(
                        response_modalities=["IMAGE"],
                        image_config=types.ImageConfig(
                            aspect_ratio=aspect_ratio
                        )
                    )
                )

                if not response.candidates:
                    raise RuntimeError("لم يُرجع النموذج أي مرشحات (candidates) في الاستجابة.")

                cand = response.candidates[0]
                if not cand.content or not cand.content.parts:
                    raise RuntimeError(f"لم تُرجع الصورة (السبب: {cand.finish_reason})")

                for part in cand.content.parts:
                    if part.inline_data and part.inline_data.data:
                        out_stream = io.BytesIO(part.inline_data.data)
                        out_stream.seek(0)
                        new_filename = f"ai_recreated_{datetime.now(ALGERIA_TZ).strftime('%Y%m%d_%H%M%S')}.jpg"
                        source_image_stream.seek(0)
                        return out_stream, new_filename

                raise RuntimeError("النموذج لم يُرجع بيانات صورة في الاستجابة.")
            except Exception as e:
                last_error = e
                print(f"تنبيه أثناء إعادة صياغة الصورة عبر {model_name} (محاولة {attempt + 1}): {e}")
                time.sleep(5)

    raise RuntimeError(f"فشل إعادة صياغة الصورة عبر Google AI: {last_error}")


def upload_image_to_archive(image_stream, filename):
    """حفظ نسخة من الصورة الجديدة المولدة في مجلد الأرشيف Posted_Posts"""
    image_stream.seek(0)
    file_metadata = {
        "name": filename,
        "parents": [POSTED_FOLDER_ID]
    }
    media = MediaIoBaseUpload(image_stream, mimetype="image/jpeg", resumable=False)
    drive_service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id"
    ).execute()
    image_stream.seek(0)


def find_media_in_folder(filename, folder_id):
    """البحث عن الصورة أو الفيديو في مجلد محدد"""
    base_name = os.path.splitext(filename.strip())[0]
    safe_base = base_name.replace("'", "\\'")

    possible_names = [
        f"name = '{safe_base}.webp'",
        f"name = '{safe_base}.jpg'",
        f"name = '{safe_base}.jpeg'",
        f"name = '{safe_base}.png'",
        f"name = '{safe_base}.mp4'",
        f"name = '{safe_base}.mov'",
        f"name = '{safe_base}.mkv'",
        f"name = '{safe_base}'"
    ]
    names_query = " or ".join(possible_names)
    query = f"'{folder_id}' in parents and ({names_query}) and trashed = false"

    results = drive_service.files().list(
        q=query,
        pageSize=1,
        fields="files(id, name, mimeType)"
    ).execute()
    files = results.get('files', [])
    return files[0] if files else None


def download_file(file_id):
    request = drive_service.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    fh.seek(0)
    return fh


def is_video_file(file_info):
    name = file_info.get("name", "").lower()
    mime = file_info.get("mimeType", "").lower()
    return name.endswith(VIDEO_EXTENSIONS) or mime.startswith("video/")


def clean_facebook_caption(text):
    """إزالة علامات تنسيق Markdown مثل ** و ## ليظهر النص نظيفاً على فيسبوك"""
    text = text.replace("**", "")
    text = re.sub(r'^\s*#+\s*', '', text, flags=re.MULTILINE)
    text = text.replace("\\", "")
    return text.strip()


def generate_caption_for_image(image_stream, prompt_text):
    """توليد منشور لصورة عبر Gemini"""
    image_stream.seek(0)
    img = Image.open(image_stream)

    models_to_try = ["gemini-3.8-flash", "gemini-3.5-flash-lite"]

    for model_name in models_to_try:
        for attempt in range(2):
            try:
                response = gemini_client.models.generate_content(
                    model=model_name,
                    contents=[img, prompt_text]
                )
                image_stream.seek(0)
                return clean_facebook_caption(response.text)
            except Exception as e:
                print(f"تنبيه: النموذج {model_name} مشغول (محاولة {attempt + 1}): {e}")
                time.sleep(5)

    raise RuntimeError("جميع نماذج Gemini مشغولة حالياً، سيتم إعادة المحاولة في الدورة القادمة.")


def generate_caption_for_video(video_stream, filename, prompt_text):
    """رفع الفيديو مؤقتاً إلى Gemini وتحليله لتوليد عنوان قصير بالإنجليزية ثم حذفه"""
    video_stream.seek(0)
    ext = os.path.splitext(filename)[1] or ".mp4"
    tmp_path = None
    uploaded_file = None

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            tmp.write(video_stream.read())
            tmp_path = tmp.name

        uploaded_file = gemini_client.files.upload(file=tmp_path)

        while uploaded_file.state.name == "PROCESSING":
            time.sleep(3)
            uploaded_file = gemini_client.files.get(name=uploaded_file.name)

        if uploaded_file.state.name == "FAILED":
            raise RuntimeError("فشلت معالجة الفيديو في Gemini.")

        models_to_try = ["gemini-3.8-flash", "gemini-3.5-flash-lite"]
        for model_name in models_to_try:
            for attempt in range(2):
                try:
                    response = gemini_client.models.generate_content(
                        model=model_name,
                        contents=[uploaded_file, prompt_text]
                    )
                    video_stream.seek(0)
                    title = clean_facebook_caption(response.text).splitlines()[0].strip()
                    return title
                except Exception as e:
                    print(f"تنبيه: النموذج {model_name} مشغول للفيديو (محاولة {attempt + 1}): {e}")
                    time.sleep(5)

        raise RuntimeError("جميع نماذج Gemini مشغولة حالياً لمعالجة الفيديو.")
    finally:
        video_stream.seek(0)
        if uploaded_file:
            try:
                gemini_client.files.delete(name=uploaded_file.name)
            except Exception:
                pass
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


def prepare_image_for_fb(image_stream, filename):
    image_stream.seek(0)
    img = Image.open(image_stream).convert("RGB")
    output_stream = io.BytesIO()
    img.save(output_stream, format="JPEG", quality=95)
    output_stream.seek(0)
    new_filename = os.path.splitext(filename)[0] + ".jpg"
    return output_stream, new_filename, "image/jpeg"


def post_photo_to_facebook(page_id, access_token, image_stream, filename, caption):
    ready_stream, ready_name, mime_type = prepare_image_for_fb(image_stream, filename)
    url = f"https://graph.facebook.com/v26.0/{page_id}/photos"
    payload = {
        "message": caption,
        "published": "true",
        "access_token": access_token
    }
    files = {
        "source": (ready_name, ready_stream, mime_type)
    }
    response = requests.post(url, data=payload, files=files)
    if not response.ok:
        print(f"تفاصيل خطأ نشر الصورة للصفحة {page_id}:", response.text)
    response.raise_for_status()
    return response.json()


def post_reel_to_facebook(page_id, access_token, video_stream, caption):
    """نشر الفيديو القصير كـ Facebook Reel عبر 3 مراحل رسمية"""
    video_stream.seek(0)
    video_bytes = video_stream.read()
    file_size = len(video_bytes)

    base_url = f"https://graph.facebook.com/v26.0/{page_id}/video_reels"

    init_res = requests.post(base_url, data={
        "upload_phase": "start",
        "access_token": access_token
    })
    if not init_res.ok:
        print(f"خطأ تهيئة رفع Reel للصفحة {page_id}:", init_res.text)
    init_res.raise_for_status()
    init_data = init_res.json()

    video_id = init_data["video_id"]
    upload_url = init_data["upload_url"]

    upload_headers = {
        "Authorization": f"OAuth {access_token}",
        "offset": "0",
        "file_size": str(file_size),
        "Content-Type": "application/octet-stream"
    }
    up_res = requests.post(upload_url, headers=upload_headers, data=video_bytes)
    if not up_res.ok:
        print(f"خطأ رفع ملف الفيديو Reel للصفحة {page_id}:", up_res.text)
    up_res.raise_for_status()

    finish_res = requests.post(base_url, data={
        "upload_phase": "finish",
        "video_id": video_id,
        "video_state": "PUBLISHED",
        "description": caption,
        "access_token": access_token
    })
    if not finish_res.ok:
        print(f"خطأ نشر Reel النهائي للصفحة {page_id}:", finish_res.text)
    finish_res.raise_for_status()

    return {"id": video_id, "details": finish_res.json()}


def move_to_archive(file_id):
    drive_service.files().update(
        fileId=file_id,
        addParents=POSTED_FOLDER_ID,
        removeParents=PENDING_FOLDER_ID,
        fields='id, parents'
    ).execute()


def update_sheet_row(tab_name, row_number, caption):
    now_str = datetime.now(ALGERIA_TZ).strftime("%Y-%m-%d %H:%M")
    sheets_service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{tab_name}'!B{row_number}:D{row_number}",
        valueInputOption="USER_ENTERED",
        body={"values": [[caption, "Posted", now_str]]}
    ).execute()


def resolve_access_token(account_name, tab_name, page_id, default_token):
    if not account_name:
        return default_token

    acc_clean = account_name.strip().lower()
    tab_clean = tab_name.strip().lower()
    pid_clean = page_id.strip().lower()

    for k, v in TOKENS_CONFIG.items():
        if k.strip().lower() == acc_clean:
            if isinstance(v, dict):
                for page_key, tok in v.items():
                    pk_clean = str(page_key).strip().lower()
                    if pk_clean in (tab_clean, pid_clean) and str(tok).strip():
                        print(f"[{tab_name}] استخدام توكن الحساب '{k.strip()}' المخصص لهذه الصفحة.")
                        return str(tok).strip().replace("\n", "").replace("\r", "")
                print(f"[{tab_name}] تحذير: الحساب '{k.strip()}' لا يملك توكناً مسجلاً لهذه الصفحة، سيتم استخدام الافتراضي.")
                return default_token
            elif isinstance(v, str) and v.strip():
                print(f"[{tab_name}] استخدام التوكن العام للحساب '{k.strip()}'.")
                return v.strip().replace("\n", "").replace("\r", "")

    for p in PAGES_CONFIG:
        if p.get("name", "").strip().lower() == acc_clean and p.get("access_token", "").strip():
            print(f"[{tab_name}] استخدام توكن الحساب '{p.get('name').strip()}' من PAGES_CONFIG.")
            return p["access_token"].strip().replace("\n", "").replace("\r", "")

    print(f"[{tab_name}] تحذير: لم يتم العثور على الحساب '{account_name}'، سيتم استخدام التوكن الافتراضي.")
    return default_token


def process_page(page_cfg):
    tab_name = page_cfg["sheet_tab"].strip()
    page_id = page_cfg["page_id"].strip()
    default_access_token = page_cfg["access_token"].strip().replace("\n", "").replace("\r", "")
    page_default_prompt = page_cfg.get("prompt", "").strip()

    print(f"\n--- فحص صفحة: {tab_name} ---")
    try:
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=f"'{tab_name}'!A2:G"
        ).execute()
        rows = result.get('values', [])

        if not is_interval_passed(rows, tab_name):
            return

        row_num, media_name, caption, custom_prompt, account_name, row_image_prompt = get_next_scheduled_row(rows)
        if not media_name:
            print(f"[{tab_name}] لا توجد منشورات جديدة في هذا التبويب.")
            return

        active_access_token = resolve_access_token(account_name, tab_name, page_id, default_access_token)

        print(f"[{tab_name}] الصف {row_num}: البحث عن '{media_name}' في Drive...")
        media_file = find_media_in_folder(media_name, PENDING_FOLDER_ID)

        if not media_file:
            archived_file = find_media_in_folder(media_name, POSTED_FOLDER_ID)
            if archived_file:
                print(f"[{tab_name}] الملف '{media_name}' موجود مسبقاً في الأرشيف! جاري تحديث الشيت إلى Posted...")
                update_sheet_row(tab_name, row_num, caption or "(تم النشر والأرشفة مسبقاً)")
                return
            print(f"[{tab_name}] تحذير: الملف '{media_name}' غير موجود في مجلد Pending_Posts!")
            return

        media_data = download_file(media_file['id'])
        is_video = is_video_file(media_file)
        active_filename = media_file['name']
        was_recreated = False

        # إعادة صياغة الصورة فقط إذا وُجد برومت في العمود G
        if not is_video and row_image_prompt:
            print(f"[{tab_name}] إعادة صياغة الصورة عبر Google AI حسب برومت العمود G...")
            media_data, active_filename = recreate_image_with_gemini(
                media_data,
                row_image_prompt,
                aspect_ratio="4:5"
            )
            was_recreated = True
        elif not is_video:
            print(f"[{tab_name}] العمود G فارغ: سيتم نشر الصورة الأصلية كما هي.")

        if not caption:
            if is_video:
                active_prompt = custom_prompt or DEFAULT_VIDEO_PROMPT
                prompt_source = "العمود E" if custom_prompt else "عنوان Reel الافتراضي"
                print(f"[{tab_name}] تحليل الفيديو وتوليد عنوان Reel بالإنجليزية (المصدر: {prompt_source})...")
                caption = generate_caption_for_video(media_data, active_filename, active_prompt)
            else:
                active_prompt = custom_prompt or page_default_prompt or DEFAULT_PROMPT
                prompt_source = "العمود E" if custom_prompt else ("إعدادات الصفحة" if page_default_prompt else "الافتراضي العام")
                print(f"[{tab_name}] توليد نص الصورة عبر Gemini (المصدر: {prompt_source})...")
                caption = generate_caption_for_image(media_data, active_prompt)
        else:
            caption = clean_facebook_caption(caption)

        if is_video:
            print(f"[{tab_name}] جاري رفع ونشر الفيديو كـ Reel بعنوان: {caption}")
            fb_res = post_reel_to_facebook(page_id, active_access_token, media_data, caption)
            print(f"[{tab_name}] تم نشر الـ Reel بنجاح! معرف الفيديو: {fb_res.get('id')}")
        else:
            fb_res = post_photo_to_facebook(page_id, active_access_token, media_data, active_filename, caption)
            print(f"[{tab_name}] تم نشر الصورة بنجاح! رقم المنشور: {fb_res.get('id')}")

        move_to_archive(media_file['id'])
        if was_recreated:
            upload_image_to_archive(media_data, active_filename)

        update_sheet_row(tab_name, row_num, caption)
        print(f"[{tab_name}] تم الأرشفة وحفظ النص وتحديث الجدول.")

    except Exception as e:
        print(f"[{tab_name}] حدث خطأ أثناء معالجة هذه الصفحة: {e}")


if __name__ == "__main__":
    for page in PAGES_CONFIG:
        process_page(page)
