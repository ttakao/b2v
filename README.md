# b2v — 自炊PDFからオーディオブックを作る

b2v は、手元のPDF BOOKを **OCR → 必要箇所の校正 → 人間レビュー → 日本語音声合成 → MP3** にするツールです。OCR・校正・保存はローカル、標準の音声合成はGoogle Neural2を使用します。音声化する本文はGoogleへ送信されます。

音声合成を **Google Cloud Text-to-Speech APIのNeural2-C** に変更しました。作者の試聴では、以前のローカル音声よりも抑揚が落ち着き、自然で長時間聞き続けやすい朗読になりました。細かな調整をしなくても、標準の話速1.0・ピッチ0で使えることを重視しています。ローカル音声エンジンは削除し、大きな音声モデルの管理も不要になりました。
Google Cloud Text to Speech APIをプログラムから使うためには、パソコンに認証が必要です。最後にGoogle Cloud Console CLIのインストールと設定については最後にまとめてあります。

クラウド利用で気になる費用も、Neural2には**毎月100万文字までの無料枠**があります。個人で本を音声化する用途では、この範囲で十分に使えると考えています。例えば本文10万文字の本なら、単純計算で月10冊分です。b2vでは余裕を持って月90万文字を初期上限とし、試聴・再生成を含む送信量を管理します。無料枠内ならNeural2の音声生成料金はかかりません。単位はトークンではなく文字数です。料金の詳細は[Google公式料金表](https://cloud.google.com/text-to-speech/pricing?hl=ja)をご確認ください（2026年9月21日確認）。

大量の「自炊」PDFを持っていても、すべての本に市販オーディオブックが存在するわけではありません。また、運転しながら、歩きながら、スマホを見るよりもオーディオブックを効いているほうが安全であることはいうまでもありません。b2v は、そうしたPDFを自分で聞ける形へ変換することを目的にしています。

> **このプロジェクトは、現時点では一般ユーザー向けのワンクリックアプリではありません。**
> 
> Python、Homebrew、ターミナル、Google Cloud CLIと認証のセットアップが必要です。ローカルLLMは任意です。
> 一方で、処理はローカル中心で、コードも追いやすい構成にしてあります。自分の環境や本に合わせて改変できる人、AIに読み込ませて改変する人には役立つと考えています。

---

## できること

1冊のPDFについて、次の工程を順に実行します。PDFは自炊したグラフィックイメージでOKです。

```text
PDF
 ↓
GUIで読み取り範囲(Crop)調整
 ↓
Tesseract OCR
 ↓
OCR confidenceによる要確認ページ抽出
 ↓
必要なページだけローカルLLM校正（任意）
 ↓
PDF画像とOCRで生成した本文を見比べて人間レビュー
 ↓
book_final.txt
 ↓
Google Neural2
 ↓
WAV
 ↓
FFmpeg
 ↓
MP3
```

主な機能：

- 縦書き・横書きPDFのOCR
- ブラウザーインターフェースでCrop範囲を目視調整
- Tesseract confidenceによる「怪しいページ」の抽出
- LLM校正を全ページではなく必要ページだけに限定
- 原ページ画像とOCR本文を並べた人間レビュー
- 挿絵・広告・重複・不要ページなどの除外
- 最終TXT生成
- Google Neural2-Cによる日本語朗読（声・話速・ピッチを本ごとに保存）
- Googleへの送信予定文字数・月間使用量の表示と上限停止
- 長文を内部chunkへ分割し、1冊分のWAVを生成
- WAVから96 kbps / mono MP3を生成
- SQLiteでPDF・OCR・本文・音声成果物を関連付けて管理
- ブラウザから完成MP3を再生・ダウンロード

LLMは必須ではありません。実際には、低confidenceページの多くが挿絵や低文字量ページであることもあり、**OCR + 人間レビューだけで十分な本もあります**。作者もLLMはめったに使いません。

---

# 対象ユーザー

b2v は特に次のような人を想定しています。

- 自炊PDFを大量に保有している
- 市販オーディオブックがない本を聞きたい
- PDFをクラウドへアップロードせずローカル処理したい
- OCR結果を自分で確認・修正したい
- TTSの声・速度・抑揚を自分で調整したい
- PythonやAIコーディングツールを使って自分向けに改造できる

「PDFを選ぶだけで完全自動変換」を目指したものではありません。  
自炊PDF特有の、壊れたページ、広告、挿絵、スキャン事故などを回避する機能満載の設計にしています。

---

# 動作確認環境

開発・実機確認は主に以下の環境で行っています。

- macOS / Apple Silicon
- Mac mini M4
- Homebrew
- Python 3.12
- Tesseract 5系
- llama.cpp / llama-server（LLM校正を使う場合）
- FFmpeg / FFprobe

Windows / Linuxでも構成要素自体は動作可能なものが多いですが、**このREADMEのセットアップ手順はmacOS Apple Siliconを基準**にしています。

---

# まず必要なもの

## 1. Homebrew

Homebrewが未導入なら、公式サイトの手順でインストールしてください。

- https://brew.sh/

確認：

```sh
brew --version
```

---

## 2. システムパッケージ

macOSでは、少なくとも次を用意します。

```sh
brew install python@3.12
brew install tesseract
brew install tesseract-lang
brew install ffmpeg
```

LLM校正を使う場合：

```sh
brew install llama.cpp
```

`tesseract` 本体だけでは英語等の基本データしか含まれないため、日本語OCRでは `tesseract-lang` も必要です。

確認：

```sh
/opt/homebrew/bin/python3.12 -V
tesseract --version
tesseract --list-langs | grep -E 'jpn|jpn_vert'
ffmpeg -version
ffprobe -version
```

LLMを使う場合：

```sh
llama-server --help
```

Apple Silicon以外ではHomebrewのパスが `/opt/homebrew` ではない場合があります。

---

# b2v本体のセットアップ

リポジトリをcloneします。

```sh
git clone <YOUR_B2V_REPOSITORY_URL>
cd b2v
```

## Python仮想環境

b2v本体用の仮想環境を作成します。

まずb2v本体：

```sh
/opt/homebrew/bin/python3.12 -m venv .venv
source .venv/bin/activate

python -m pip install -U pip
python -m pip install -r requirements.txt
```

確認：

```sh
python -V
echo $VIRTUAL_ENV
```

期待する例：

```text
Python 3.12.x
.../b2v/.venv
```

テスト：

```sh
.venv/bin/python -m pytest -q
```

---

# Google Neural2 のセットアップ（標準）

利用するサービスは、テキストから音声を生成する **Cloud Text-to-Speech API** です。Neural2は毎月100万文字まで無料、超過分は100万文字あたり16米ドルです。以下の請求先設定は無料枠を利用する場合も必要です。b2vの初期上限は月90万文字なので、他の利用と合算して無料枠を超えない範囲で運用できます。

Google Cloud Consoleで請求先を設定し、Cloud Text-to-Speech APIを有効化します。このMacのターミナルで実行してください。

```sh
brew install --cask gcloud-cli
gcloud auth application-default login --scopes=openid,https://www.googleapis.com/auth/userinfo.email,https://www.googleapis.com/auth/cloud-platform
gcloud auth application-default set-quota-project YOUR_PROJECT_ID
```

`B2V_GOOGLE_PROJECT`でプロジェクトIDを指定します。この環境の既定値は`(プロジェクトID)`です。APIキー・サービスアカウントJSON・追加のPython SDKは不要です。b2vがADCのアクセストークンをGoogle Cloud CLIから取得し、長時間の生成中も更新します。認証情報やトークンはb2vのログには出しません。

標準の声は`ja-JP-Neural2-C`、話速1.0、ピッチ0です。画面「8. 音声設定・試聴」で声C/D、話速0.5〜2.0、ピッチ-20〜20半音を変更・保存できます。旧音声設定の本も起動時にNeural2-C・話速1.0・ピッチ0へ移行します。既存の完成MP3は保持します。

「送信予定文字数を確認」で成功済みchunkを除いた新規送信量を確認できます。生成開始時にも上限を確認し、一冊の新規送信量が残量を超える場合は送信せず停止します。生成中は各リクエストの直前にも上限を確認します。停止・失敗後は同じ声・設定・分割上限で再開すると成功済みchunkを再利用します。

月間上限の初期値は90万文字です。`B2V_GOOGLE_MONTHLY_LIMIT`で0〜100万文字の範囲に設定できます（0は新規送信停止）。変更はb2v再起動後に反映します。使用量は`B2V_DATA_DIR/google_tts_usage.sqlite3`に保存し、米国太平洋時間の暦月・プロジェクト別に集計します。試聴も含め、送信前に予約し、通信失敗・タイムアウトでも減算せず、自動再送もしません。書籍・MP3を削除しても使用量は減りません。使用量DBは削除しないでください。

この上限はGoogle請求の確定額ではなく、この保存先から送信した文字数の保守的な管理です。同じ請求先アカウントの他プロジェクト・別アプリ・別の保存先での利用は自動取得しません。Googleの無料枠は請求先アカウント内で共有されるため、Google Cloudの使用状況も確認してください。料金・無料枠が変更された場合は上限も見直してください。

Googleはモデルの重みや固定バージョンを公開していないため、長期間あけて再開した場合の声の完全一致は保証できません。キャッシュにはエンジン、声、話速、ピッチ、出力形式、本文、分割条件を含め、異なる音声設定のキャッシュとは混在させません。

公式情報：[認証](https://docs.cloud.google.com/text-to-speech/docs/authentication)、[料金](https://cloud.google.com/text-to-speech/pricing?hl=ja)、[月間料金階層のリセット](https://docs.cloud.google.com/billing/docs/how-to/pricing-table)。

# ローカルLLM（任意）

LLM校正はオプションです。

b2vはOCR confidenceの低いページを候補にし、選択されたページだけLLMへ渡します。全ページを必ずLLMへ通す設計ではありません。

llama.cpp：

```sh
brew install llama.cpp
```

b2vでは `llama-server` をローカルHTTPサーバとして使用します。

モデルファイル（GGUF）は別途用意してください。  
使用モデルはマシンのメモリ容量と必要な品質に合わせて選択します。

**LLMを使わなくても、OCR → 人間レビュー → 最終TXT → 音声生成は可能です。**

---

# 環境変数による設定

設定は `b2v/config.py` に集約しています。未指定なら以下の値を使います。

| 環境変数                | デフォルト                         |
| ------------------- | ----------------------------- |
| `B2V_DATA_DIR`      | `/Volumes/RAID1-6TB/b2v-data` |
| `B2V_API_URL`       | `http://127.0.0.1:8600`       |
| `B2V_LLM_URL`       | `http://127.0.0.1:8602`       |

`B2V_DATA_DIR`直下にSQLiteと`books/`を保存します。外付けボリュームを接続してから起動してください。以下で`$B2V_DATA_DIR`と表記する場所は、環境変数未指定の場合も上記デフォルトを指します。

変更する場合は起動する各ターミナルで環境変数を設定します。`.env`ファイルの自動読込は行いません。

```sh
export B2V_DATA_DIR=/Volumes/RAID1-6TB/b2v-data
export B2V_API_URL=http://127.0.0.1:8600
export B2V_LLM_URL=http://127.0.0.1:8602
```

起動スクリプトも同じURLからホスト・ポートを取得します。付属スクリプトはHTTP起動用です。ブラウザーは`B2V_API_URL`のURLで開いてください。以前の`B2V_PORT`・`B2V_LLM_PORT`は使用しません。URL変更はプロセス再起動後に反映されます。

# 起動

## まとめて起動・停止

```sh
cd /path/to/b2v
./run-all.sh
```

標準ではb2vだけをバックグラウンドで起動し、応答を確認します。Google音声にはローカルの音声サーバーは不要です。LLMが必要なときは`./run-llm.sh`を実行してください。一括起動に含める場合は`B2V_START_LLM=1`を指定します。環境変数は個別起動と共通です。ログは `logs/b2v.log`・`logs/llm.log` に追記し、起動記録は `run/` に保存します。

終了するとき：

```sh
./stop-all.sh
```

b2vへ通常終了を要求し、その終了後にLLMを停止します。実行中の処理を待ち、WAV生成は現在のchunk終了後に止めます。待ち時間による自動強制終了はしません。停止待ちをCtrl+Cで中断しても、再度 `./stop-all.sh` を実行できます。

別の方法で起動済みのサービスは利用可能か確認しますが、停止管理へ取り込みません。管理対象外のb2vが稼働中なら、LLMの停止も見合わせます。すべてを一括停止したい場合は、最初に個別起動したサービスを元のターミナルから終了し、その後 `./run-all.sh` で起動してください。

起動失敗時は成功と表示せず、該当ログを案内します。それまでに起動したサービスは保持します。必要なら `./stop-all.sh` で停止してください。起動確認は最大180秒です。再実行時は管理中のPID・開始時刻・実行引数とサービスの応答を照合します。

b2vのローカルプロセスは以下の通りです。通常は8600だけを使用します。

| ポート  | 用途               | 必須        |
| ---- | ---------------- | --------- |
| 8600 | b2v Webアプリ       | 必須        |
| 8602 | llama.cpp LLM    | 任意        |

## Terminal 1 — b2v

```sh
cd /path/to/b2v
./run.sh
```

ブラウザー：

```text
http://127.0.0.1:8600
```

## Terminal 2 — LLM（任意）

```sh
cd /path/to/b2v
./run-llm.sh
```

LLM校正を使わなければ起動不要です。

# 使い方

## 1. PDFを登録

PDFをドラッグ＆ドロップ、またはクリックして指定します。

現在の上限は512MBです。

## 2. 余白を調整

OCR前にページを表示し、上下左右の余白を数値または境界線ドラッグで調整します。

ページ番号、柱、ヘッダー、フッターをできるだけOCR対象外へ出すことが重要です。

複数ページを確認して本文を切っていないことを確認し、「このcropを使用」を押します。

## 3. OCR

OCR対象ページ、除外ページ、書字方向等を設定してOCRを開始します。

日本語：

- 横書き：`jpn`
- 縦書き：`jpn_vert`

OCR完了後、各ページについてTesseract TSVのconfidenceを保存します。

> OCR confidenceは正読率ではありません。
> 
> 同じ本の中で「相対的に怪しいページ」を探すための参考値として使っています。

## 4. 品質確認

初期状態では、通常本文の平均confidence下位10%を確認候補にします。

- 下位割合は変更可能
- 保存済みconfidenceから再計算するためOCR再実行は不要
- 低文字量ページは別扱い
- confidence同点ページは境界でまとめて対象になる場合あり

挿絵、章扉、広告などが候補になることもあります。

## 5. LLM校正（任意）

選択したページだけLLMへ渡します。

LLMは万能なOCR復元器ではありません。大量欠落、読み順崩壊、スキャン事故等は人間確認を優先してください。

## 6. 人間レビュー

ページ番号をクリックすると、

```text
Crop後のPDFページ画像 | 現在の本文
```

を並べて確認できます。

ここで：

- 誤字修正
- LLM修正の訂正
- ページ除外
- 除外解除
- 「編集せず確認済み」

などを行えます。

広告、重複ページ、スキャン事故などはここで除外できます。

## 7. 最終TXT

「最終テキストを生成」で、採用ページをPDF順に連結したUTF-8の：

```text
book_final.txt
```

を生成します。

本文を後から変更した場合は最終TXTを再生成してください。

## 8. WAV音声生成

Google Neural2への接続確認が成功すれば生成できます。

Googleの設定は声C/D・Speed（話速）・ピッチです。通常はNeural2-C、話速1.0、ピッチ0を使います。

長文は内部的に複数chunkへ分割しますが、これはTTS処理単位です。  
**最終成果物を複数ファイルに分割するためのものではありません。**

一文が最大chunk文字数を超える場合は、文章途中で強制切断せず生成を止めます。本文を確認・修正してください。

## 9. MP3生成

完成WAVからFFmpegでMP3を生成します。

現在：

```text
96 kbps
mono
```

です。朗読にステレオや高音質は意味がないので、一般的なレートにしてあります。

完成後：

- ブラウザー再生
- MP3ダウンロード

が可能です。

---

# データ管理

b2vはSQLiteと文書単位のディレクトリで管理します。

```text/Volumes/RAID1-6TB/b2v-data/
├── catalog.sqlite3
└── books/
    └── <document_id>/
        ├── source/
        │   └── book.pdf
        ├── ocr/
        ├── text/
        │   └── book_final.txt
        └── audio/
```

主な保存物：

- `$B2V_DATA_DIR/catalog.sqlite3`  
  文書、ページ設定、OCR confidence、レビュー状態、音声生成状態等

- `source/book.pdf`  
  PDF原本

- `ocr/page_NNNN.txt`  
  OCR raw text

- `ocr/page_NNNN_tsv.json`  
  Tesseract TSV由来情報

- `text/page_NNNN.txt`  
  現在採用中のページ本文

- `text/page_NNNN_llm.json`  
  直近のLLM差分・適用結果

- `text/llm_requests.jsonl`  
  LLM処理ログ

- `text/book_final.txt`  
  最終本文

- `audio/`  
  TTS chunk、WAV、MP3、音声生成ログ

ファイルはSQLiteの状態と関連しています。

**`$B2V_DATA_DIR/books/...` のファイルをFinderやシェルから直接削除するのではなく、原則としてb2vの管理画面から削除してください。**

直接削除するとSQLiteと実ファイルの状態がずれる可能性があります。

---

# MP3の扱い

MP3は文書のSQLiteレコードに関連付けます。

現在の実装では：

- WAV生成完了後にMP3化
- 96 kbps / mono
- MP3変換失敗時は入力WAVを保持
- 完成MP3はブラウザ再生可能
- 文書タイトルでダウンロード
- MP3削除はアプリ経由

完成MP3を作成後、現在の実装では不要になった完成WAV/chunkを整理します。再生成する場合は最終TXTからWAV生成をやり直します。

---

# 制限事項

現時点で特に重要な制限です。

軽い気持ちでDocker上で作り始めたのですが、たいへんな範囲に広がってしまいHomebrewでのインストールをやらざるを得なくなりました。

### 一般ユーザー向けインストーラではない

複数のPython環境と3つのローカルサービスを扱います。

### OCRは完全ではない

confidenceが高くても誤認識はあり得ます。逆にconfidenceが低くても正しく読めている場合があります。

### LLMでも復元できないページがある

大量欠落や画像自体の破損は人間判断が必要です。LLMの処理を加速するためにブランクの圧縮はしてあります。

### 挿絵の説明はしない

画像内容をAIで説明する機能はありません。

### 自動章分割はしない

現在は原則として1冊を1つのMP3にします。これは個人的にそのほうが便利だからというのもあります。

# よくあるトラブル

## `jpn` / `jpn_vert` がない

```sh
tesseract --list-langs
```

で確認します。

macOS/Homebrew：

```sh
brew install tesseract-lang
```

## 8602へ接続できない

ポート：

```text
8600 b2v
8602 LLM
```

を確認してください。

## FFmpegがない

```sh
brew install ffmpeg
```

確認：

```sh
ffmpeg -version
ffprobe -version
```

# 実装概要

主要部分：

```text
b2v/catalog.py
    SQLite・文書管理

b2v/document_api.py
    HTTP API

b2v/quality.py
    OCR confidence集計・順位

b2v/ocr_confidence.py
    Tesseract TXT/TSV取得

b2v/narration.py
    LLM差分校正

b2v/static/
    HTML / CSS / Vanilla JavaScript
```

Google音声は `b2v/google_tts.py` からHTTPSで呼び出します。

ローカル音声モデルやPyTorchは不要です。

---

# テスト

```sh
.venv/bin/python -m pytest -q
```

OCR confidence、候補選択、低文字量ページ、人間編集、LLM処理、ページ除外、SQLite再読み込み、最終TXT等を検証します。

自動テストではGoogle通信をモックします。MP3の検証にはFFmpegが必要です。

---

# このプロジェクトの考え方

b2vは「AIですべて自動修正する」方向ではなく、

```text
機械でできること → Python
判断が必要な一部 → LLM
最後に確認したい部分 → 人間
```

という分担を採っています。

実装してみると、OCR confidenceの低いページが必ずしも「AIで直すべきページ」ではなく、挿絵・広告・章扉・低文字量ページであることも多くありました。

そのため、最後に人間が原画像と本文を見て判断できることを重視しています。

---

# 著作物について

このソフトウェアは、利用者が適法に扱えるPDFを対象として使用してください。

PDFの複製、OCR、音声化、共有、公開等に関する権利関係は、作品・入手経路・地域・利用目的によって異なります。

生成した本文や音声を第三者へ配布・公開する場合は、原著作物の権利を確認してください。

---

# ライセンスと外部モデル

b2v本体のライセンスは、MITライセンスです。

Google Cloudサービス、LLMモデル、Tesseract、FFmpegその他の依存物には、それぞれ別のライセンス・利用条件があります。したがって同梱しておりません。

特に音声モデルやLLMモデルは、コード本体と同じライセンスとは限りません。利用・再配布前に各配布元の条件を確認してください。

---

# 関連プロジェクト

- Google Cloud Text-to-Speech
  https://cloud.google.com/text-to-speech

- Tesseract OCR  
  https://github.com/tesseract-ocr/tesseract

- llama.cpp  
  https://github.com/ggml-org/llama.cpp

- FFmpeg  
  https://ffmpeg.org/

---

# Status

Version 1では、

```text
PDF
→ OCR
→ 品質確認
→ 人間 / LLM校正
→ 最終TXT
→ Google Neural2
→ WAV
→ MP3
```

まで一冊を通して生成できています。

今後の改善候補はありますが、まずは実際に作ったMP3を長時間聞き、必要になったものだけを追加する方針です。



2026/09/07 高尾　司

試聴と本番生成は別の欄です。「8. 音声設定・試聴」で短文を聴き比べ、「9. 本全体のWAV生成（本番）」で対象の本を確認して全文生成を開始します。試聴の完了メッセージ・再生プレーヤーは試聴欄内に表示します。

---

# Google Cloud CLIと音声APIの設定

Google Cloud Consoleはブラウザーの管理画面、**Google Cloud CLI（`gcloud`）** はMacのターミナルで使うコマンドです。b2vではCLIで認証し、Google Cloud Text-to-Speech APIで音声を生成します。以下はこのM4 Mac miniで使う設定です（2026年9月21日）。

## 手元に必要な情報

| 項目 | この環境の設定 |
| --- | --- |
| Googleアカウント | 下記プロジェクトを利用できるアカウントでログイン |
| プロジェクトID | (Google Cloud Consoleで登録したら割り当てられる。表示名やプロジェクト番号とは別） |
| 有効にするAPI | **Cloud Text-to-Speech API**（`texttospeech.googleapis.com`） |
| 請求先 | プロジェクトへ有効な請求先アカウントを関連付ける。無料枠でも必要 |
| 認証方法 | Application Default Credentials（ADC） |
| 標準音声 | `ja-JP-Neural2-C`、話速1.0、ピッチ0 |
| b2vの月間送信上限 | 90万文字。Neural2の無料枠は毎月100万文字 |

APIキーの取得やサービスアカウントキーJSONの作成は、この構成では不要です。音声認識の「Speech-to-Text API」と間違えないようにしてください。

## 1. ConsoleでプロジェクトとAPIを確認

プロジェクトのText-to-Speech API画面を開き、プロジェクトが割り当てられているかを確認します。APIが未有効なら「有効にする」を選びます。Consoleの「お支払い」で請求先の関連付けも確認します。

## 2. CLIをインストール

Homebrew導入済みのMacで実行します。すでに`gcloud --version`が使えれば、再インストールは不要です。

```sh
brew install --cask gcloud-cli
gcloud --version
```
コマンドが見つからない場合はbrewのパスがとおっていません。

## 3. b2v用に認証する

次のコマンドをそのまま実行します。ブラウザーが開いたら、プロジェクトを利用できるGoogleアカウントでログインしてください。

```sh
gcloud auth application-default login \
  --scopes=openid,https://www.googleapis.com/auth/userinfo.email,https://www.googleapis.com/auth/cloud-platform
```

同意画面ではGoogle Cloudへのアクセスとアカウント情報の権限を許可します。Google Cloudの「データの参照、編集、設定、削除」という広い表示は`cloud-platform`スコープに対応します。実際にできる操作は、そのアカウントに付与された権限でも制限されます。**Cloud SQLインスタンスへのログイン権限はb2vには不要**です。上記コマンドにはSQL用スコープを含めていません。

続いて、APIの使用量・課金先となるプロジェクトをADCへ登録します。

```sh
gcloud auth application-default set-quota-project プロジェクトID
```

`Quota project "(プロジェクトID)" was added to ADC`と表示されれば完了です。最初のログインで`Cannot find a quota project`という警告が出ても、この設定が成功すれば解消します。

認証情報は通常`~/.config/gcloud/application_default_credentials.json`へ自動保存されます。これは秘密情報を含むため、READMEやGitへコピーしません。通常は起動のたびにログインする必要はありません。認証が失効した場合は、この節の2つのコマンドをもう一度実行します。

`gcloud auth login`だけではb2v用ADCの設定になりません。また、`gcloud config set project`だけではADCのquota projectは設定されません。上記の`application-default`付きコマンドを使ってください。

## 4. b2vを起動して確認

外付けRAIDを接続してから実行します。この環境ではプロジェクトIDと月間上限はすでに既定値ですが、明示する場合は以下のとおりです。設定を変更するときは、実行中の生成が完了してから`./stop-all.sh`で停止し、再起動します。

```sh
cd (システムを置くフォルダ)
export B2V_GOOGLE_PROJECT=(プロジェクトID)
export B2V_GOOGLE_MONTHLY_LIMIT=900000
./run-all.sh
```

[http://127.0.0.1:8600/](http://127.0.0.1:8600/)を開き、「8. 音声設定・試聴」で「Google Neural2: 利用可能」と表示されることを確認します。短い文章で試聴生成・再生ができれば、認証から音声生成まで動作しています。試聴も送信文字数に含まれます。

## 困ったとき

| 表示・症状 | 確認すること |
| --- | --- |
| ADC認証を取得できない・`invalid_grant` | 「3. b2v用に認証する」を再実行 |
| `API not enabled` / `SERVICE_DISABLED` | `(プロジェクトID)`でText-to-Speech APIを有効化したか |
| quota projectが見つからない | `set-quota-project (プロジェクトID)`を再実行 |
| `set-quota-project`で権限エラー | ログインしたアカウントとプロジェクトを確認。`serviceusage.services.use`権限が必要（例：Service Usage Consumerロール） |
| 課金・請求先関連のエラー | Consoleでプロジェクトに有効な請求先が関連付いているか |
| b2vの月間上限に到達 | 画面の使用量を確認。翌月への切り替えは米国太平洋時間基準。使用量DBを消してリセットしない |

使用量の記録は`/Volumes/RAID1-6TB/b2v-data/google_tts_usage.sqlite3`に保存します。同じ請求先の別プロジェクト・別アプリでの使用分はb2vでは集計できないため、その分はGoogle Cloud側で確認します。

公式資料：[CLIのインストール](https://docs.cloud.google.com/sdk/docs/downloads-homebrew)、[Text-to-Speechの認証](https://docs.cloud.google.com/text-to-speech/docs/authentication)、[料金・無料枠](https://cloud.google.com/text-to-speech/pricing?hl=ja)。
