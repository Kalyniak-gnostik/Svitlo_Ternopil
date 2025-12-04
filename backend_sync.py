import os
import json
import csv
import cv2
import numpy as np
import requests
import re
import time
import logging
from datetime import datetime
from github import Github
from dotenv import load_dotenv

# Налаштування
load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)

# КОНФІГУРАЦІЯ
TESSERACT_PATH = r"C:\Program Files\Tesseract-OCR\tesseract.exe" # Або шлях до вашої папки Tesseract-OCR/tesseract.exe
API_ENDPOINT = "https://api-toe-poweron.inneti.net/api/options"
CSV_FILE = "cherg_bd.csv" # Файл з адресами має лежати поруч

# Підключаємо Tesseract
try:
    import pytesseract
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_PATH
except:
    logger.warning("Tesseract не знайдено, OCR не працюватиме")

# === ЧАСТИНА 1: OCR ТА ГРАФІКИ ===
class ScheduleProcessor:
    def __init__(self):
        self.queues = ["1.1", "1.2", "2.1", "2.2", "3.1", "3.2", "4.1", "4.2", "5.1", "5.2", "6.1", "6.2"]
        self.times = [f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 30)]

    def extract_date(self, img_path):
        try:
            img = cv2.imread(img_path)
            h, w, _ = img.shape
            roi = img[0:int(h*0.20), 0:w]
            text = pytesseract.image_to_string(roi, lang='ukr+eng')
            match = re.search(r'(\d{2})[.,/](\d{2})[.,/](\d{4})', text)
            if match:
                d, m, y = match.groups()
                return f"{d}.{m}.{y}"
        except: pass
        return datetime.now().strftime("%d.%m.%Y")

    def process(self, img_path):
        img = cv2.imread(img_path)
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, np.array([0,40,50]), np.array([180,255,255]))
        kernel = np.ones((10,10), np.uint8)
        closed = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        
        if not contours: return None
        x, y, w, h = cv2.boundingRect(max(contours, key=cv2.contourArea))
        
        cw, ch = w / len(self.times), h / len(self.queues)
        data = {}
        for r, q in enumerate(self.queues):
            data[q] = {}
            for c, t in enumerate(self.times):
                cx, cy = int(x + c*cw + cw/2), int(y + r*ch + ch/2)
                roi = hsv[cy-1:cy+2, cx-1:cx+2]
                h_val, s_val, v_val = cv2.mean(roi)[:3]
                
                status = "unknown"
                if s_val < 40: pass
                elif 35 < h_val < 95: status = "on"
                elif 20 <= h_val <= 35: status = "maybe"
                elif (0 <= h_val < 20) or (160 < h_val <= 180): status = "off"
                data[q][t] = status
        return data

# === ЧАСТИНА 2: ОБРОБКА БАЗИ АДРЕС ===
def convert_csv_to_json():
    """Конвертує CSV в оптимізований JSON для пошуку"""
    if not os.path.exists(CSV_FILE):
        logger.error("Файл адрес не знайдено!")
        return []
    
    addresses = []
    encodings = ['utf-8', 'utf-8-sig', 'cp1251']
    
    for enc in encodings:
        try:
            with open(CSV_FILE, 'r', encoding=enc) as f:
                reader = csv.reader(f, delimiter=';')
                next(reader, None) # Пропуск заголовка
                for row in reader:
                    if len(row) >= 5:
                        # Формат: Група; Район; Місто; Вулиця; Будинок
                        addresses.append({
                            "g": row[0].strip(), # Group
                            "c": row[2].strip(), # City
                            "s": row[3].strip(), # Street
                            "h": row[4].strip()  # House
                        })
            logger.info(f"Адреси конвертовано: {len(addresses)} записів")
            return addresses
        except UnicodeDecodeError: continue
    return []

# === ЧАСТИНА 3: GITHUB SYNC ===
def upload_to_github(files_dict):
    token = os.getenv("GITHUB_TOKEN")
    repo_name = os.getenv("GITHUB_REPO")
    
    if not token or not repo_name:
        logger.error("Немає доступу до GitHub (.env)")
        return

    g = Github(token)
    repo = g.get_repo(repo_name)

    for filename, content in files_dict.items():
        json_content = json.dumps(content, indent=2, ensure_ascii=False)
        try:
            contents = repo.get_contents(filename)
            repo.update_file(contents.path, f"Update {filename}", json_content, contents.sha)
            logger.info(f"✅ Оновлено: {filename}")
        except:
            repo.create_file(filename, f"Create {filename}", json_content)
            logger.info(f"✅ Створено: {filename}")

# === ГОЛОВНИЙ ЦИКЛ ===
def main():
    processor = ScheduleProcessor()
    last_url = None

    while True:
        try:
            # 1. Оновлюємо базу адрес (якщо раптом ви змінили CSV локально)
            address_db = convert_csv_to_json()
            
            # 2. Шукаємо графік
            # Тут спрощена логіка - беремо URL з API
            # У повному варіанті використовуйте логіку перебору ключів як у боті
            r = requests.get(API_ENDPOINT, params={"option_key": "pw_gpv_image_today"})
            data_api = r.json()
            url = None
            
            if "hydra:member" in data_api and data_api["hydra:member"]:
                val = data_api["hydra:member"][0].get("value")
                if val: url = val if val.startswith("http") else f"https://api-toe-poweron.inneti.net/{val.lstrip('/')}"

            schedule_data = {}
            if url and url != last_url:
                logger.info(f"Новий графік: {url}")
                img_name = "temp_sched.png"
                with open(img_name, "wb") as f:
                    f.write(requests.get(url).content)
                
                raw_data = processor.process(img_name)
                date_str = processor.extract_date(img_name)
                
                if raw_data:
                    schedule_data = {
                        "date": date_str,
                        "updated_at": datetime.now().strftime("%d.%m.%Y %H:%M"),
                        "schedule": raw_data
                    }
                    last_url = url
            
            # 3. Відправка на GitHub (якщо є графік або адреси)
            files_to_upload = {}
            if address_db: 
                files_to_upload["addresses.json"] = address_db
            if schedule_data: 
                files_to_upload["schedule.json"] = schedule_data
            
            if files_to_upload:
                upload_to_github(files_to_upload)
            
            # Спимо 10 хвилин
            time.sleep(600)

        except Exception as e:
            logger.error(f"Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()