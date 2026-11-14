import os
import json
import io
from datetime import datetime, timezone
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# قراءة المفاتيح السرية من إعدادات GitHub
FB_PAGE_ID = os.environ["FB_PAGE_ID"]
FB_ACCESS_TOKEN = os.environ["FB_ACCESS_TOKEN"]
PENDING_FOLDER_ID = os.environ["PENDING_FOLDER_ID"]
POSTED_FOLDER_ID = os.environ["POSTED_FOLDER_ID"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
creds_dict = json.loads(os.environ["GDRIVE_CREDENTIALS"])

# الاتصال بـ Google Drive و Google Sheets
SCOPES = [
    'https://www.googleapis.com/auth/drive',
    'https://www.googleapis.com/auth/spreadsheets'
]
creds = service_account.Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
drive_service = build('drive', 'v3', credentials=creds)
sheets_service = build('sheets', 'v4', credentials=creds)

def get_next_scheduled_row():
    """جلب أول صف غير منشور من الجدول"""
    result = sheets_service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="Sheet1!A2:C"
    ).execute()
    rows = result.get('values', [])
    
    for idx, row in enumerate(rows, start=2):
        image_name = row[0].strip() if len(row) > 0 else ""
        caption = row[1].strip() if len(row) > 1 else ""
        status = row[2].strip().lower() if len(row) > 2 else ""
        
        if image_name and status != "posted":
            return idx, image_name, caption
    return None, None, None

def find_image_in_drive(filename):
    """البحث عن الصورة بالاسم داخل مجلد الانتظار"""
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

def post_to_facebook(image_stream, filename, caption):
    url = f"https://graph.facebook.com/v26.0/{FB_PAGE_ID}/photos"
    payload = {
        "message": caption,
        "access_token": FB_ACCESS_TOKEN
    }
    files = {
        "source": (filename, image_stream, "image/jpeg")
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
    """تحديث حالة الصف في الجدول وتسجيل وقت النشر"""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    sheets_service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"Sheet1!C{row_number}:D{row_number}",
        valueInputOption="USER_ENTERED",
        body={"values": [["Posted", now_str]]}
    ).execute()

if __name__ == "__main__":
    row_num, image_name, caption = get_next_scheduled_row()
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
