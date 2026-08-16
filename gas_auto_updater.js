/**
 * りょーとV Channel - 概要欄タイムスタンプ全自動更新 GAS WebApp
 * 
 * GitHub Actions / Python から送信されたタイムスタンプデータを受信し、
 * 最新のライブ配信アーカイブの概要欄を安全に更新します。
 */

function doPost(e) {
  try {
    const contents = JSON.parse(e.postData.contents);
    const videoId = contents.videoId;
    const timestampsText = contents.timestampsText;
    const apiKeySecret = contents.secret;
    
    // 安全認証チェック（簡易トークン照合）
    const expectedSecret = PropertiesService.getScriptProperties().getProperty('AUTO_SECRET') || 'ryoto_timestamp_secret';
    if (apiKeySecret !== expectedSecret) {
      return ContentService.createTextOutput(JSON.stringify({ status: 'error', message: 'Unauthorized secret' })).setMimeType(ContentService.MimeType.JSON);
    }
    
    if (!videoId || !timestampsText) {
      return ContentService.createTextOutput(JSON.stringify({ status: 'error', message: 'Missing videoId or timestampsText' })).setMimeType(ContentService.MimeType.JSON);
    }
    
    // YouTube Data API から対象動画の現在の情報を取得
    const videoResponse = YouTube.Videos.list('snippet', { id: videoId });
    if (!videoResponse.items || videoResponse.items.length === 0) {
      return ContentService.createTextOutput(JSON.stringify({ status: 'error', message: 'Video not found: ' + videoId })).setMimeType(ContentService.MimeType.JSON);
    }
    
    const video = videoResponse.items[0];
    const snippet = video.snippet;
    const currentDescription = snippet.description || '';
    
    // 概要欄の差し替え処理（既存のSNSリンクやクレジットを破壊しない安全置換）
    const newDescription = updateDescriptionTimestamps(currentDescription, timestampsText);
    
    // 概要欄を更新
    snippet.description = newDescription;
    YouTube.Videos.update({
      id: videoId,
      snippet: snippet
    }, 'snippet');
    
    Logger.log('概要欄を正常に更新しました: ' + videoId);
    return ContentService.createTextOutput(JSON.stringify({ status: 'success', videoId: videoId })).setMimeType(ContentService.MimeType.JSON);
    
  } catch (error) {
    Logger.log('エラー発生: ' + error.toString());
    return ContentService.createTextOutput(JSON.stringify({ status: 'error', message: error.toString() })).setMimeType(ContentService.MimeType.JSON);
  }
}

/**
 * 既存の概要欄から【タイムスタンプ】ブロックだけを安全に差し替える関数
 */
function updateDescriptionTimestamps(oldDesc, newTimestamps) {
  const headerTag = '【タイムスタンプ】';
  
  // すでに【タイムスタンプ】タグが存在する場合
  if (oldDesc.indexOf(headerTag) !== -1) {
    const parts = oldDesc.split(headerTag);
    const beforePart = parts[0];
    const afterPartRaw = parts[1];
    
    // 次のセクション（例: 【使用させて頂いている...】や ▼公式URL 等）の開始を探す
    const nextSectionIndex = afterPartRaw.search(/\n\s*【|\n\s*▼|\n\s*◆|\n\s*■/);
    let afterPart = '';
    if (nextSectionIndex !== -1) {
      afterPart = afterPartRaw.substring(nextSectionIndex);
    }
    
    return beforePart.trim() + '\n\n' + headerTag + '\n' + newTimestamps.trim() + '\n\n' + afterPart.trim();
  } else {
    // タグが存在しない場合は、最上部または指定位置に配置
    return headerTag + '\n' + newTimestamps.trim() + '\n\n' + oldDesc.trim();
  }
}
