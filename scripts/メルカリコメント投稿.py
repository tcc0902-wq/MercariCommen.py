# -*- coding: utf-8 -*-
"""
GitHub Actions 対応版（ヘッドレス切替可）
- Chromeをヘッドレス or 画面表示ありで実行（コメントアウトで切替）
- Cookieは MERCARI_COOKIES_PATH から読み込み
- スプレッドシートは GOOGLE_APPLICATION_CREDENTIALS から認証
"""

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException

from google.oauth2.service_account import Credentials
import gspread
import os
import time
import random
import datetime
import traceback
import json
from pathlib import Path

# =========================
# パス設定
# =========================
REPO_ROOT = Path(__file__).resolve().parents[1]
DEBUG_DIR = REPO_ROOT / "debug"
DEBUG_DIR.mkdir(parents=True, exist_ok=True)

COOKIES_PATH = os.environ.get("MERCARI_COOKIES_PATH", str(REPO_ROOT / "mercari_cookies.json"))
SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/1E0XCjvoEriGnBU8dhMro0bC464JJ5hOmiIZUrZoQal8/edit"
TARGET_SHEET = "メルカリコメント投稿"

# =========================
# Chrome 起動
# =========================
def create_driver():
    opts = Options()

    # ✅ ここをコメントアウトで切り替え
    # ------------------------------
    # opts.add_argument("--headless=new")  # ← ヘッドレスモードON（通常運用）
    # ------------------------------
    # ↑ コメントアウトすると「画面表示あり」で実行（デバッグ用）
    # ------------------------------

    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")

    driver = webdriver.Chrome(options=opts)
    driver.set_page_load_timeout(60)
    return driver

# =========================
# Cookie 注入
# =========================
def inject_cookies_if_available(driver):
    p = Path(COOKIES_PATH)
    if not p.exists():
        print("⏭️ Cookieファイルなし（未注入）:", p)
        return False
    try:
        with p.open("r", encoding="utf-8") as f:
            cookies = json.load(f)
        driver.get("https://jp.mercari.com/")
        time.sleep(1.0)
        count = 0
        for c in cookies:
            if not all(k in c for k in ("name", "value", "domain")):
                continue
            ck = {
                "name": c["name"],
                "value": c["value"],
                "domain": c["domain"],
                "path": c.get("path", "/"),
                "secure": bool(c.get("secure", True)),
                "httpOnly": bool(c.get("httpOnly", False)),
            }
            if "expiry" in c:
                ck["expiry"] = c["expiry"]
            try:
                driver.add_cookie(ck)
                count += 1
            except Exception:
                pass
        print(f"🍪 Cookie 注入完了: {count} 件")
        return count > 0
    except Exception as e:
        print("⚠️ Cookie注入エラー:", e)
        return False

# =========================
# ログイン判定
# =========================
def check_logged_in(driver, timeout=5):
    try:
        WebDriverWait(driver, timeout).until_not(
            EC.presence_of_element_located((By.XPATH, "//a[contains(@href,'/signin') or contains(text(),'ログイン')]"))
        )
        return True
    except TimeoutException:
        return False

# =========================
# Google スプレッドシート
# =========================
def load_sheet_rows():
    cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "service_account.json")
    scope = ["https://www.googleapis.com/auth/spreadsheets",
             "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_file(cred_path, scopes=scope)
    client = gspread.authorize(creds)
    ws = client.open_by_url(SPREADSHEET_URL).worksheet(TARGET_SHEET)
    rows = ws.get_all_values()
    header = rows[0] if rows else []
    data = rows[1:] if len(rows) > 1 else []
    try:
        status_col = header.index("ステータス") + 1
    except ValueError:
        status_col = 5
    return ws, data, status_col

def mark_fail(worksheet, sheet_row: int, status_col: int, reason: str = ""):
    val = "失敗" + (f"（{reason}）" if reason else "")
    try:
        worksheet.update_cell(sheet_row, status_col, val)
    except Exception as e:
        print(f"⚠️ ステータス更新失敗: {e}")

# =========================
# デバッグ保存
# =========================
def now_tag():
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

def save_debug(driver, prefix):
    try:
        ts = now_tag()
        png = DEBUG_DIR / f"{prefix}_{ts}.png"
        html = DEBUG_DIR / f"{prefix}_{ts}.html"
        driver.save_screenshot(str(png))
        with html.open("w", encoding="utf-8") as f:
            f.write(driver.page_source)
        print(f"🧾 デバッグ保存: {png} / {html}")
    except Exception as e:
        print(f"デバッグ保存失敗: {e}")

# =========================
# コメント欄関連
# =========================
def find_comment_textarea(driver):
    selectors = [
        (By.CSS_SELECTOR, "#item-info textarea"),
        (By.CSS_SELECTOR, "form textarea"),
        (By.XPATH, "//textarea[not(@disabled)]"),
    ]
    for by, sel in selectors:
        for el in driver.find_elements(by, sel):
            try:
                if el.is_displayed() and el.is_enabled():
                    return el
            except Exception:
                continue
    return None

def get_comment_count(driver):
    els = driver.find_elements(By.XPATH, "//*[contains(@class,'comment')]")
    return len(els)

def find_submit_button(driver):
    btns = driver.find_elements(By.XPATH, "//button[contains(text(),'コメントを送信')]")
    for b in btns:
        if b.is_displayed() and b.is_enabled():
            return b
    raise TimeoutException("送信ボタンが見つかりません")

# =========================
# メイン処理
# =========================
def main():
    driver = create_driver()
    inject_cookies_if_available(driver)
    driver.get("https://jp.mercari.com/")
    if not check_logged_in(driver):
        print("⚠️ Cookieログイン失敗の可能性があります")

    ws, data, status_col = load_sheet_rows()

    for idx, row in enumerate(data, start=2):
        try:
            url = row[2] if len(row) > 2 else ""
            comment = row[3] if len(row) > 3 else ""
            if not url or not comment.strip():
                continue

            driver.get(url)
            print(f"\nRow {idx}: {url}")

            area = find_comment_textarea(driver)
            if not area:
                print(f"Row {idx}: ❌ コメント欄未検出")
                save_debug(driver, f"no_textarea_row{idx}")
                mark_fail(ws, idx, status_col, "コメント欄なし")
                continue

            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", area)
            area.click(); time.sleep(0.3)
            area.clear(); area.send_keys(comment)

            btn = find_submit_button(driver)
            driver.execute_script("arguments[0].click();", btn)
            print(f"Row {idx}: ✅ コメント送信完了")
            time.sleep(random.uniform(2, 4))
        except Exception as e:
            print(f"Row {idx}: ⚠️ エラー: {e}")
            save_debug(driver, f"error_row{idx}")
            mark_fail(ws, idx, status_col, "エラー")

    driver.quit()
    print("✅ 全処理完了")

if __name__ == "__main__":
    main()
