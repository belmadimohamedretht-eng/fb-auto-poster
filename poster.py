import os
import json
import io
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# قراءة المفاتيح السرية من إعدادات GitHub
FB_PAGE_ID = os.environ["FB_PAGE_ID"]
FB_ACCESS_TOKEN = os.environ["FB_ACCESS_TOKEN"]
PENDING_FOLDER_ID = os.environ["PENDING_FOLDER_ID"]
POSTED_FOLDER_ID = os.environ["POSTED_FOLDER_ID"]
creds_dict = json.loads(os.environ["GDRIVE_CREDENTIALS"])

# الاتصال بحساب Google Drive
SCOPES = ['https://www.googleapis.com/auth/drive']
creds = service_account.Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
drive_service = build('drive', 'v3', credentials=creds)

def get_oldest_image():
    query = f"'{PENDING_FOLDER_ID}' in parents and mimeType contains 'image/' and trashed = false"
    results = drive_service.files().list(
        q=query,
        orderBy="createdTime asc",
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

if __name__ == "__main__":
    image_file = get_oldest_image()
    if not image_file:
        print("مجلد الانتظار فارغ، لا توجد صور للنشر حالياً.")
        exit(0)

    file_id = image_file['id']
    file_name = image_file['name']
    # تحويل اسم الملف إلى نص للمنشور (مع حذف صيغة الصورة مثل .jpg)
    caption = os.path.splitext(file_name)[0].replace("_", " ").replace("-", " ")

    print(f"جاري تحميل ونشر الصورة: {file_name}")
    img_data = download_file(file_id)
    fb_res = post_to_facebook(img_data, file_name, caption)
    print(f"تم النشر بنجاح! رقم المنشور: {fb_res.get('id')}")
    
    move_to_archive(file_id)
    print("تم نقل الصورة إلى مجلد الأرشيف بنجاح.")
