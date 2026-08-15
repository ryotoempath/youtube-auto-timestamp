import os
import json
import base64
import time
import subprocess
import urllib.request
import cv2

CHANNEL_PLAYLIST_URL = "https://www.youtube.com/playlist?list=UUsei55iBwnVsqClwwpvmzrw"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GAS_WEBAPP_URL = os.environ.get("GAS_WEBAPP_URL", "https://script.google.com/macros/s/AKfycbx-Si2brMAVjlaQyRnoOmqeu8lO-Tv2t1xbwUytG3bWPJR6PCtJxUE8g0A53uz61k_vRA/exec")
AUTO_SECRET = os.environ.get("AUTO_SECRET", "ryoto_timestamp_secret")

def get_latest_completed_video():
    """アーカイブ処理中の動画を回避し、最新の配信完了アーカイブを確実に取得"""
    cmd = [
        "yt-dlp",
        "--user-agent", "Mozilla/5.0",
        "--extractor-args", "youtube:player_client=android,web",
        "--dump-json",
        "https://www.youtube.com/@RyotoV/streams"
    ]
    try:
        out = subprocess.check_output(cmd)
        lines = out.decode("utf-8", errors="ignore").strip().split("\n")
        for line in lines:
            if not line.strip():
                continue
            entry = json.loads(line)
            # 生配信中ではなく、動画が完了しているものを選択
            if entry.get("was_live") or entry.get("duration"):
                return entry
    except Exception as e:
        print("ストリーム一覧取得エラー:", e)
    return None

def main():
    print("=== 【りょーとV 全自動タイムスタンプ解析・更新システム】起動 ===")
    
    if not GEMINI_API_KEY:
        print("エラー: GEMINI_API_KEY が設定されていません。")
        return
        
    latest_video = get_latest_completed_video()
    if not latest_video:
        print("最新の配信動画が見つかりませんでした。")
        return
        
    video_id = latest_video.get("id")
    video_title = latest_video.get("title", "")
    
    print(f"最新の配信アーカイブを検知しました: ID={video_id} | Title={video_title}")
    
    # 2. 動画ストリームの一括取得
    local_video_path = f"stream_{video_id}.mp4"
    if not os.path.exists(local_video_path):
        print(f"動画ストリームを取得中 ({video_id})...")
        dl_cmd = [
            "yt-dlp",
            "--user-agent", "Mozilla/5.0",
            "--extractor-args", "youtube:player_client=android,web",
            "-f", "18/140/b",
            "-o", local_video_path,
            f"https://www.youtube.com/watch?v={video_id}"
        ]
        subprocess.run(dl_cmd, check=True)
    
    # 3. OpenCV によるキーフレーム抽出 (20分刻み)
    cap = cv2.VideoCapture(local_video_path)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    duration_sec = total_frames / fps if total_frames > 0 else 12000
    
    print(f"実測再生時間: {duration_sec / 3600:.2f}時間 ({duration_sec:.1f}秒)")
    
    interval_sec = 1200
    extracted_frames = []
    sec = 0
    while sec < duration_sec:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(sec * fps))
        ret, frame = cap.read()
        if ret:
            _, buf = cv2.imencode(".jpg", frame)
            b64 = base64.b64encode(buf.tobytes()).decode("utf-8")
            hrs, mts, scs = int(sec // 3600), int((sec % 3600) // 60), int(sec % 60)
            extracted_frames.append({"time": f"{hrs:02d}:{mts:02d}:{scs:02d}", "b64": b64})
        sec += interval_sec
    cap.release()
    
    print(f"抽出コマ数: {len(extracted_frames)}コマ")
    
    # 4. 学習済み【りょーと直筆スタイル】による Gemini AI マルチモーダル解析
    gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent?key={GEMINI_API_KEY}"
    
    prompt = f"""以下は最新のライブ配信『{video_title}』（動画ID: {video_id}）の実際のゲーム配信画面画像群です。

【重要：りょーと直筆公式プロンプトルール】
以下の「お手本スタイル」を100%真似してタイムスタンプを作成してください。

[お手本サンプル1]
- 00:00:00 待機画面
- 00:02:15 配信開始（日課）
- 00:34:00 双炎確認
- 00:46:00 炎舞型味見
- 01:24:00 レグティニス遺跡
- 02:21:00 迷霧の森 攻略
- 04:02:00 まとめ＆雑談

[お手本サンプル2]
- 00:00:00 待機画面
- 00:02:30 配信開始（日課）
- 00:38:00 双炎型確認
- 01:00:00 ワールドレイド
- 01:25:00 M5工場（首飾り厳選）
- 02:30:00 装備調整＆雑談
- 03:25:00 まとめ＆雑談

[制約]
- 見出しの文字数は4〜12文字程度で極めてシンプルに抑えること。
- 大げさに「〇〇検証」と言わず、「味見」「確認」「日課」「厳選」「M5煌墓」「蝕ティナ」「雑談」などの自然な言葉選びにすること。
- 画面UI（ダンジョン名、ドロップ画面、ステータス画面、雑談画面等）と時間に合致させて、全体で6〜8項目程度に絞ってタイムスタンプを出力すること。"""

    parts = [{"text": prompt}]
    for item in extracted_frames:
        parts.append({"text": f"\n--- タイムコード [{item['time']}] の実際の配信画面画像 ---"})
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": item["b64"]}})

    payload = {"contents": [{"parts": parts}]}
    headers = {"Content-Type": "application/json"}

    result_timestamps = ""
    for attempt in range(1, 5):
        try:
            req = urllib.request.Request(gemini_url, data=json.dumps(payload).encode("utf-8"), headers=headers)
            with urllib.request.urlopen(req) as res:
                json_res = json.loads(res.read().decode("utf-8"))
                result_timestamps = json_res["candidates"][0]["content"]["parts"][0]["text"].strip()
                print("=== 生成完了したタイムスタンプ ===")
                print(result_timestamps)
                break
        except Exception as e:
            print(f"Gemini API 試行{attempt} エラー:", e)
            time.sleep(4)

    if not result_timestamps:
        print("タイムスタンプの生成に失敗しました。")
        return

    # 一時動画ファイルのクレンジング
    if os.path.exists(local_video_path):
        os.remove(local_video_path)

    # 5. GAS WebApp へ更新リクエストを送信
    if GAS_WEBAPP_URL:
        print(f"GAS WebApp へ自動送信中 ({GAS_WEBAPP_URL})...")
        gas_payload = {
            "videoId": video_id,
            "timestampsText": result_timestamps,
            "secret": AUTO_SECRET
        }
        gas_req = urllib.request.Request(GAS_WEBAPP_URL, data=json.dumps(gas_payload).encode("utf-8"), headers=headers)
        with urllib.request.urlopen(gas_req) as gas_res:
            res_data = json.loads(gas_res.read().decode("utf-8"))
            print("GAS更新レスポンス:", res_data)
            print("🎉 最新配信アーカイブの概要欄全自動更新が成功しました！")

if __name__ == "__main__":
    main()
