# -*- coding: utf-8 -*-
"""
レディース：出品一覧取得（GitHub Actions対応）
- 固定 --user-data-dir を使わず、一時プロファイルで衝突回避
- ヘッドレスはコメントアウトだけで ON/OFF 切替
- Cookie は MERCARI_COOKIES_PATH（Secrets）から注入（未設定でも動作）
- 取得結果を以下の3シートに書き込み：
    1) メルカリ出品2            [商品名, 価格, URL]
    2) メルカリ100円値下げ2     [商品名, 価格, 編集URL]
    3) メルカリコメント投稿2     [商品名, 価格, URL, コメント]
"""

import os
import time
import json
import tempfile
import shutil
import atexit
import datetime
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


# ====== 設定 ======
REPO_ROOT = Path(__file__).resolve().parents[1]
DEBUG_DIR = REPO_ROOT / "debug"
DEBUG_DIR.mkdir(parents=True, exist_ok=True)

COOKIES_PATH = os.environ.get("MERCARI_COOKIES_PATH", str(REPO_ROOT / "mercari_cookies.json"))
SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/1E0XCjvoEriGnBU8dhMro0bC464JJ5hOmiIZUrZoQal8/edit"

# ★ 対象ユーザープロフィール（レディース）
PROFILE_URL = "https://jp.mercari.com/user/profile/515867944"

SHEET_LISTING   = "メルカリ出品2"
SHEET_EDIT      = "メルカリ100円値下げ2"
SHEET_COMMENT   = "メルカリコメント投稿2"


# ====== Chrome 起動 ======
def create_driver():
    chrome_options = Options()

    # ==== ヘッドレス（ここでON/OFF切替）====
    
    #chrome_options.add_argument("--headless=chrome")  # 必要に応じて外してOK（OFF）

    # 一時プロファイルで競合防止
    tmp_profile = tempfile.mkdtemp(prefix="mercari_profile_")
    chrome_options.add_argument(f"--user-data-dir={tmp_profile}")
    chrome_options.add_argument("--no-first-run")
    chrome_options.add_argument("--no-default-browser-check")

    # CI安定化
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")

    driver = webdriver.Chrome(options=chrome_options)
    driver.set_page_load_timeout(60)

    # 終了時に一時プロファイル削除
    def cleanup():
        try:
            shutil.rmtree(tmp_profile, ignore_errors=True)
        except Exception:
            pass
    atexit.register(cleanup)

    return driver


# ====== Cookie 注入（任意：ログインが必要な場合のみ） ======
def inject_cookies_if_available(driver):
    p = Path(COOKIES_PATH)
    if not p.exists():
        print("⏭️ Cookieファイルなし（未注入）:", p)
        return
    try:
        with p.open("r", encoding="utf-8") as f:
            cookies = json.load(f)
        driver.get("https://jp.mercari.com/")
        time.sleep(1.0)
        ok = 0
        for c in cookies:
            # 基本的な項目があればそのまま入れて問題ない
            if not all(k in c for k in ("name", "value", "domain")):
                continue
            try:
                driver.add_cookie(c)
                ok += 1
            except Exception:
                pass
        print(f"🍪 Cookie 注入完了: {ok} 件")
    except Exception as e:
        print("⚠️ Cookie注入エラー:", e)


# ====== Google Sheets ======
def open_sheet():
    cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "service_account.json")
    scope = ["https://www.googleapis.com/auth/spreadsheets",
             "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_file(cred_path, scopes=scope)
    client = gspread.authorize(creds)
    return client.open_by_url(SPREADSHEET_URL)

def update_or_create_sheet(spreadsheet, sheet_name, header, rows):
    try:
        ws = spreadsheet.worksheet(sheet_name)
        ws.clear()
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=sheet_name, rows="2000", cols="10")
    ws.update('A1', [header] + rows)


# ====== スクレイプ ======
def scrape_listings(driver, profile_url: str):
    wait = WebDriverWait(driver, 30)

    driver.get(profile_url)
    # 検索ボックスクリック（ページ読込トリガ兼ねていたため残す）
    try:
        inp = wait.until(EC.element_to_be_clickable((By.XPATH, '//*[@id="main"]/div[3]/label/input')))
        inp.click()
    except Exception:
        pass
    time.sleep(1.5)

    # 「もっと見る」を叩けるだけ叩く
    more_xpath = '//button[text()="もっと見る"]'
    while True:
        try:
            more = WebDriverWait(driver, 5).until(EC.element_to_be_clickable((By.XPATH, more_xpath)))
            driver.execute_script("arguments[0].click();", more)
            driver.execute_script("window.scrollBy(0, 500);")
            time.sleep(0.8)
        except Exception:
            break

    # 遅延ロード対策：底までゆっくりスクロール
    last_h = 0
    stagnation = 0
    while stagnation < 5:
        driver.execute_script("window.scrollBy(0, 800);")
        time.sleep(1.2)
        h = driver.execute_script("return document.body.scrollHeight")
        if h == last_h:
            stagnation += 1
        else:
            stagnation = 0
            last_h = h

    # 商品 a タグ単位で抽出
    items = []
    seen = set()
    links = driver.find_elements(By.XPATH, '//a[contains(@href, "/item/")]')
    for el in links:
        try:
            url = el.get_attribute('href')
            if not url or url in seen:
                continue
            seen.add(url)
            name_el = el.find_element(By.XPATH, './/span[@data-testid="thumbnail-item-name"]')
            price_el = el.find_element(By.XPATH, './/span[contains(@class,"number__")]')
            name = name_el.text.strip()
            price = price_el.text.strip()
            items.append([name, price, url])
        except Exception:
            continue

    print(f"✅ 取得件数: {len(items)}")
    return items


# ====== コメント文面生成 ======
def build_comment_text():
    import jpholiday
    today = datetime.datetime.now()
    is_holiday = jpholiday.is_holiday(today)
    is_weekend = today.weekday() >= 5  # 5:土, 6:日

    head = "☆★土日祝限定SALE★☆\n" if (is_holiday or is_weekend) else "☆★本日限定SALE★☆\n"
    body = (
        "こちらの商品ご検討頂き\nありがとうございます♫本日に限り\n"
        "『ご希望の価格』を承ります！あまりに大幅な場合はお断りすることがございますが、"
        "できる限りご要望お応えしたいと思います！\n"
        "早い者勝ちになりますのでコメント\nにて金額ご提示ください(^^)\n"
    )
    return head + body


# ====== メイン ======
def main():
    driver = create_driver()
    try:
        # （必要な場合のみ）Cookie 注入
        inject_cookies_if_available(driver)

        # 出品一覧取得
        items = scrape_listings(driver, PROFILE_URL)

        # スプレッドシートへ出力
        spreadsheet = open_sheet()

        # 1) 出品一覧
        header_main = ['商品名', '価格', 'URL']
        update_or_create_sheet(spreadsheet, SHEET_LISTING, header_main, items)

        # 2) 100円値下げシート（編集URL）
        rows_edit = []
        for name, price, url in items:
            edit_url = url.replace('/item/', '/sell/edit/') if '/item/' in url else url
            rows_edit.append([name, price, edit_url])
        update_or_create_sheet(spreadsheet, SHEET_EDIT, header_main, rows_edit)

        # 3) コメント投稿シート
        comment = build_comment_text()
        header_comment = ['商品名', '価格', 'URL', 'コメント']
        rows_comment = [[name, price, url, comment] for name, price, url in items]
        update_or_create_sheet(spreadsheet, SHEET_COMMENT, header_comment, rows_comment)

        print("✅ スプレッドシートへのアップロード完了")
    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()
