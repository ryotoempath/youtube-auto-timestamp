/**
 * りょーとV Channel - 全自動タイムスタンプ生成システム (GAS完結版)
 *
 * GitHub Actions不要・完全クラウド動作版
 * Google自身のサーバーで動くため YouTube ブロック問題を根本解決
 *
 * ■ 初期設定
 *  1. このスクリプトのプロパティに ANTHROPIC_API_KEY を設定
 *  2. setupTrigger() を一度だけ手動実行 → 3時間ごとの自動実行が開始
 */

var CHANNEL_ID   = 'UCsei55iBwnVsqClwwpvmzrw';
var CLAUDE_MODEL = 'claude-sonnet-5';

// ============================================================
// メイン処理（タイマー / 手動から呼ばれる）
// ============================================================
function autoTimestampMain() {
  Logger.log('=== 【りょーとV 全自動タイムスタンプシステム】起動 ===');

  // Step 1: 最新の完了済みライブ配信を取得
  var video = getLatestCompletedStream();
  if (!video) {
    Logger.log('最新の配信動画が見つかりませんでした。終了します。');
    return;
  }
  Logger.log('動画ID  : ' + video.id);
  Logger.log('タイトル: ' + video.title);
  Logger.log('再生時間: ' + (video.durationSec / 3600).toFixed(2) + '時間');

  // Step 2: 重複スキップ
  if (video.description && video.description.indexOf('【タイムスタンプ】') !== -1) {
    Logger.log('[Skip] 既にタイムスタンプが存在します。終了します。');
    return;
  }

  // Step 3: 字幕取得（2段階フォールバック）
  Logger.log('[Step 2] 字幕を取得中...');
  var transcript = getTranscriptTimedtext(video.id);

  if (!transcript || transcript.length === 0) {
    Logger.log('  timedtext API失敗。Captions API（OAuth）を試みます...');
    transcript = getTranscriptCaptionsApi(video.id);
  }

  if (!transcript || transcript.length === 0) {
    Logger.log('  字幕の取得に失敗しました。次回の自動実行をお待ちください。');
    Logger.log('  ※ライブ配信直後は字幕生成に数時間かかる場合があります。');
    return;
  }
  Logger.log('  字幕取得完了: ' + transcript.length + ' エントリ');

  // Step 4: サンプリング
  Logger.log('[Step 3] 字幕をサンプリング中...');
  var sampled = sampleTranscript(transcript);
  Logger.log('  サンプリング後文字数: ' + sampled.length + ' 文字');

  // Step 5: Claude API でタイムスタンプ生成
  Logger.log('[Step 4] Claude (' + CLAUDE_MODEL + ') でタイムスタンプを生成中...');
  var timestamps = callClaude(video.title, video.id, video.durationSec, sampled);
  if (!timestamps) {
    Logger.log('  タイムスタンプ生成に失敗しました。終了します。');
    return;
  }
  Logger.log('\n=== 生成されたタイムスタンプ ===\n' + timestamps);

  // Step 6: 概要欄更新
  Logger.log('[Step 5] 概要欄を更新中...');
  updateDescription(video.id, video.snippet, timestamps);

  Logger.log('=== 全処理完了 ===');
}

// ============================================================
// Step 1: 最新の完了済みライブ配信を取得
// ============================================================
function getLatestCompletedStream() {
  var searchRes = YouTube.Search.list('id,snippet', {
    channelId: CHANNEL_ID,
    eventType: 'completed',
    type: 'video',
    order: 'date',
    maxResults: 5
  });

  if (!searchRes.items || searchRes.items.length === 0) return null;
  var videoId = searchRes.items[0].id.videoId;

  var detailRes = YouTube.Videos.list('snippet,contentDetails', { id: videoId });
  if (!detailRes.items || detailRes.items.length === 0) return null;

  var item = detailRes.items[0];
  return {
    id:          videoId,
    title:       item.snippet.title,
    description: item.snippet.description || '',
    snippet:     item.snippet,
    durationSec: parseIsoDuration(item.contentDetails.duration)
  };
}

// ============================================================
// Step 3a: timedtext API で字幕取得（認証不要・高速）
// ============================================================
function getTranscriptTimedtext(videoId) {
  try {
    var url = 'https://www.youtube.com/api/timedtext?lang=ja&v=' + videoId + '&fmt=json3';
    var res = UrlFetchApp.fetch(url, { muteHttpExceptions: true });
    if (res.getResponseCode() !== 200) return null;

    var data = JSON.parse(res.getContentText());
    var events = data.events || [];
    var entries = [];

    for (var i = 0; i < events.length; i++) {
      var ev = events[i];
      if (!ev.segs) continue;
      var text = ev.segs.map(function(s) { return s.utf8 || ''; }).join('').trim();
      if (text) {
        entries.push({ start: (ev.tStartMs || 0) / 1000, text: text });
      }
    }

    Logger.log('  timedtext API: ' + entries.length + ' エントリ取得');
    return entries.length > 0 ? entries : null;
  } catch(e) {
    Logger.log('  timedtext APIエラー: ' + e.message);
    return null;
  }
}

// ============================================================
// Step 3b: YouTube Captions API で字幕取得（OAuth認証・チャンネルオーナー専用）
// ============================================================
function getTranscriptCaptionsApi(videoId) {
  try {
    var captionsList = YouTube.Captions.list('id,snippet', videoId);
    if (!captionsList.items || captionsList.items.length === 0) return null;

    // 自動生成（asr）または手動字幕（standard）を優先
    var captionId = null;
    for (var i = 0; i < captionsList.items.length; i++) {
      var kind = captionsList.items[i].snippet.trackKind;
      if (kind === 'asr' || kind === 'standard') {
        captionId = captionsList.items[i].id;
        break;
      }
    }
    if (!captionId) return null;

    // 字幕コンテンツをダウンロード
    var token = ScriptApp.getOAuthToken();
    var captionUrl = 'https://www.googleapis.com/youtube/v3/captions/' + captionId + '?tfmt=json3';
    var captionRes = UrlFetchApp.fetch(captionUrl, {
      headers: { Authorization: 'Bearer ' + token },
      muteHttpExceptions: true
    });

    if (captionRes.getResponseCode() !== 200) return null;

    var captionData = JSON.parse(captionRes.getContentText());
    var entries = [];
    var events = captionData.events || [];

    for (var j = 0; j < events.length; j++) {
      var ev = events[j];
      if (!ev.segs) continue;
      var text = ev.segs.map(function(s) { return s.utf8 || ''; }).join('').trim();
      if (text) {
        entries.push({ start: (ev.tStartMs || 0) / 1000, text: text });
      }
    }

    Logger.log('  Captions API: ' + entries.length + ' エントリ取得');
    return entries.length > 0 ? entries : null;
  } catch(e) {
    Logger.log('  Captions APIエラー: ' + e.message);
    return null;
  }
}

// ============================================================
// Step 4: 字幕サンプリング（20分ブロック）
// ============================================================
function sampleTranscript(entries) {
  var INTERVAL = 1200; // 20分（秒）
  var blocks = [];
  var blockStart = 0;
  var blockTexts = [];

  function fmtSec(s) {
    var h = Math.floor(s / 3600);
    var m = Math.floor((s % 3600) / 60);
    var sec = Math.floor(s % 60);
    return (h < 10 ? '0' : '') + h + ':' + (m < 10 ? '0' : '') + m + ':' + (sec < 10 ? '0' : '') + sec;
  }

  for (var i = 0; i < entries.length; i++) {
    var entry = entries[i];
    if (entry.start >= blockStart + INTERVAL) {
      if (blockTexts.length > 0) {
        blocks.push('[' + fmtSec(blockStart) + '頃] ' + blockTexts.slice(0, 12).join('／'));
      }
      blockStart = Math.floor(entry.start / INTERVAL) * INTERVAL;
      blockTexts = [entry.text];
    } else {
      blockTexts.push(entry.text);
    }
  }
  if (blockTexts.length > 0) {
    blocks.push('[' + fmtSec(blockStart) + '頃] ' + blockTexts.slice(0, 12).join('／'));
  }

  var result = blocks.join('\n');
  if (result.length > 14000) result = result.substring(0, 14000) + '\n...(以降省略)';
  return result;
}

// ============================================================
// Step 5: Claude API でタイムスタンプ生成
// ============================================================
function callClaude(title, videoId, durationSec, sampledTranscript) {
  var apiKey = PropertiesService.getScriptProperties().getProperty('ANTHROPIC_API_KEY') || '';
  if (!apiKey) {
    Logger.log('  ANTHROPIC_API_KEY が未設定です。');
    return null;
  }

  var durationH = (durationSec / 3600).toFixed(1);
  var prompt = 'あなたはYouTubeライブ配信のタイムスタンプ作成の専門家です。\n'
    + '以下の配信アーカイブの字幕データを分析し、視聴者が内容を把握しやすい日本語タイムスタンプを作成してください。\n\n'
    + '配信タイトル: 『' + title + '』\n'
    + '動画ID: ' + videoId + '\n'
    + '配信時間: 約' + durationH + '時間\n\n'
    + '【字幕データ（20分ごとブロック）】\n'
    + sampledTranscript + '\n\n'
    + '【タイムスタンプ作成ルール】\n'
    + '1. 「HH:MM:SS タイトル」形式のみで出力すること（例: 00:02:15 配信開始）\n'
    + '2. 00:00:00 から始めること\n'
    + '3. 30分置き程度（内容区切りに応じて20〜40分で調整可）で 6〜9 項目\n'
    + '4. 各項目は字幕データの実際の発言・内容を元に具体的に書くこと\n'
    + '5. スタレゾ（スターレゾナンス）の固有名詞はそのまま使用\n'
    + '6. タイムスタンプ行のみを出力し、前置きや説明は不要';

  var payload = {
    model: CLAUDE_MODEL,
    max_tokens: 1024,
    messages: [{ role: 'user', content: prompt }]
  };

  var options = {
    method: 'post',
    headers: {
      'x-api-key': apiKey,
      'anthropic-version': '2023-06-01',
      'Content-Type': 'application/json'
    },
    payload: JSON.stringify(payload),
    muteHttpExceptions: true
  };

  try {
    var res = UrlFetchApp.fetch('https://api.anthropic.com/v1/messages', options);
    if (res.getResponseCode() !== 200) {
      Logger.log('  Claude APIエラー: ' + res.getContentText());
      return null;
    }
    var data = JSON.parse(res.getContentText());
    return data.content[0].text.trim();
  } catch(e) {
    Logger.log('  Claude API例外: ' + e.message);
    return null;
  }
}

// ============================================================
// Step 6: 概要欄の【タイムスタンプ】ブロックを安全に更新
// ============================================================
function updateDescription(videoId, snippet, timestamps) {
  var headerTag = '【タイムスタンプ】';
  var currentDesc = snippet.description || '';
  var newDesc;

  if (currentDesc.indexOf(headerTag) !== -1) {
    var parts = currentDesc.split(headerTag);
    var before = parts[0];
    var afterRaw = parts[1];
    var nextIdx = afterRaw.search(/\n\s*[【▼◆■]/);
    var after = nextIdx !== -1 ? afterRaw.substring(nextIdx) : '';
    newDesc = before.trim() + '\n\n' + headerTag + '\n' + timestamps.trim() + '\n\n' + after.trim();
  } else {
    newDesc = headerTag + '\n' + timestamps.trim() + '\n\n' + currentDesc.trim();
  }

  snippet.description = newDesc;
  YouTube.Videos.update({ id: videoId, snippet: snippet }, 'snippet');
  Logger.log('  ✅ 概要欄を更新しました: ' + videoId);
}

// ============================================================
// ユーティリティ
// ============================================================
function parseIsoDuration(duration) {
  var match = (duration || '').match(/PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?/);
  if (!match) return 12000;
  return (parseInt(match[1] || 0) * 3600
        + parseInt(match[2] || 0) * 60
        + parseInt(match[3] || 0));
}

// ============================================================
// 【初回のみ実行】3時間ごとのタイマーをセットアップ
// ============================================================
function setupTrigger() {
  // 既存トリガーをすべて削除
  ScriptApp.getProjectTriggers().forEach(function(t) {
    ScriptApp.deleteTrigger(t);
  });
  // 3時間ごとのトリガーを作成
  ScriptApp.newTrigger('autoTimestampMain')
    .timeBased()
    .everyHours(3)
    .create();
  Logger.log('✅ 3時間ごとの自動実行トリガーを設定しました。');
}

// ============================================================
// WebApp受信（GitHub Actions からの手動トリガー互換）
// ============================================================
function doPost(e) {
  try {
    var contents = JSON.parse(e.postData.contents);
    var apiKeySecret = contents.secret;
    var expectedSecret = PropertiesService.getScriptProperties().getProperty('AUTO_SECRET') || 'ryoto_timestamp_secret';
    if (apiKeySecret !== expectedSecret) {
      return ContentService.createTextOutput(JSON.stringify({ status: 'error', message: 'Unauthorized' }))
        .setMimeType(ContentService.MimeType.JSON);
    }

    // timestampsText が送られてきた場合は旧来の直接書き込みモード
    if (contents.videoId && contents.timestampsText) {
      var videoResponse = YouTube.Videos.list('snippet', { id: contents.videoId });
      if (!videoResponse.items || videoResponse.items.length === 0) {
        return ContentService.createTextOutput(JSON.stringify({ status: 'error', message: 'Video not found' }))
          .setMimeType(ContentService.MimeType.JSON);
      }
      var snippet = videoResponse.items[0].snippet;
      updateDescription(contents.videoId, snippet, contents.timestampsText);
      return ContentService.createTextOutput(JSON.stringify({ status: 'success', videoId: contents.videoId }))
        .setMimeType(ContentService.MimeType.JSON);
    }

    // トリガーモード: フルパイプラインを実行
    autoTimestampMain();
    return ContentService.createTextOutput(JSON.stringify({ status: 'triggered' }))
      .setMimeType(ContentService.MimeType.JSON);

  } catch(error) {
    Logger.log('エラー: ' + error.toString());
    return ContentService.createTextOutput(JSON.stringify({ status: 'error', message: error.toString() }))
      .setMimeType(ContentService.MimeType.JSON);
  }
}

function doGet(e) {
  return ContentService.createTextOutput(JSON.stringify({ status: 'ok', model: CLAUDE_MODEL }))
    .setMimeType(ContentService.MimeType.JSON);
}
