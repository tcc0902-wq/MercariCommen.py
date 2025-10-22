import multiprocessing
import subprocess
import time

# 仮想環境の Python パス
python_path = "/Users/machidatatsuhiro/Desktop/ショートカット/python/myenv/bin/python"

# レディーススクリプト（user-data-dir=/tmp/mercari_ladies_profile を使う必要あり）


def run_script1():
    print("🚀 レディースコメント投稿 実行開始")
    subprocess.run(
        [python_path, "/Users/machidatatsuhiro/Desktop/ショートカット/python/メルカリレディースコメント投稿実行.py"])

# メンズスクリプト（user-data-dir=/tmp/mercari_mens_profile を使う必要あり）


def run_script2():
    print("⏳ メンズコメント投稿まで20秒待機...")
    time.sleep(20)
    print("🚀 メンズコメント投稿 実行開始")
    subprocess.run(
        [python_path, "/Users/machidatatsuhiro/Desktop/ショートカット/python/メルカリコメント投稿実行.py"])


if __name__ == "__main__":
    # プロセス作成
    p1 = multiprocessing.Process(target=run_script1)
    p2 = multiprocessing.Process(target=run_script2)

    # 並列に実行
    p1.start()
    p2.start()

    # 終了待機
    p1.join()
    p2.join()

    print("✅ 両方のスクリプトの実行が完了しました。")
