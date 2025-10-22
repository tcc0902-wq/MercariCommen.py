# -*- coding: utf-8 -*-
"""
メルカリ コメント投稿スクリプト（GitHub Actions対応）
- Chrome固定プロファイル使用せず、一時プロファイルで衝突回避
- ヘッドレスはコメントアウトでON/OFFを切り替え可能
"""

import os
import time
import json
import random
import datetime
import tempfile
import shutil
import atexit
import traceback
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException


# ====== パス・設定 ======
REPO_ROOT = Path(__file__).resolve().parents[1]
DEBUG_DIR = REPO_ROOT / "debug"
DEBUG_DIR.mkdir(parents=True, exist_ok=True)

COOKIES_PATH = os.environ.get("MERCARI_COOKIES_PATH", str(REPO_ROOT / "mercari_cookies.json"))
SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/1E0XCjvoEriGnBU8dhMro0bC464JJ5hOmiIZUrZoQal8/edit"
TARGET_SHEET = "メルカリコメント投稿"


# ====== Chrome起動 ======
def create_driver():
    chrome_options = Options()

    # ==== ヘッドレス設定（ここでON/OFFを切り替える）====
  
    chrome_options.add_argument("--headless=chrome")  # 必要に応じて外してOK（OFF）

    # 一時プロファイルで競合防止
    tmp_profile = tempfile.mkdtemp(prefix="mercari_profile_")
    chrome_options.add_argument(f"--user-data-dir={tmp_profile}")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")

    driver = webdriver.Chrome(options=chrome_options)
    driver.set_page_load_timeout(60)

    # 終了時に一時プロファイル削除
    def cleanup():
        shutil.rmtree(tmp_profile, ignore_errors=True)
    atexit.register(cleanup)

    return driver


# ====== Cookie注入 ======
def inject_cookies(driver):
    path = Path(COOKIES_PATH)
    if not path.exists():
        print("⏭️ Cookieファイルが存在しません:", path)
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            cookies = json.load(f)
        driver.get("https://jp.mercari.com/")
        time.sleep(1)
        count = 0
        for c in cookies:
            try:
                driver.add_cookie(c)
                count += 1
            except Exception:
                pass
        print(f"🍪 Cookie注入完了: {count}件")
    except Exception as e:
        print("⚠️ Cookie読み込みエラー:", e)


# ====== Google Sheets ======
def load_sheet_rows():
    cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "service_account.json")
    scope = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_file(cred_path, scopes=scope)
    client = gspread.authorize(creds)
    ws = client.open_by_url(SPREADSHEET_URL).worksheet(TARGET_SHEET)
    data = ws.get_all_values()
    return ws, data


# ====== デバッグ保存 ======
def save_debug(driver, prefix):
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    png = DEBUG_DIR / f"{prefix}_{ts}.png"
    html = DEBUG_DIR / f"{prefix}_{ts}.html"
    try:
        driver.save_screenshot(str(png))
        with open(html, "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        print(f"🧾 デバッグ保存: {png}, {html}")
    except Exception as e:
        print(f"デバッグ保存失敗: {e}")


# ====== コメント投稿メイン処理 ======
def main():
    driver = create_driver()
    wait = WebDriverWait(driver, 15)

    inject_cookies(driver)
    driver.get("https://jp.mercari.com/")
    time.sleep(1)

    ws, rows = load_sheet_rows()
    print("✅ スプレッドシート読込完了:", len(rows), "行")

    for idx, row in enumerate(rows[1:], start=2):
        try:
            url = row[2] if len(row) > 2 else ""
            comment = row[3] if len(row) > 3 else ""

            if not url or not comment.strip():
                print(f"Row {idx}: URLまたはコメントが空のためスキップ")
                continue

            driver.get(url)
            print(f"\nRow {idx}: アクセス → {url}")
            time.sleep(3)

            textarea = None
            for attempt in range(3):
                elems = driver.find_elements(By.TAG_NAME, "textarea")
                textarea = next((e for e in elems if e.is_displayed()), None)
                if textarea:
                    break
                driver.execute_script("window.scrollBy(0, 500);")
                time.sleep(1)

            if not textarea:
                print(f"Row {idx}: ❌ コメント欄が見つかりません")
                save_debug(driver, f"no_textarea_row{idx}")
                continue

            textarea.click()
            textarea.clear()
            textarea.send_keys(comment)
            print(f"Row {idx}: コメント入力完了")

            # 送信ボタン
            buttons = driver.find_elements(By.XPATH, "//button[contains(text(),'コメント')]")
            button = next((b for b in buttons if b.is_displayed()), None)
            if button:
                driver.execute_script("arguments[0].click();", button)
                print("🚀 コメント送信クリック")
            else:
                print("⚠️ 送信ボタンが見つかりません")

            time.sleep(random.uniform(2.5, 4.0))
        except Exception as e:
            print(f"Row {idx}: エラー発生: {e}")
            save_debug(driver, f"error_row{idx}")

    driver.quit()
    print("✅ 全処理完了")


if __name__ == "__main__":
    main()
