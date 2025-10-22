# -*- coding: utf-8 -*-
# GitHub Actions 対応版（ヘッドレス / プロファイル非依存 / 環境変数で認証）
# Publicページ（プロフィール）をスクレイプして Sheets に書き込み

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import StaleElementReferenceException, TimeoutException
import time
from datetime import datetime
import os
import re

import gspread
from google.oauth2.service_account import Credentials
import jpholiday

# ====== 設定 ======
PROFILE_URL = "https://jp.mercari.com/user/profile/412786978"  # 公開プロフィールURL（ログイン不要）
SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/1E0XCjvoEriGnBU8dhMro0bC464JJ5hOmiIZUrZoQal8/edit#gid=261546822"

# ====== Chrome 起動 ======
def setup_driver():
    opts = Options()
    # Actions上のヘッドレス安定化フラグ
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    # Selenium Manager が自動で適合Driverを使用
    return webdriver.Chrome(options=opts)

# ====== Wait & ユーティリティ ======
def safe_click(driver, by, value, retries=3, timeout=90):
    for i in range(retries):
        try:
            element = WebDriverWait(driver, timeout).until(
                EC.element_to_be_clickable((by, value))
            )
            element.click()
            return
        except StaleElementReferenceException:
            print(f"⚠️ StaleElement (retry {i+1}/{retries})")
            time.sleep(1)
        except TimeoutException:
            break
    raise Exception("❌ 要素が安定せずクリックできませんでした")

def int_price(text: str) -> int:
    # "¥12,300" → 12300
    m = re.sub(r"[^\d]", "", text or "")
    return int(m) if m.isdigit() else 0

# ====== Google Sheets 認証 ======
def setup_gsheet():
    cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "service_account.json")
    scope = ["https://www.googleapis.com/auth/spreadsheets",
             "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_file(cred_path, scopes=scope)
    return gspread.authorize(creds)

def update_or_create_sheet(spreadsheet, sheet_name, header, rows):
    try:
        ws = spreadsheet.worksheet(sheet_name)
        ws.clear()
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=sheet_name, rows="2000", cols="10")
    if rows:
        ws.update('A1', [header] + rows)
    else:
        ws.update('A1', [header])

def main():
    driver = setup_driver()
    wait = WebDriverWait(driver, 90)
    item_data = []

    try:
        # プロフィールへ
        driver.get(PROFILE_URL)

        # 検索/入力欄（表示トリガー）を軽くクリック（ページの初期化目的）
        try:
            safe_click(driver, By.XPATH, '//*[@id="main"]/div[3]/label/input', retries=1, timeout=10)
            time.sleep(1)
        except Exception:
            pass  # 要素が無いレイアウトでも続行

        # 「もっと見る」を押し切る
        more_xpath = '//button[text()="もっと見る"]'
        while True:
            try:
                more = wait.until(EC.element_to_be_clickable((By.XPATH, more_xpath)))
                more.click()
                driver.execute_script("window.scrollBy(0, 300);")
                time.sleep(0.8)
            except TimeoutException:
                break
            except Exception:
                break

        # スクロールで遅延ロードを全読込
        last_height = 0
        stable_rounds = 5
        while stable_rounds > 0:
            driver.execute_script("window.scrollBy(0, 800);")
            time.sleep(1.2)
            new_height = driver.execute_script("return document.body.scrollHeight")
            if new_height == last_height:
                stable_rounds -= 1
            else:
                last_height = new_height
                stable_rounds = 5

        # 商品カードの <a href="/item/..."> を収集
        seen = set()
        anchors = driver.find_elements(By.XPATH, '//a[contains(@href, "/item/")]')
        for a in anchors:
            try:
                href = a.get_attribute("href")
                if not href or href in seen:
                    continue
                seen.add(href)

                name_el = a.find_element(By.XPATH, './/span[@data-testid="thumbnail-item-name"]')
                price_el = a.find_element(By.XPATH, './/span[contains(@class,"number__")]')
                name = (name_el.text or "").strip()
                price_text = (price_el.text or "").strip()
                price = int_price(price_text)

                item_data.append([name, price, href])
            except Exception as e:
                print(f"❌ 商品取得失敗: {e}")
                continue

        # ====== Google Sheets 出力 ======
        gc = setup_gsheet()
        sh = gc.open_by_url(SPREADSHEET_URL)

        # 1. メルカリメンズ出品
        header_main = ['商品名', '価格', 'URL']
        update_or_create_sheet(sh, "メルカリメンズ出品", header_main, item_data)

        # 2. メルカリ100円値下げ（編集URL作成）
        rows_edit = []
        for name, price, url in item_data:
            edit_url = url.replace('/item/', '/sell/edit/') if '/item/' in url else url
            rows_edit.append([name, price, edit_url])
        update_or_create_sheet(sh, "メルカリ100円値下げ", header_main, rows_edit)

        # 3. メルカリコメント投稿（本文生成）
        today = datetime.now()
        is_holiday = jpholiday.is_holiday(today)
        is_weekend = today.weekday() >= 5  # 5=Sat, 6=Sun
        comment_head = "☆★土日祝限定SALE★☆\n" if (is_weekend or is_holiday) else "☆★本日限定SALE★☆\n"
        body = (
            "こちらの商品ご検討頂き\nありがとうございます♫本日に限り\n"
            "『ご希望の価格』を承ります！あまりに大幅な場合はお断りすることがございますが、"
            "できる限りご要望お応えしたいと思います！\n"
            "早い者勝ちになりますのでコメント\nにて金額ご提示ください(^^)\n"
        )
        header_comment = ['商品名', '価格', 'URL', 'コメント']
        rows_comment = [[name, price, url, comment_head + body] for name, price, url in item_data]
        update_or_create_sheet(sh, "メルカリコメント投稿", header_comment, rows_comment)

        print(f"✅ 出品件数: {len(item_data)} 件")
        print("✅ スプレッドシートへのアップロード完了")

    finally:
        try:
            driver.quit()
        except Exception:
            pass

if __name__ == "__main__":
    main()
