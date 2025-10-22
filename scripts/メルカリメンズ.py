#     python "scripts/メルカリメンズ.py"
# -*- coding: utf-8 -*-

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import StaleElementReferenceException
import time
import jpholiday
from datetime import datetime
import os
import gspread
from google.oauth2.service_account import Credentials
import re
import tempfile
import shutil
import atexit

# ====== 設定 ======
PROFILE_URL = "https://jp.mercari.com/user/profile/412786978"  # ★メンズ
SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/1E0XCjvoEriGnBU8dhMro0bC464JJ5hOmiIZUrZoQal8/edit#gid=261546822"

SHEET_MAIN_NAME   = "メルカリメンズ出品"    # 商品名, 価格, URL
SHEET_EDIT_NAME   = "メルカリ100円値下げ"   # 商品名, 価格, 編集URL
SHEET_CM_NAME     = "メルカリコメント投稿"   # 商品名, 価格, URL, コメント

# ====== ドライバ作成（テンポラリプロフィールで競合回避）======
def create_driver():
    chrome_options = Options()

    # === ヘッドレス（ON / OFF はこの1行をコメントアウトで切替）===
    chrome_options.add_argument("--headless=new")   # ←ヘッドレスON
    # chrome_options.add_argument("--headless=new")  # ←ヘッドレスOFFにしたい時は上をコメントアウト

    # CI安定化
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")

    # 一時プロファイルを使う（毎回ユニーク）
    tmp_profile = tempfile.mkdtemp(prefix="mercari_profile_")
    chrome_options.add_argument(f"--user-data-dir={tmp_profile}")
    chrome_options.add_argument("--no-first-run")
    chrome_options.add_argument("--no-default-browser-check")

    driver = webdriver.Chrome(options=chrome_options)
    driver.set_page_load_timeout(60)

    # 後始末
    def _cleanup():
        try:
            driver.quit()
        except Exception:
            pass
        try:
            shutil.rmtree(tmp_profile, ignore_errors=True)
        except Exception:
            pass

    atexit.register(_cleanup)
    return driver

# ====== 安定クリック ======
def safe_click(driver, by, value, retries=3):
    for i in range(retries):
        try:
            element = WebDriverWait(driver, 30).until(
                EC.element_to_be_clickable((by, value))
            )
            element.click()
            return
        except StaleElementReferenceException:
            print(f"⚠️ StaleElement (retry {i+1}/{retries})")
            time.sleep(1)
    raise Exception("❌ 要素が安定せずクリックできませんでした")

# ====== Google スプレッドシート ======
def open_spreadsheet():
    cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "service_account.json")
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/spreadsheets",
    ]
    credentials = Credentials.from_service_account_file(cred_path, scopes=scope)
    client = gspread.authorize(credentials)
    return client.open_by_url(SPREADSHEET_URL)

def update_or_create_sheet(spreadsheet, sheet_name, header, rows):
    try:
        worksheet = spreadsheet.worksheet(sheet_name)
        worksheet.clear()
    except gspread.exceptions.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=sheet_name, rows="1000", cols="10")
    worksheet.update('A1', [header] + rows)

# ====== メイン ======
def main():
    driver = create_driver()
    wait = WebDriverWait(driver, 30)

    # 1. プロフィールにアクセス
    driver.get(PROFILE_URL)

    # 検索入力欄（ページ内検索）を一度クリック
    try:
        safe_click(driver, By.XPATH, '//*[@id="main"]/div[3]/label/input')
        time.sleep(1.5)
    except Exception:
        pass

    # 「もっと見る」を可能な限り押す
    more_xpath = '//button[text()="もっと見る"]'
    while True:
        try:
            more = wait.until(EC.element_to_be_clickable((By.XPATH, more_xpath)))
            more.click()
            driver.execute_script("window.scrollBy(0, 400);")
            time.sleep(1)
        except Exception:
            break

    # ゆっくりスクロールして全件読み込み
    last_height = 0
    retries = 5
    while retries > 0:
        driver.execute_script("window.scrollBy(0, 600);")
        time.sleep(1.8)
        new_height = driver.execute_script("return document.body.scrollHeight")
        if new_height == last_height:
            retries -= 1
        else:
            last_height = new_height
            retries = 5

    # 2. 商品 a タグを収集
    item_data = []
    seen = set()
    elements = driver.find_elements(By.XPATH, '//a[contains(@href, "/item/")]')
    for el in elements:
        try:
            url = el.get_attribute('href')
            if not url or url in seen:
                continue
            seen.add(url)

            name_elem = el.find_element(By.XPATH, './/span[@data-testid="thumbnail-item-name"]')
            price_elem = el.find_element(By.XPATH, './/span[contains(@class,"number__")]')
            name = (name_elem.text or "").strip()
            price = (price_elem.text or "").strip()
            if not name or not price:
                continue
            item_data.append([name, price, url])
        except Exception as e:
            print(f"❌ 商品取得失敗: {e}")
            continue

    print(f"✅ 取得件数: {len(item_data)} 件")

    # 3. Google シート更新
    spreadsheet = open_spreadsheet()

    header_main = ['商品名', '価格', 'URL']
    update_or_create_sheet(spreadsheet, SHEET_MAIN_NAME, header_main, item_data)

    rows_edit = []
    for name, price, url in item_data:
        edit_url = url.replace('/item/', '/sell/edit/') if '/item/' in url else url
        rows_edit.append([name, price, edit_url])
    update_or_create_sheet(spreadsheet, SHEET_EDIT_NAME, header_main, rows_edit)

    # コメント文面
    today = datetime.now()
    weekday = today.strftime('%A')
    is_holiday = jpholiday.is_holiday(today)
    is_weekend = weekday in ['Saturday', 'Sunday']
    comment = (
        "☆★土日祝限定SALE★☆\n" if is_weekend or is_holiday else "☆★本日限定SALE★☆\n"
    )
    comment += (
        "こちらの商品ご検討頂き\nありがとうございます♫本日に限り\n"
        "『ご希望の価格』を承ります！あまりに大幅な場合はお断りすることがございますが、"
        "できる限りご要望お応えしたいと思います！\n"
        "早い者勝ちになりますのでコメント\nにて金額ご提示ください(^^)\n"
    )

    header_comment = ['商品名', '価格', 'URL', 'コメント']
    rows_comment = [[name, price, url, comment] for name, price, url in item_data]
    update_or_create_sheet(spreadsheet, SHEET_CM_NAME, header_comment, rows_comment)

    print("✅ スプレッドシートへのアップロード完了")

    try:
        driver.quit()
    except Exception:
        pass

if __name__ == "__main__":
    main()
