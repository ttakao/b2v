# b2v 引き継ぎ資料

更新日：2026-09-21。この資料は次のチャットで作業を再開するための現状・判断の記録です。新しい作業を自動的に開始する指示ではありません。

## 最初に読むこと

- プロジェクト：`/Users/tsukasa_takao/dev/b2v`
- 画面名：**PDF オーディオブック変換**。
- 一冊のPDFからOCR、校正、全文WAV、MP3まで実装済み。ユーザーはWAV生成の完走を確認した。
- Docker化しない。現在のMacネイティブ環境を維持する。
- 新しい独立Job体系・文書の自動フェーズ管理・本文バージョン管理は不要。各工程を人間が開始する。
- ユーザーの次の指示を待つ。機能追加や最適化を勝手に進めない。
- README.mdはユーザー自身も大幅編集している。古い会話の内容で上書きしない。

## 起動・停止と設定

```sh
cd /Users/tsukasa_takao/dev/b2v
./run-all.sh
./stop-all.sh
```

個別起動は `run.sh`、`run-llm.sh`。一括起動は `b2v/services.py`、個別プロセス起動は `b2v/launch.py` が担当。

| 環境変数 | デフォルト |
| --- | --- |
| B2V_DATA_DIR | /Volumes/RAID1-6TB/b2v-data |
| B2V_API_URL | http://127.0.0.1:8600 |
| B2V_LLM_URL | http://127.0.0.1:8602 |

設定は `b2v/config.py` に一元化。旧8601、B2V_PORT、B2V_LLM_PORTを復活させない。85xxは別プロジェクト用。.env自動読込は未実装。ブラウザーのAPI呼び出しは同一originの相対URL。

`run-all.sh` は応答確認を行い、ログを `logs/` へ追記。PID等を `run/` に保存。`stop-all.sh` はb2vへSIGTERM→終了待ち→LLMの順。時間切れで強制終了しない。別の方法で起動したプロセスは管理に取り込まない。管理外b2vが稼働している場合、依存サーバーも停止しない。

再起動前は実行中の処理を必ず確認する。WAV生成中に再起動すると現在chunk終了後に停止する。実行中処理を理由なく中断しない。

## 保存とSQLite

旧プロジェクト直下 `data/` は上記B2V_DATA_DIRへ移動済み。704ファイルの内容一致とSQLite正常性を確認して旧dataを削除した。記録は相対ファイル名で、旧data絶対パスの書き換え対象は0件だった。

```text
B2V_DATA_DIR/
  catalog.sqlite3
  books/<32文字のdocument_id>/
    source/book.pdf
    ocr/page_NNNN.txt
    ocr/page_NNNN_tsv.json
    text/page_NNNN.txt
    text/page_NNNN_llm.json
    text/book_final.txt
    text/llm_requests.jsonl
    audio/
```

既存schema：documents(id TEXT PRIMARY KEY, data TEXT)、pages(document_id TEXT, number INTEGER, data TEXT)、audio_chunks(document_id TEXT, fingerprint TEXT, data TEXT)。設定・成果物情報は主にJSONへ保存。新しい重複成果物テーブルは作っていない。

PDF・OCR・テキスト・音声は種類別に削除。他の種類へ連鎖削除しない。最後のファイル削除時は `Catalog.prune_empty()` で文書と関連ページ・chunk登録も削除。残存内容ファイルとassetsを確認し、ログだけなら整理する。空だった「バッドラック」は削除済み。

## 画面と確定した仕様

1. PDF選択：1冊、ドラッグ＆ドロップ対応。
2. Crop：PDF画像を見ながら上下左右を数値・ドラッグで調整。ページ移動可能。確認中にOCRは走らない。既存OCRSettingsは百分率、既存制限0〜30%。本全体へ共通設定。
3. OCR：対象・除外ページ指定。Tesseract。基本方向・横書きブロック処理。
4. 品質一覧：通常本文の平均confidence下位割合（初期10%）をLLM候補へ。同点は全部含める。低文字量・評価不能は赤太字で人間確認対象、LLM自動対象にはしない。手動選択可能。
5. 選択ページのLLM校正：現在の本文へ差分修正。人間編集済みでも明示実行なら処理する。LLM成功は人間確認済みを意味しない。進捗バー、処理ページ・分割番号、経過時間・応答待ち時間、成功失敗数を追加済み。
6. 原画像と本文の校正：保存した本文が以後の入力。版管理・cleanへ戻す仕組みは不要。原画像はPDFから描画するのでPDF削除後は表示不可。
7. 最終テキスト：対象本文をページ順に連結しUTF-8 TXT生成。ここで音声化の可否を決定しない。本文編集後は未更新状態となる。
8. 音声設定・試聴：案内は「音声の設定をサンプルを使っておこなってください。」。本なしで最大150文字の自由文章を試聴。メッセージとプレーヤーはこの枠内。本番とは別form。Neural2-C・話速1.0・ピッチ0が標準。声C/D・話速・ピッチを変更可能。試聴音声はブラウザーBlobで保持し成果物登録しない。
9. 本全体のWAV生成（本番）：対象の本と長時間処理である旨を表示。本文編集後は「修正本文から最終TXTを更新してWAV生成を再開」で最終TXT更新→WAV開始。未保存本文は開始不可。
10. MP3生成：人間が開始。mono・96kbps固定。完成後再生・ダウンロード・削除可能。

OCR confidenceは正読率ではない。Tesseract TSV level=5の非空認識単位について有効conf(0〜100)を集計する。書字方向confidenceとは別。OCR後Python cleanup→現在本文→必要なページだけLLM/人間校正。OCR再実行は現在本文を保持し、「OCRからテキストを再生成」を押すと置き換える。

## 音声生成の重要事項

- 音声はGoogle Neural2専用。`b2v/google_tts.py` がADC認証とHTTPS通信を担当。ローカル音声エンジン・専用venv・モデル・起動スクリプトは撤去済み。
- プロジェクトは`text2voice-509213`。月90万文字で停止するローカル台帳は`google_tts_usage.sqlite3`。試聴・失敗通信も計上し、他アプリの使用量は把握しない。
- `run-all.sh`はb2vのみ起動。LLMは`B2V_START_LLM=1`で任意起動。旧音声設定は起動時にGoogle標準へ移行し、完成MP3は保持。

- WAV上限は50〜300文字、通常300。句点・段落境界で分割し、長すぎる一文を強制途中分割しない。
- 本番はバックグラウンド逐次生成。停止要求は現在chunk終了後に処理。成功済みchunkは保持。
- 再利用は本文・TTS設定・モデルファイル識別情報・分割設定が一致するchunkのみ。これは途中再開用の安全性であり、MP3の版管理とは別。修正で分割が変わったchunkは再生成。
- 進捗はchunk数の割合。残り時間は概算で最後の結合を含まない。
- MP3はFFmpegで一時出力→形式・時間・全デコード検証→正式登録→旧MP3・完成WAV・chunk削除。変換失敗時は以前のMP3と入力WAVを保持。
- MP3には本文hash整合性管理・stale表示・バージョン管理を設けない。最後に成功したMP3を成果物として扱う。
- WAVはMP3の作業ファイル。通常画面の完成WAV再生は撤去済み。試聴機能とは別。

## エラー表示と直近のトラブル

WAVエラーは `audio_run.error` と `error_context` に保持。Webの本番欄に3行の赤いエラーボックス（原因・ページ/chunk・本文）と該当ページの校正ボタンを表示する。直近の変更で、その上へ **「該当ページすべてを確認してください。」** を追加済み。

事前スキャンは追加しない。「実行時に止まり、画面で原因を見る」方式をユーザーが選択した。


「半分にして試聴すると成功する」との相談では、成功した試聴ログに問題部分が含まれていなかった。試聴textareaのmaxlength=150により貼り付けが途中で切れた可能性を説明した。これは未改善の使い勝手の課題。ユーザーの追加指示なしに上限変更等はしていない。

古い失敗は、生成時hashと残っている最終TXTが一致する場合だけ `describe_failure()` でchunk本文を復元。本文編集後のページ特定を憶測で行わない。以後の失敗はその時点でerror_contextを保存する。

## コードの入口

| ファイル | 担当 |
| --- | --- |
| b2v/api.py | FastAPI起動、旧API、各機能の組み立て |
| b2v/document_api.py | 現行文書API、ファイル提供、一覧 |
| b2v/catalog.py | SQLite、文書・ページ操作、OCR/LLM、最終TXT、削除 |
| b2v/ocr.py、ocr_workflow.py、crop_preview.py | OCR・画像・crop |
| b2v/quality.py、ocr_confidence.py | 品質評価・TSV |
| b2v/narration.py | LLM設定・差分プロンプト・適用検査 |
| b2v/audio_workflow.py | WAV分割・生成・再開・結合・失敗箇所 |
| b2v/tts_api.py | WAV・試聴・MP3 API |
| b2v/mp3.py | MP3生成・検証・削除 |
| b2v/static/index.html、app.js、tts.js、style.css | Vanilla UI |
| b2v/config.py、launch.py、services.py | 設定・起動停止 |

旧work/Job系コードは残っている。今回の仕組みへ勝手に大規模統合・削除しない。

## 検証状況と残課題

```sh
.venv/bin/python -m pytest -q
```

2026-09-21：Google専用化・旧環境削除後の全体テストは121件成功。依存ライブラリの非推奨警告7件あり。

- 2026-09-08の今回確認：SQLiteには「エネルギーマイスターの絶対法則.pdf」1文書、busyなし、MP3成果物登録あり。WAV実行記録はMP3後の削除によりなし。将来は必ず現状を再確認する。
- バッドラックは実冊MP3（2時間14分32秒、96kbps mono）生成・ブラウザー再生・配信を確認したが、後にユーザーが全ファイルを削除し、エントリーも削除済み。
- 外部MP3プレーヤー/スマートフォンでの実機再生はユーザー確認事項。こちらの検証済み扱いにしない。
- 試聴の150文字上限で貼り付け切れが分かりにくい。改善未実施。
- services.pyのプロセス開始時刻はpsの表示を使う。ターミナルのロケールが変わると同じPIDでも照合できない可能性がある（過去に日本語・英語の日時表示差を観測）。次に停止問題が出たら確認する。勝手にPIDのみの照合へ弱めない。
- 保存済み本文はOCRの崩れが残る場合がある。ユーザーの原画像照合なしに文字列を推測置換しない。
- 新しいチャットでは権限が読み取り専用の場合がある。変更できないのに変更済みと報告しない。

## 次のチャットへの依頼例

「`/Users/tsukasa_takao/dev/b2v/HANDOFF.md` を読んで、b2vの続きをお願いします。今回の依頼は○○です。」
