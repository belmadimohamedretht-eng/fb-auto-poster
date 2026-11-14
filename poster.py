import os
import json
import io
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# قراءة المفاتيح السرية
FB_PAGE_ID = os.environ["FB_PAGE_ID"]
FB_ACCESS_TOKEN = os.environ["FB_ACCESS_TOKEN"]
PENDING_FOLDER_ID = os.environ["PENDING_FOLDER_ID"]
POSTED_FOLDER_ID = os.environ["POSTED_FOLDER_ID"]
creds_dict = json.loads(os.environ["GDRIVE_CREDENTIALS"])

# الاتصال بـ Google Drive
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

def get_matching_text_file(base_name):
    safe_txt_name = f"{base_name}.txt".replace("'", "\\'")
    query = f"'{PENDING_FOLDER_ID}' in parents and name = '{safe_txt_name}' and trashed = false"
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

if __name__ == "__main__":
    image_file = get_oldest_image()
    if not image_file:
        print("مجلد الانتظار فارغ، لا توجد صور للنشر حالياً.")
        exit(0)

    file_id = image_file['id']
    file_name = image_file['name']
    base_name = os.path.splitext(file_name)[0]

    # البحث عن ملف نصي بنفس اسم الصورة
    txt_file = get_matching_text_file(base_name)
    if txt_file:
        print(f"تم العثور على ملف نصي مرافق: {txt_file['name']}")
        txt_stream = download_file(txt_file['id'])
        caption = txt_stream.read().decode('utf-8').strip()
    else:
        # في حال عدم وجود ملف نصي، يستخدم اسم الصورة كعنوان للمنشور
        caption = base_name.replace("_", " ").replace("-", " ")

    print(f"جاري نشر الصورة: {file_name}")
    img_data = download_file(file_id)
    fb_res = post_to_facebook(img_data, file_name, caption)
    print(f"تم النشر بنجاح! رقم المنشور: {fb_res.get('id')}")
    
    # نقل الصورة والملف النصي (إن وجد) إلى الأرشيف
    move_to_archive(file_id)
    if txt_file:
        move_to_archive(txt_file['id'])
    print("تم نقل الملفات إلى مجلد الأرشيف بنجاح.")
