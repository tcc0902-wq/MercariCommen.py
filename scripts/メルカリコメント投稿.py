# -*- coding: utf-8 -*-
"""
GitHub Actions 対応版（安定化強化）
- ローカルChromeプロファイル非依存（--user-data-dir 未使用）
- ヘッドレス前提（CI向けオプションを追加）
- MERCARI_COOKIES_PATH（Secretsから展開）でCookie注入 → ログイン再現
- スプレッドシート認証は GOOGLE_APPLICATION_CREDENTIALS 環境変数で解決
- コメント欄の検出・送信・反映確認を堅牢化
- debug/*.png, *.html を出力（workflowで upload-artifact すれば取得可能）
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
# パス・環境
# =========================
REPO_ROOT = Path(__file__).resolve().parents[1]  # リポのルート
DEBUG_DIR = REPO_ROOT / "debug"
DEBUG_DIR.mkdir(parents=True, exist_ok=True)

COOKIES_PATH = os.environ.get("MERCARI_COOKIES_PATH", str(REPO_ROOT / "mercari_cookies.json"))

SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/1E0XCjvoEriGnBU8dhMro0bC464JJ5hOmiIZUrZoQal8/edit"
TARGET_SHEET   = "メルカリコメント投稿"   # ←必要に応じて「メルカリコメント投稿2」等に変更

# =========================
# Chrome 起動
# =========================
def create_driver():
    opts = Options()
    # CI向け安定化オプション
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--lang=ja-JP,ja")
    opts.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    )

    # Selenium Manager が ChromeDriver を自動解決
    driver = webdriver.Chrome(options=opts)
    driver.set_page_load_timeout(60)
    return driver

# =========================
# Cookie 注入（ログイン再現）
# =========================
def inject_cookies_if_available(driver):
    p = Path(COOKIES_PATH)
    if not p.exists():
        print("⏭️ Cookieファイルなし（未注入）:", p)
        return False

    try:
        with p.open("r", encoding="utf-8") as f:
            cookies = json.load(f)

        # 先にドメインへアクセス
        driver.get("https://jp.mercari.com/")
        time.sleep(1.0)

        count = 0
        for c in cookies:
            # name/value/domain は最低限必要
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

def is_logged_in(driver, timeout=5) -> bool:
    """
    ゆるいログイン判定：
      - ログインリンク/サインインが見当たらない
      - ヘッダーのプロフィール/出品ボタン系が見える
    """
    try:
        # 「ログイン」リンクが見えなくなることを基準に
        WebDriverWait(driver, timeout).until_not(
            EC.presence_of_element_located(
                (By.XPATH, "//a[contains(@href,'/signin') or contains(.,'ログイン')]")
            )
        )
        return True
    except TimeoutException:
        # 逆にプロフィール/出品ボタン等が見えたらOKにする
        icons = driver.find_elements(
            By.XPATH,
            "//*[@data-testid='header-profile' or contains(@href,'/sell') or contains(@href,'/mypage')]"
        )
        return len(icons) > 0

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
    # ステータス列（E=5）
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
# デバッグ出力
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
# UI Utility
# =========================
def expand_more_comments_if_any(driver):
    try:
        more = WebDriverWait(driver, 4).until(
            EC.element_to_be_clickable((By.XPATH, "//button[normalize-space()='コメントをもっと見る']"))
        )
        driver.execute_script("arguments[0].click();", more)
        print("👆『コメントをもっと見る』クリック済")
        time.sleep(0.6)
    except Exception:
        print("⏭️ 『コメントをもっと見る』は無し")

def get_comment_blocks(driver):
    return driver.find_elements(
        By.XPATH,
        "//*[(@data-testid='comment' or contains(@class,'CommentItem') or contains(@class,'comment'))]"
    )

def get_comment_count(driver):
    return len(get_comment_blocks(driver))

# --- 強化版：コメント欄検出 ---
def find_comment_textarea_stronger(driver, timeout=10):
    candidates = [
        (By.CSS_SELECTOR, "#item-info textarea"),
        (By.CSS_SELECTOR, "form textarea"),
        (By.XPATH, "//textarea[not(@disabled)]"),
        (By.XPATH, "//*[@data-testid='comment']//textarea"),
        (By.XPATH, "//div[contains(@class,'comment')]/textarea"),
        # プレースホルダー（クリックで textarea が出てくるUI）
        (By.XPATH, "//*[self::button or self::div or self::span][contains(., 'コメント') and not(contains(., 'もっと'))]"),
    ]
    end = time.time() + timeout
    while time.time() < end:
        for by, sel in candidates:
            elems = driver.find_elements(by, sel)
            for el in elems:
                try:
                    if el.tag_name.lower() != "textarea":
                        # プレースホルダーならクリックして再探索
                        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                        el.click()
                        time.sleep(0.5)
                        ta = driver.find_elements(By.XPATH, "//textarea[not(@disabled)]")
                        for t in ta:
                            if t.is_displayed() and t.is_enabled():
                                return t
                        continue
                    if el.is_displayed() and el.is_enabled():
                        return el
                except Exception:
                    pass
        time.sleep(0.3)
    return None

SUBMIT_XPATHS = [
    "//form//button[@type='submit' and contains(normalize-space(),'コメントを送信')]",
    "//button[@type='submit' and contains(normalize-space(),'コメント')]",
    "//form//button[@type='submit']",
]

def find_submit_button(driver, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        for xp in SUBMIT_XPATHS:
            btns = driver.find_elements(By.XPATH, xp)
            for b in btns:
                try:
                    if b.is_displayed() and b.is_enabled():
                        return b
                except Exception:
                    continue
        time.sleep(0.2)
    raise TimeoutException("送信ボタンが見つかりません")

def verify_posted(driver, comment_text: str, before_count: int, timeout=18):
    end = time.time() + timeout
    seen_toast = False
    partial = comment_text.strip()[:20]
    while time.time() < end:
        if get_comment_count(driver) > before_count:
            return True
        # トースト
        for pat in ["コメントを送信", "コメントを投稿", "コメントを送信しました", "コメントを投稿しました"]:
            if driver.find_elements(By.XPATH, f"//*[contains(normalize-space(), '{pat}')]"):
                seen_toast = True
        # textarea 空
        ta = find_comment_textarea_stronger(driver, timeout=1)
        if ta is not None and (ta.get_attribute("value") or "").strip() == "":
            if seen_toast or get_comment_count(driver) >= before_count:
                return True
        # 直近コメント一致
        blocks = get_comment_blocks(driver)
        if blocks:
            last_txt = (blocks[-1].text or "").strip()
            if partial and partial in last_txt:
                return True
        time.sleep(0.4)
    return False

def wait_item_loaded(driver, timeout=25):
    try:
        WebDriverWait(driver, timeout).until(
            EC.any_of(
                EC.presence_of_element_located((By.CSS_SELECTOR, "h1")),
                EC.presence_of_element_located((By.CSS_SELECTOR, "#item-info textarea"))
            )
        )
        return True
    except TimeoutException:
        return False

# =========================
# メイン
# =========================
def main():
    driver = create_driver()
    wait = WebDriverWait(driver, 15)

    # Cookie 注入 → ログイン状態チェック
    inject_cookies_if_available(driver)
    driver.get("https://jp.mercari.com/")
    time.sleep(1.0)
    if not is_logged_in(driver):
        print("⚠️ 未ログインの可能性があります（Cookie不十分/期限切れ）。ログイン必須の処理は失敗する可能性があります。")

    worksheet, data, status_col = load_sheet_rows()

    for idx, row in enumerate(data, start=2):  # シート上の行番号
        try:
            url     = row[2] if len(row) > 2 else ""
            comment = row[3] if len(row) > 3 else ""

            if not url or not comment.strip():
                print(f"Row {idx}: URL/コメントが空のためスキップ")
                continue

            # アクセス
            driver.get(url)
            print(f"\nRow {idx}: アクセス → {url}")

            if not wait_item_loaded(driver, timeout=30):
                print(f"Row {idx}: ⚠️ 商品ページ読み込み失敗")
                save_debug(driver, f"load_timeout_row{idx}")
                mark_fail(worksheet, idx, status_col, "読み込み失敗")
                continue

            expand_more_comments_if_any(driver)

            # 送信前の件数
            before = get_comment_count(driver)

            # テキストエリア取得＆入力（強化版で再試行）
            area = None
            for attempt in range(1, 4):
                area = find_comment_textarea_stronger(driver, timeout=3)
                if area:
                    break
                print(f"Row {idx}: コメント欄検出失敗 {attempt}/3 → スクロール再試行")
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(1.0)
            if not area:
                print(f"Row {idx}: ❌ コメント欄未検出")
                save_debug(driver, f"no_textarea_row{idx}")
                mark_fail(worksheet, idx, status_col, "コメント欄なし")
                continue

            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", area)
            WebDriverWait(driver, 6).until(EC.element_to_be_clickable(area))
            area.click(); time.sleep(0.1)
            try:
                area.clear()
            except Exception:
                pass
            time.sleep(0.1)
            area.send_keys(comment)
            # 入力イベント発火
            driver.execute_script("arguments[0].dispatchEvent(new Event('input', {bubbles:true}));", area)

            # 送信ボタン取得 → クリック（JS → ネイティブ → Actions）
            try:
                btn = find_submit_button(driver, timeout=12)
            except TimeoutException:
                print(f"Row {idx}: ❌ 送信ボタン未検出")
                save_debug(driver, f"no_submit_row{idx}")
                mark_fail(worksheet, idx, status_col, "送信ボタンなし")
                continue

            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
            time.sleep(0.2)
            clicked = False
            for how in ("js", "native", "actions"):
                try:
                    if how == "js":
                        driver.execute_script("arguments[0].click();", btn)
                    elif how == "native":
                        btn.click()
                    else:
                        ActionChains(driver).move_to_element(btn).pause(0.05).click().perform()
                    print("🚀 送信ボタンをクリック")
                    clicked = True
                    break
                except Exception as e:
                    print(f"送信ボタン クリック失敗({how}): {e}")
                    time.sleep(0.3)

            if not clicked:
                print(f"Row {idx}: ❌ 送信クリックに失敗")
                save_debug(driver, f"post_clickfail_row{idx}")
                mark_fail(worksheet, idx, status_col, "クリック失敗")
                continue

            # 反映検証
            ok = verify_posted(driver, comment_text=comment, before_count=before, timeout=20)
            if ok:
                print(f"Row {idx}: ✅ 投稿完了（反映確認済）")
                # 成功時に「完了」を入れたい場合は以下を有効化
                # worksheet.update_cell(idx, status_col, "完了")
            else:
                print(f"Row {idx}: ❌ 投稿失敗（反映確認できず）")
                save_debug(driver, f"post_fail_row{idx}")
                mark_fail(worksheet, idx, status_col, "反映確認できず")

            # クールダウン
            wt = random.uniform(2.5, 4.0)
            time.sleep(wt)
            print(f"Row {idx}: ⏳ {wt:.1f} 秒待機")

        except TimeoutException as te:
            print(f"Row {idx}: Timeout → {te}")
            save_debug(driver, f"timeout_row{idx}")
            mark_fail(worksheet, idx, status_col, "Timeout")
            continue
        except WebDriverException as we:
            print(f"Row {idx}: WebDriver例外 → {we}")
            save_debug(driver, f"webdriver_row{idx}")
            mark_fail(worksheet, idx, status_col, "WebDriver")
            # ドライバ再起動で継続（Cookie再注入）
            try:
                driver.quit()
            except Exception:
                pass
            driver = create_driver()
            inject_cookies_if_available(driver)
            wait = WebDriverWait(driver, 15)
            continue
        except Exception as e:
            print(f"Row {idx}: 予期せぬ例外 → {e}\n{traceback.format_exc()}")
            save_debug(driver, f"unexpected_row{idx}")
            mark_fail(worksheet, idx, status_col, "例外")
            continue

    # 終了
    try:
        driver.quit()
    except Exception:
        pass
    print("✅ 全コメント投稿処理 完了")

if __name__ == "__main__":
    main()
