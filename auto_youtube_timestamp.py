import os
import sys
import json
import re
import subprocess
import urllib.request
import urllib.parse

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')

CHANNEL_ID        = "UCsei55iBwnVsqClwwpvmzrw"
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
YOUTUBE_API_KEY   = os.environ.get("YOUTUBE_API_KEY", "")
GAS_WEBAPP_URL    = os.environ.get("GAS_WEBAPP_URL", "")
AUTO_SECRET       = os.environ.get("AUTO_SECRET", "ryoto_timestamp_secret")
CLAUDE_MODEL      = "claude-sonnet-5"

# ──────────────────────────────────────────────
# Step 1: 最新配信アーカイブ取得（YouTube Data API v3）
# ──────────────────────────────────────────────
def parse_iso_duration(duration: str) -> float:
    """ISO 8601 duration (PT1H30M45S) を秒数に変換"""
    m = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', duration or "")
    if not m:
        return 12000.0
    h  = int(m.group(1) or 0)
    mi = int(m.group(2) or 0)
    s  = int(m.group(3) or 0)
    return float(h * 3600 + mi * 60 + s)

def get_latest_completed_video():
    """
    YouTube Data API v3 で最新の完了済みライブ配信アーカイブを取得する。
    GitHub Actions の IP ブロック問題を回避するために公式 API を使用。
    """
    if not YOUTUBE_API_KEY:
        print("  YOUTUBE_API_KEY が未設定です。")
        return None

    # ① 最新の完了済みライブ配信を検索（最大5件）
    search_params = urllib.parse.urlencode({
        "part": "snippet",
        "channelId": CHANNEL_ID,
        "eventType": "completed",
        "type": "video",
        "order": "date",
        "maxResults": "5",
        "key": YOUTUBE_API_KEY
    })
    search_url = f"https://www.googleapis.com/youtube/v3/search?{search_params}"

    try:
        with urllib.request.urlopen(search_url, timeout=15) as res:
            search_data = json.loads(res.read().decode())
    except Exception as e:
        print(f"  YouTube Search API エラー: {e}")
        return None

    items = search_data.get("items", [])
    if not items:
        print("  完了済みライブ配信が見つかりませんでした。")
        return None

    video_id = items[0]["id"]["videoId"]

    # ② 動画の詳細情報を取得（duration / description）
    detail_params = urllib.parse.urlencode({
        "part": "snippet,contentDetails",
        "id": video_id,
        "key": YOUTUBE_API_KEY
    })
    detail_url = f"https://www.googleapis.com/youtube/v3/videos?{detail_params}"

    try:
        with urllib.request.urlopen(detail_url, timeout=15) as res:
            detail_data = json.loads(res.read().decode())
    except Exception as e:
        print(f"  YouTube Videos API エラー: {e}")
        return None

    if not detail_data.get("items"):
        print(f"  動画詳細の取得に失敗しました: {video_id}")
        return None

    item         = detail_data["items"][0]
    snippet      = item["snippet"]
    duration_sec = parse_iso_duration(item["contentDetails"].get("duration", ""))

    return {
        "id":          video_id,
        "title":       snippet.get("title", ""),
        "description": snippet.get("description", ""),
        "duration":    duration_sec,
        "was_live":    True
    }

# ──────────────────────────────────────────────
# Step 2: 重複スキップ判定
# ──────────────────────────────────────────────
def already_has_timestamps(description: str) -> bool:
    """概要欄に既に【タイムスタンプ】ブロックがある場合は True を返す"""
    return "【タイムスタンプ】" in description

# ──────────────────────────────────────────────
# Step 3: 字幕取得（youtube-transcript-api）
# ──────────────────────────────────────────────
def fetch_transcript(video_id: str) -> list[tuple[float, str]] | None:
    """
    youtube-transcript-api v0.5 で日本語字幕を取得する。
    GitHub Actions の IP でブロックされにくく安定。
    戻り値: [(秒数, テキスト), ...] のリスト、失敗時は None
    """
    print(f"  字幕取得中 (video_id={video_id})...")
    try:
        from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound

        def _to_entries(raw) -> list[tuple[float, str]]:
            """dict形式(v0.5)・オブジェクト形式(v0.6+)どちらにも対応"""
            result = []
            for item in raw:
                text  = item['text']  if isinstance(item, dict) else item.text
                start = item['start'] if isinstance(item, dict) else item.start
                text  = (text or "").strip()
                if text:
                    result.append((float(start), text))
            return result

        try:
            raw = YouTubeTranscriptApi.get_transcript(video_id, languages=['ja'])
        except NoTranscriptFound:
            print("  日本語字幕なし。英語でフォールバック...")
            raw = YouTubeTranscriptApi.get_transcript(video_id, languages=['en'])

        entries = _to_entries(raw)
        print(f"  [OK] 字幕取得完了: {len(entries)} エントリ")
        return entries
    except Exception as e:
        print(f"  字幕取得エラー: {e}")
        return None


def sample_transcript(entries: list[tuple[float, str]],
                      interval_sec: int = 1200,
                      max_chars: int = 14000) -> str:
    """
    字幕エントリを interval_sec（デフォルト20分）ごとのブロックに区切り、
    各ブロックの先頭タイムスタンプと代表テキストをまとめてサンプリングする。

    Claude が「この時間帯にこの話題があった」と正確に把握できる形式で出力する。
    """
    if not entries:
        return ""

    blocks: list[str] = []
    block_start = 0
    block_texts: list[str] = []

    def fmt_sec(s: int) -> str:
        return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"

    for sec, text in entries:
        if sec >= block_start + interval_sec:
            if block_texts:
                header = fmt_sec(int(block_start))
                summary = "／".join(block_texts[:12])  # 最大12フレーズ
                blocks.append(f"[{header}頃] {summary}")
            block_start = (int(sec) // interval_sec) * interval_sec
            block_texts = [text]
        else:
            block_texts.append(text)

    # 最後のブロック
    if block_texts:
        header = fmt_sec(int(block_start))
        blocks.append(f"[{header}頃] " + "／".join(block_texts[:12]))

    result = "\n".join(blocks)
    if len(result) > max_chars:
        result = result[:max_chars] + "\n...(以降省略)"
    return result

# ──────────────────────────────────────────────
# Step 5: Claude API でタイムスタンプ生成
# ──────────────────────────────────────────────
def call_claude_api(video_title: str, video_id: str,
                    duration_sec: float, sampled_transcript: str) -> str | None:
    """
    Claude API にサンプリング済み字幕を渡し、内容一致のタイムスタンプを生成する。
    """
    if not ANTHROPIC_API_KEY:
        print("  ANTHROPIC_API_KEY が未設定です。")
        return None

    duration_h = duration_sec / 3600

    prompt = f"""あなたはYouTubeライブ配信のタイムスタンプ作成の専門家です。
以下の配信アーカイブの**実際の字幕データ（20分ブロック区切り）**を分析し、
視聴者が内容を把握しやすい日本語タイムスタンプを作成してください。

配信タイトル: 『{video_title}』
動画ID: {video_id}
配信時間: 約{duration_h:.1f}時間

【字幕データ（20分ごとブロック）】
{sampled_transcript}

【タイムスタンプ作成ルール】
1. 「HH:MM:SS タイトル」形式のみで出力すること（例: 00:02:15 配信開始）
2. 00:00:00 から始めること
3. 30分置き程度（内容区切りに応じて20〜40分で調整可）で 6〜9 項目
4. 各項目は字幕データの「その時間帯の実際の発言・内容」を元に具体的に書くこと
5. スタレゾ（スターレゾナンス）の固有名詞（双炎・煌墓・M5等）はそのまま使用
6. タイムスタンプ行のみを出力し、前置きや説明・markdown記号は不要

【出力例】
00:00:00 待機画面
00:02:15 配信開始・日課周回
00:34:00 双炎確認
01:24:00 レグティニス遺跡
02:21:00 M5煌墓 攻略
04:02:00 まとめ＆雑談"""

    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}]
    }
    headers = {
        "Content-Type": "application/json",
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01"
    }

    try:
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=60) as res:
            json_res = json.loads(res.read().decode("utf-8"))
            result = json_res["content"][0]["text"].strip()
            return result
    except Exception as e:
        print("  Claude APIエラー:", e)
        return None

# ──────────────────────────────────────────────
# Step 6: GAS WebApp へ POST
# ──────────────────────────────────────────────
def post_to_gas(video_id: str, timestamps_text: str) -> bool:
    """生成したタイムスタンプを GAS WebApp に POST して概要欄を更新する"""
    if not GAS_WEBAPP_URL:
        print("  GAS_WEBAPP_URL が未設定です。")
        return False

    payload = {
        "videoId": video_id,
        "timestampsText": timestamps_text,
        "secret": AUTO_SECRET
    }
    headers = {"Content-Type": "application/json"}

    try:
        req = urllib.request.Request(
            GAS_WEBAPP_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as res:
            result = json.loads(res.read().decode("utf-8"))
        if result.get("status") == "success":
            print(f"  ✅ 概要欄の更新に成功しました: {video_id}")
            return True
        else:
            print(f"  ❌ GAS エラー: {result.get('message', 'Unknown error')}")
            return False
    except Exception as e:
        print("  GAS POST エラー:", e)
        return False

# ──────────────────────────────────────────────
# メイン処理
# ──────────────────────────────────────────────
def main():
    print("=== 【りょーとV 全自動タイムスタンプ解析・更新システム】===")
    print(f"  使用モデル: {CLAUDE_MODEL}\n")

    # Step 1: 最新配信アーカイブ取得
    print("[Step 1] 最新配信アーカイブを検索中...")
    latest_video = get_latest_completed_video()
    if not latest_video:
        print("  最新の配信動画が見つかりませんでした。終了します。")
        return

    video_id    = latest_video.get("id")
    video_title = latest_video.get("title", "")
    duration_sec = float(latest_video.get("duration", 12000))
    description  = latest_video.get("description", "")

    print(f"  動画ID  : {video_id}")
    print(f"  タイトル: {video_title}")
    print(f"  再生時間: {duration_sec / 3600:.2f}時間")

    # Step 2: 重複スキップ
    if already_has_timestamps(description):
        print("\n[Skip] 概要欄に既に【タイムスタンプ】が存在します。処理をスキップします。")
        return

    # Step 3: 字幕取得（youtube-transcript-api）
    print("\n[Step 2] 字幕を取得中...")
    entries = fetch_transcript(video_id)
    if not entries:
        print("  字幕が取得できませんでした。終了します。")
        return

    # Step 4: サンプリング
    print("\n[Step 3] 字幕をサンプリング中...")
    sampled = sample_transcript(entries)
    print(f"  字幕エントリ数      : {len(entries)} 件")
    print(f"  サンプリング後文字数: {len(sampled)} 文字")

    # Step 5: Claude API でタイムスタンプ生成
    print(f"\n[Step 4] Claude ({CLAUDE_MODEL}) でタイムスタンプを生成中...")
    timestamps = call_claude_api(video_title, video_id, duration_sec, sampled)
    if not timestamps:
        print("  タイムスタンプ生成に失敗しました。終了します。")
        return

    print("\n=== 生成されたタイムスタンプ ===")
    print(timestamps)

    # Step 6: GAS 経由で概要欄を更新
    print("\n[Step 5] GAS 経由で概要欄を更新中...")
    post_to_gas(video_id, timestamps)

    print("\n=== 全処理完了 ===")


if __name__ == "__main__":
    main()
