import os
import json
import io
import time
import re
from datetime import datetime, timezone, timedelta
import requests
from PIL import Image
from google import genai
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# قراءة الإعدادات من متغيرات البيئة وتنظيف المسافات والسطور الجديدة
raw_pages_config = os.environ["PAGES_CONFIG"].replace("\r", "").replace("\n", "")
PAGES_CONFIG = json.loads(raw_pages_config)

# قاموس التوكنات: يدعم { "اسم_الحساب": { "اسم_التبويب_أو_PageID": "التوكن" } } أو { "اسم_الحساب": "التوكن" }
raw_tokens_config = os.environ.get("TOKENS_CONFIG", "{}").replace("\r", "").replace("\n", "").strip()
TOKENS_CONFIG = json.loads(raw_tokens_config) if raw_tokens_config else {}

PENDING_FOLDER_ID = os.environ["PENDING_FOLDER_ID"].strip()
POSTED_FOLDER_ID = os.environ["POSTED_FOLDER_ID"].strip()
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"].strip()
creds_dict = json.loads(os.environ["GDRIVE_CREDENTIALS"])
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"].strip()

# تهيئة عميل Gemini API
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# توقيت الجزائر (UTC+1) والفاصل الزمني لكل صفحة (ساعتان و 5 دقائق = 125 دقيقة)
ALGERIA_TZ = timezone(timedelta(hours=1))
INTERVAL_MINUTES = 125

# البرومت الافتراضي المخصص حسب طلبك
DEFAULT_PROMPT = (
    "اكتب منشورا كتابي على صفحة فايسبوك باللغة الانجليزية وباستعمال الإموجي حول محتوى الصورة.\n"
    "بدون مقترحات في الاول والأخير\n"
    "بدون طلب حفظ او مشاركة المنشور او سؤال تفاعلي في اخر المنشور.\n"
    "بدون استعمال الهاشتاغ في الاخير.\n"
    "بدون ذكر المصادر\n"
    "اكتب كنص عادي Plain Text بدون استخدام أي علامات تنسيق مثل ** أو #."
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
    for idx, row in enumerate(rows, start=2):
        image_name = row[0].strip() if len(row) > 0 else ""
        caption = row[1].strip() if len(row) > 1 else ""
        status = row[2].strip().lower() if len(row) > 2 else ""
        custom_prompt = row[4].strip() if len(row) > 4 else ""
        account_name = row[5].strip() if len(row) > 5 else ""

        if image_name and status != "posted":
            return idx, image_name, caption, custom_prompt, account_name
    return None, None, None, None, None


def find_image_in_drive(filename):
    base_name = os.path.splitext(filename.strip())[0]
    safe_base = base_name.replace("'", "\\'")

    possible_names = [
        f"name = '{safe_base}.webp'",
        f"name = '{safe_base}.jpg'",
        f"name = '{safe_base}.jpeg'",
        f"name = '{safe_base}.png'",
        f"name = '{safe_base}'"
    ]
    names_query = " or ".join(possible_names)
    query = f"'{PENDING_FOLDER_ID}' in parents and ({names_query}) and trashed = false"

    results = drive_service.files().list(
        q=query,
        pageSize=1,
        fields="files(id, name)"
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


def clean_facebook_caption(text):
    """إزالة علامات تنسيق Markdown مثل ** و ## ليظهر النص نظيفاً على فيسبوك"""
    text = text.replace("**", "")
    text = re.sub(r'^\s*#+\s*', '', text, flags=re.MULTILINE)
    text = text.replace("\\", "")
    return text.strip()


def generate_caption_with_gemini(image_stream, prompt_text):
    """توليد منشور فيسبوك مع إعادة المحاولة تلقائياً وتنظيف علامات التنسيق"""
    image_stream.seek(0)
    img = Image.open(image_stream)

    models_to_try = ["gemini-3.8-flash", "gemini-3.8-flash-lite", "gemini-3.0-flash"]

    for model_name in models_to_try:
        for attempt in range(2):
            try:
                response = gemini_client.models.generate_content(
                    model=model_name,
                    contents=[img, prompt_text]
                )
                image_stream.seek(0)
                return clean_facebook_caption(response.text)
            except Exception:
                print(f"تنبيه: النموذج {model_name} مشغول (محاولة {attempt + 1})، إعادة المحاولة بعد 5 ثوانٍ...")
                time.sleep(5)

    raise RuntimeError("جميع نماذج Gemini مشغولة حالياً، سيتم إعادة المحاولة في الدورة القادمة.")


def prepare_image_for_fb(image_stream, filename):
    image_stream.seek(0)
    if filename.lower().endswith(".webp"):
        img = Image.open(image_stream).convert("RGB")
        output_stream = io.BytesIO()
        img.save(output_stream, format="JPEG", quality=95)
        output_stream.seek(0)
        new_filename = os.path.splitext(filename)[0] + ".jpg"
        return output_stream, new_filename, "image/jpeg"

    mime_type = "image/png" if filename.lower().endswith(".png") else "image/jpeg"
    return image_stream, filename, mime_type


def post_to_facebook(page_id, access_token, image_stream, filename, caption):
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
        print(f"تفاصيل خطأ فيسبوك للصفحة {page_id}:", response.text)
    response.raise_for_status()
    return response.json()


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
    """جلب التوكن المناسب للحساب والصفحة من TOKENS_CONFIG أو PAGES_CONFIG"""
    if not account_name:
        return default_token

    acc_clean = account_name.strip().lower()
    tab_clean = tab_name.strip().lower()
    pid_clean = page_id.strip().lower()

    # البحث في قاموس TOKENS_CONFIG
    for k, v in TOKENS_CONFIG.items():
        if k.strip().lower() == acc_clean:
            # إذا كانت قيمة الحساب قاموساً يحتوي على توكنات لكل صفحة
            if isinstance(v, dict):
                for page_key, tok in v.items():
                    pk_clean = str(page_key).strip().lower()
                    if pk_clean in (tab_clean, pid_clean) and str(tok).strip():
                        print(f"[{tab_name}] استخدام توكن الحساب '{k.strip()}' المخصص لهذه الصفحة.")
                        return str(tok).strip().replace("\n", "").replace("\r", "")
                print(f"[{tab_name}] تحذير: الحساب '{k.strip()}' لا يملك توكناً مسجلاً لهذه الصفحة، سيتم استخدام الافتراضي.")
                return default_token
            # إذا كانت قيمة الحساب نصاً مباشراً (توكن عام للحساب)
            elif isinstance(v, str) and v.strip():
                print(f"[{tab_name}] استخدام التوكن العام للحساب '{k.strip()}'.")
                return v.strip().replace("\n", "").replace("\r", "")

    # البحث في PAGES_CONFIG إذا كان الاسم مسجلاً هناك
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
            range=f"'{tab_name}'!A2:F"
        ).execute()
        rows = result.get('values', [])

        if not is_interval_passed(rows, tab_name):
            return

        row_num, image_name, caption, custom_prompt, account_name = get_next_scheduled_row(rows)
        if not image_name:
            print(f"[{tab_name}] لا توجد منشورات جديدة في هذا التبويب.")
            return

        active_access_token = resolve_access_token(account_name, tab_name, page_id, default_access_token)

        print(f"[{tab_name}] الصف {row_num}: البحث عن '{image_name}'...")
        image_file = find_image_in_drive(image_name)
        if not image_file:
            print(f"[{tab_name}] تحذير: الصورة '{image_name}' غير موجودة في مجلد Pending_Posts!")
            return

        img_data = download_file(image_file['id'])

        if not caption:
            active_prompt = custom_prompt or page_default_prompt or DEFAULT_PROMPT
            prompt_source = "العمود E" if custom_prompt else ("إعدادات الصفحة" if page_default_prompt else "الافتراضي العام")
            print(f"[{tab_name}] توليد النص عبر Gemini (المصدر: {prompt_source})...")
            caption = generate_caption_with_gemini(img_data, active_prompt)
        else:
            caption = clean_facebook_caption(caption)

        fb_res = post_to_facebook(page_id, active_access_token, img_data, image_file['name'], caption)
        print(f"[{tab_name}] تم النشر بنجاح! رقم المنشور: {fb_res.get('id')}")

        move_to_archive(image_file['id'])
        update_sheet_row(tab_name, row_num, caption)
        print(f"[{tab_name}] تم أرشفة الصورة وحفظ النص وتحديث الجدول.")

    except Exception as e:
        print(f"[{tab_name}] حدث خطأ أثناء معالجة هذه الصفحة: {e}")


if __name__ == "__main__":
    for page in PAGES_CONFIG:
        process_page(page)
