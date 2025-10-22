# -*- coding: utf-8 -*-
"""
メルカリ レディース：コメント投稿（GitHub Actions対応）
- 固定プロファイルは使わず、一時プロファイルで衝突回避
- ヘッドレスはコメントアウトだけで ON/OFF 切替
- Cookie は MERCARI_COOKIES_PATH（Secrets）から注入
- Google Sheets は GOOGLE_APPLICATION_CREDENTIALS で認証
- 失敗時は debug/ に HTML/PNG を保存（Actions Artifact で確認可能）
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


# ====== パス・定数 ======
REPO_ROOT = Path(__file__).resolve().parents[1]
DEBUG_DIR = REPO_ROOT / "debug"
DEBUG_DIR.mkdir(parents=True, exist_ok=True)

COOKIES_PATH = os.environ.get("MERCARI_COOKIES_PATH", str(REPO_ROOT / "mercari_cookies.json"))
SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/1E0XCjvoEriGnBU8dhMro0bC464JJ5hOmiIZUrZoQal8/edit"
TARGET_SHEET = "メルカリコメント投稿2"   # ★ レディース用シート名


# ====== Chrome 起動 ======
def create_driver():
    chrome_options = Options()

    # ==== ヘッドレス設定（ここでON/OFFを切り替える）====
    chrome_options.add_argument("--headless=chrome")  # 必要に応じて外してOK（ON）
    #chrome_options.add_argument("--headless=chrome")  # 必要に応じて外してOK（OFF）

    # 一時プロファイルで競合防止
    tmp_profile = tempfile.mkdtemp(prefix="mercari_profile_")
    chrome_options.add_argument(f"--user-data-dir={tmp_profile}")
    chrome_options.add_argument("--no-first-run")
    chrome_options.add_argument("--no-default-browser-check")

    # CI 安定化
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


# ====== Cookie 注入（ログイン再現） ======
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
        ok = 0
        for c in cookies:
            try:
                # name/value/domain があればそのまま使える想定
                driver.add_cookie(c)
                ok += 1
            except Exception:
                pass
        print(f"🍪 Cookie注入完了: {ok}件")
    except Exception as e:
        print("⚠️ Cookie読み込みエラー:", e)


# ====== Google Sheets ======
def load_sheet_rows():
    cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "service_account.json")
    scope = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_file(cred_path, scopes=scope)
    client = gspread.authorize(creds)
    ws = client.open_by_url(SPREADSHEET_URL).worksheet(TARGET_SHEET)
    rows = ws.get_all_values()
    header = rows[0] if rows else []
    data = rows[1:] if len(rows) > 1 else []
    try:
        status_col = header.index("ステータス") + 1
    except ValueError:
        status_col = 5  # E列デフォルト
    return ws, data, status_col


def mark_fail(worksheet, sheet_row: int, status_col: int, reason: str = ""):
    val = "失敗" + (f"（{reason}）" if reason else "")
    try:
        worksheet.update_cell(sheet_row, status_col, val)
    except Exception as e:
        print(f"⚠️ ステータス更新失敗: {e}")


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


# ====== UI ユーティリティ ======
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


def find_comment_textarea(driver):
    candidates = [
        (By.CSS_SELECTOR, "#item-info textarea"),
        (By.CSS_SELECTOR, "form textarea"),
        (By.XPATH, "//textarea[not(@disabled)]"),
        (By.XPATH, "//textarea[contains(@placeholder,'コメント') or contains(@aria-label,'コメント')]"),
    ]
    for by, sel in candidates:
        for el in driver.find_elements(by, sel):
            try:
                if el.is_displayed() and el.is_enabled():
                    return el
            except Exception:
                continue
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


def verify_posted(driver, comment_text: str, before_count: int, timeout=18) -> bool:
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
        tas = driver.find_elements(By.TAG_NAME, "textarea")
        if tas and all((ta.get_attribute("value") or "").strip() == "" for ta in tas):
            if seen_toast:
                return True
        # 直近コメント一致
        blocks = get_comment_blocks(driver)
        if blocks:
            last_txt = (blocks[-1].text or "").strip()
            if partial and partial in last_txt:
                return True
        time.sleep(0.3)
    return False


def wait_item_loaded(driver, timeout=25) -> bool:
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


# ====== メイン処理 ======
def main():
    driver = create_driver()
    try:
        wait = WebDriverWait(driver, 15)

        # Cookie 注入 → 軽くトップへ
        inject_cookies(driver)
        driver.get("https://jp.mercari.com/")
        time.sleep(1)

        worksheet, data, status_col = load_sheet_rows()
        print("✅ スプレッドシート読込完了:", len(data), "行")

        for idx, row in enumerate(data, start=2):  # シートの行番号
            try:
                url = row[2] if len(row) > 2 else ""
                comment = row[3] if len(row) > 3 else ""

                if not url or not comment.strip():
                    print(f"Row {idx}: URL/コメントが空のためスキップ")
                    continue

                driver.get(url)
                print(f"\nRow {idx}: アクセス → {url}")

                if not wait_item_loaded(driver, timeout=25):
                    print(f"Row {idx}: ⚠️ 商品ページ読み込み失敗")
                    save_debug(driver, f"load_timeout_row{idx}")
                    mark_fail(worksheet, idx, status_col, "読み込み失敗")
                    continue

                expand_more_comments_if_any(driver)

                # 投稿前の件数
                before = get_comment_count(driver)

                # コメント欄探索
                area = None
                for attempt in range(1, 4):
                    area = find_comment_textarea(driver)
                    if area:
                        break
                    print(f"Row {idx}: コメント欄検出失敗 {attempt}/3 → スクロール再試行")
                    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                    time.sleep(0.8)

                if not area:
                    print(f"Row {idx}: ❌ コメント欄未検出")
                    save_debug(driver, f"no_textarea_row{idx}")
                    mark_fail(worksheet, idx, status_col, "コメント欄なし")
                    continue

                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", area)
                WebDriverWait(driver, 5).until(EC.element_to_be_clickable(area))
                try:
                    area.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", area)
                time.sleep(0.1)
                try:
                    area.clear()
                except Exception:
                    pass
                area.send_keys(comment)
                driver.execute_script("arguments[0].dispatchEvent(new Event('input', {bubbles:true}));", area)
                print("📝 コメント入力完了")

                # 送信ボタン
                try:
                    btn = find_submit_button(driver, timeout=10)
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
                        print(f"送信クリック失敗({how}): {e}")
                        time.sleep(0.2)

                if not clicked:
                    print(f"Row {idx}: ❌ 送信クリックに失敗")
                    save_debug(driver, f"post_clickfail_row{idx}")
                    mark_fail(worksheet, idx, status_col, "クリック失敗")
                    continue

                # 反映確認
                ok = verify_posted(driver, comment_text=comment, before_count=before, timeout=18)
                if ok:
                    print(f"Row {idx}: ✅ 投稿完了（反映確認済）")
                    # 成功時も記録したければ↓
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
                # 再起動で継続
                try:
                    driver.quit()
                except Exception:
                    pass
                driver = create_driver()
                inject_cookies(driver)
                wait = WebDriverWait(driver, 15)
                continue
            except Exception as e:
                print(f"Row {idx}: 予期せぬ例外 → {e}\n{traceback.format_exc()}")
                save_debug(driver, f"unexpected_row{idx}")
                mark_fail(worksheet, idx, status_col, "例外")
                continue

        print("✅ 全コメント投稿処理 完了")

    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()
