import os
import json
import io
from datetime import datetime, timezone, timedelta
import requests
from PIL import Image
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# قراءة المفاتيح السرية
FB_PAGE_ID = os.environ["FB_PAGE_ID"]
FB_ACCESS_TOKEN = os.environ["FB_ACCESS_TOKEN"]
PENDING_FOLDER_ID = os.environ["PENDING_FOLDER_ID"]
POSTED_FOLDER_ID = os.environ["POSTED_FOLDER_ID"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
creds_dict = json.loads(os.environ["GDRIVE_CREDENTIALS"])

# توقيت الجزائر (UTC+1) والفاصل الزمني المطلوب (80 دقيقة = ساعة و 20 دقيقة)
ALGERIA_TZ = timezone(timedelta(hours=1))
INTERVAL_MINUTES = 80

# الاتصال بـ Google Drive و Google Sheets
SCOPES = [
    'https://www.googleapis.com/auth/drive',
    'https://www.googleapis.com/auth/spreadsheets'
]
creds = service_account.Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
drive_service = build('drive', 'v3', credentials=creds)
sheets_service = build('sheets', 'v4', credentials=creds)

def is_interval_passed(rows):
    """التحقق من مرور 80 دقيقة على آخر منشور في أي وقت من اليوم"""
    now_algeria = datetime.now(ALGERIA_TZ).replace(tzinfo=None)
    
    # السماح بالنشر الفوري عند الضغط على زر Run workflow يدوياً
    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        print("تم التشغيل يدوياً: تجاوز شرط الانتظار.")
        return True

    # البحث عن أحدث وقت نشر في العمود D
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
        return True  # أول منشور في الجدول

    elapsed = (now_algeria - last_posted_time).total_seconds() / 60.0
    # هامش أمان دقيقتان لتعويض أي تأخير طفيف في سيرفرات GitHub
    if elapsed >= (INTERVAL_MINUTES - 2):
        return True
    else:
        remaining = int(INTERVAL_MINUTES - elapsed)
        print(f"مضى {int(elapsed)} دقيقة فقط منذ آخر منشور. المتبقي للنشر القادم: {remaining} دقيقة.")
        return False

def get_next_scheduled_row(rows):
    """جلب أول صف غير منشور من الجدول"""
    for idx, row in enumerate(rows, start=2):
        image_name = row[0].strip() if len(row) > 0 else ""
        caption = row[1].strip() if len(row) > 1 else ""
        status = row[2].strip().lower() if len(row) > 2 else ""
        
        if image_name and status != "posted":
            return idx, image_name, caption
    return None, None, None

def find_image_in_drive(filename):
    safe_name = filename.replace("'", "\\'")
    query = f"'{PENDING_FOLDER_ID}' in parents and name = '{safe_name}' and trashed = false"
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

def prepare_image_for_fb(image_stream, filename):
    if filename.lower().endswith(".webp"):
        img = Image.open(image_stream).convert("RGB")
        output_stream = io.BytesIO()
        img.save(output_stream, format="JPEG", quality=95)
        output_stream.seek(0)
        new_filename = os.path.splitext(filename)[0] + ".jpg"
        return output_stream, new_filename, "image/jpeg"
    
    mime_type = "image/png" if filename.lower().endswith(".png") else "image/jpeg"
    return image_stream, filename, mime_type

def post_to_facebook(image_stream, filename, caption):
    ready_stream, ready_name, mime_type = prepare_image_for_fb(image_stream, filename)
    url = f"https://graph.facebook.com/v26.0/{FB_PAGE_ID}/photos"
    payload = {
        "message": caption,
        "access_token": FB_ACCESS_TOKEN
    }
    files = {
        "source": (ready_name, ready_stream, mime_type)
    }
    response = requests.post(url, data=payload, files=files)
    if not response.ok:
        print("تفاصيل خطأ فيسبوك:", response.text)
    response.raise_for_status()
    return response.json()

def move_to_archive(file_id):
    drive_service.files().update(
        fileId=file_id,
        addParents=POSTED_FOLDER_ID,
        removeParents=PENDING_FOLDER_ID,
        fields='id, parents'
    ).execute()

def mark_row_as_posted(row_number):
    """تسجيل حالة النشر ووقت النشر بتوقيت الجزائر في العمودين C و D"""
    now_str = datetime.now(ALGERIA_TZ).strftime("%Y-%m-%d %H:%M")
    sheets_service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"C{row_number}:D{row_number}",
        valueInputOption="USER_ENTERED",
        body={"values": [["Posted", now_str]]}
    ).execute()

if __name__ == "__main__":
    result = sheets_service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="A2:D"
    ).execute()
    rows = result.get('values', [])

    if not is_interval_passed(rows):
        exit(0)

    row_num, image_name, caption = get_next_scheduled_row(rows)
    if not image_name:
        print("لا توجد منشورات جديدة في جدول Google Sheets.")
        exit(0)

    print(f"الصف رقم {row_num}: البحث عن الصورة '{image_name}'...")
    image_file = find_image_in_drive(image_name)
    if not image_file:
        print(f"خطأ: الصورة '{image_name}' غير موجودة في مجلد Pending_Posts!")
        exit(1)

    img_data = download_file(image_file['id'])
    fb_res = post_to_facebook(img_data, image_name, caption)
    print(f"تم النشر بنجاح! رقم المنشور: {fb_res.get('id')}")
    
    move_to_archive(image_file['id'])
    mark_row_as_posted(row_num)
    print("تم نقل الصورة للأرشيف وتحديث حالة الجدول.")
