# -*- coding: utf-8 -*-
"""
GitHub Actions 対応版（メンズ）
- 毎回ユニークな Chrome プロファイルを使用して衝突回避
- ヘッドレスは 1 行のコメントアウトで ON/OFF 切替
- Cookie は MERCARI_COOKIES_PATH（Secrets から渡す）を注入
- スプレッドシートは GOOGLE_APPLICATION_CREDENTIALS による認証
- 失敗時に debug/*.png, *.html を保存（Actions の Artifact で取得）
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
import tempfile
import shutil
import atexit

# =========================
# 定数・パス
# =========================
REPO_ROOT = Path(__file__).resolve().parents[1]
DEBUG_DIR = REPO_ROOT / "debug"
DEBUG_DIR.mkdir(parents=True, exist_ok=True)

COOKIES_PATH = os.environ.get("MERCARI_COOKIES_PATH", str(REPO_ROOT / "mercari_cookies.json"))

SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/1E0XCjvoEriGnBU8dhMro0bC464JJ5hOmiIZUrZoQal8/edit"
TARGET_SHEET   = "メルカリコメント投稿"   # ← メンズ用

# =========================
# ユーティリティ
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
# Chrome 起動（毎回ユニークな user-data-dir）
# =========================
def create_driver():
    opts = Options()

    # ✅ ヘッドレス切替（通常は ON、デバッグ時はこの行をコメントアウト）
    #opts.add_argument("--headless=new")

    # 衝突回避のための一時プロファイル
    profile_dir = tempfile.mkdtemp(prefix="mercari_profile_")
    opts.add_argument(f"--user-data-dir={profile_dir}")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")

    # CI 安定化オプション
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")

    driver = webdriver.Chrome(options=opts)
    driver.set_page_load_timeout(60)

    # 終了時にプロファイル掃除
    def _cleanup():
        try:
            shutil.rmtree(profile_dir, ignore_errors=True)
        except Exception:
            pass
    atexit.register(_cleanup)

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
        driver.get("https://jp.mercari.com/")
        time.sleep(1.0)
        ok = 0
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
                ok += 1
            except Exception:
                pass
        print(f"🍪 Cookie 注入完了: {ok} 件")
        return ok > 0
    except Exception as e:
        print("⚠️ Cookie注入エラー:", e)
        return False

def check_logged_in(driver, timeout=6):
    try:
        WebDriverWait(driver, timeout).until_not(
            EC.presence_of_element_located((By.XPATH, "//a[contains(@href,'/signin') or contains(.,'ログイン')]"))
        )
        return True
    except TimeoutException:
        return False

# =========================
# オーバーレイ除去
# =========================
def close_overlays(driver):
    for xp in [
        "//button[contains(.,'同意') or contains(.,'許可') or contains(.,'閉じる') or @aria-label='閉じる']",
        "//*[@role='dialog']//button",
    ]:
        try:
            el = WebDriverWait(driver, 2).until(EC.element_to_be_clickable((By.XPATH, xp)))
            driver.execute_script("arguments[0].click();", el)
            time.sleep(0.2)
        except Exception:
            pass
    driver.execute_script("""
      for (const e of document.querySelectorAll('*')) {
        const s = getComputedStyle(e);
        if ((s.position==='fixed' || s.position==='sticky') && e.offsetHeight>60 && e.offsetWidth>200) {
          e.style.display='none';
        }
      }
    """)

# =========================
# コメント欄検出・送信判定
# =========================
SUBMIT_XPATHS = [
    "//form//button[@type='submit' and contains(normalize-space(),'コメントを送信')]",
    "//button[@type='submit' and contains(normalize-space(),'コメント')]",
    "//form//button[@type='submit']",
]

def find_comment_textarea_stronger(driver, timeout=8):
    end = time.time() + timeout
    while time.time() < end:
        for xp in [
            "//label[contains(., 'コメント')]",
            "//*[contains(., 'コメント') and not(contains(., 'もっと')) and (self::button or self::div or self::span)]",
        ]:
            for el in driver.find_elements(By.XPATH, xp):
                try:
                    if el.is_displayed():
                        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                        el.click(); time.sleep(0.2)
                except Exception:
                    pass

        for by, sel in [
            (By.CSS_SELECTOR, "#item-info textarea"),
            (By.CSS_SELECTOR, "form textarea"),
            (By.XPATH, "//textarea[not(@disabled)]"),
            (By.XPATH, "//textarea[contains(@placeholder,'コメント') or contains(@aria-label,'コメント')]"),
            (By.XPATH, "//*[@data-testid='comment']//textarea"),
        ]:
            for t in driver.find_elements(by, sel):
                try:
                    if t.is_displayed() and t.is_enabled():
                        return t
                except Exception:
                    pass
        time.sleep(0.2)
    return None

def find_submit_button(driver, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        for xp in SUBMIT_XPATHS:
            for b in driver.find_elements(By.XPATH, xp):
                try:
                    if b.is_displayed() and b.is_enabled():
                        return b
                except Exception:
                    pass
        time.sleep(0.2)
    raise TimeoutException("送信ボタンが見つかりません")

def get_comment_blocks(driver):
    return driver.find_elements(
        By.XPATH,
        "//*[(@data-testid='comment' or contains(@class,'CommentItem') or contains(@class,'comment'))]"
    )

def verify_posted(driver, comment_text: str, before_count: int, timeout=18):
    end = time.time() + timeout
    seen_toast = False
    partial = comment_text.strip()[:20]
    while time.time() < end:
        if len(get_comment_blocks(driver)) > before_count:
            return True
        for pat in ["コメントを送信", "コメントを投稿", "コメントを送信しました", "コメントを投稿しました"]:
            if driver.find_elements(By.XPATH, f"//*[contains(normalize-space(), '{pat}')]"):
                seen_toast = True
        ta_candidates = driver.find_elements(By.TAG_NAME, "textarea")
        if ta_candidates:
            if all((ta.get_attribute("value") or "").strip() == "" for ta in ta_candidates):
                if seen_toast:
                    return True
        blocks = get_comment_blocks(driver)
        if blocks:
            last_txt = (blocks[-1].text or "").strip()
            if partial and partial in last_txt:
                return True
        time.sleep(0.3)
    return False

# =========================
# スプレッドシート
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
    data   = rows[1:] if len(rows) > 1 else []
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
# メイン
# =========================
def main():
    driver = create_driver()
    try:
        inject_cookies_if_available(driver)
        driver.get("https://jp.mercari.com/")
        time.sleep(0.8)
        if not check_logged_in(driver):
            print("⚠️ 未ログインの可能性があります（Cookie期限切れ等）")

        worksheet, data, status_col = load_sheet_rows()

        for idx, row in enumerate(data, start=2):  # シート上の行番号
            try:
                url     = row[2] if len(row) > 2 else ""
                comment = row[3] if len(row) > 3 else ""

                if not url or not comment.strip():
                    print(f"Row {idx}: URL/コメントが空のためスキップ")
                    continue

                driver.get(url)
                print(f"\nRow {idx}: アクセス → {url}")
                close_overlays(driver)

                # コメント欄探索
                before = len(get_comment_blocks(driver))
                area = None
                for attempt in range(1, 4):
                    area = find_comment_textarea_stronger(driver, timeout=3)
                    if area:
                        break
                    print(f"Row {idx}: コメント欄検出失敗 {attempt}/3 → スクロール再試行")
                    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                    time.sleep(0.8)
                    close_overlays(driver)

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
                        clicked = True
                        print("🚀 送信ボタンをクリック")
                        break
                    except Exception as e:
                        print(f"送信クリック失敗({how}): {e}")
                        time.sleep(0.2)
                if not clicked:
                    print(f"Row {idx}: ❌ 送信クリックに失敗")
                    save_debug(driver, f"post_clickfail_row{idx}")
                    mark_fail(worksheet, idx, status_col, "クリック失敗")
                    continue

                ok = verify_posted(driver, comment_text=comment, before_count=before, timeout=18)
                if ok:
                    print(f"Row {idx}: ✅ 投稿完了（反映確認済）")
                else:
                    print(f"Row {idx}: ❌ 投稿失敗（反映確認できず）")
                    save_debug(driver, f"post_fail_row{idx}")
                    mark_fail(worksheet, idx, status_col, "反映確認できず")

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
                try:
                    driver.quit()
                except Exception:
                    pass
                driver = create_driver()
                inject_cookies_if_available(driver)
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
