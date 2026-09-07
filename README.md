# b2v — 自炊PDFからローカルでオーディオブックを作る

b2v は、手元のPDF BOOKを **OCR → 必要箇所の校正 → 人間レビュー → 日本語音声合成 → MP3** までローカルで処理し、オーディオブックを作るためのツールです。

大量の「自炊」PDFを持っていても、すべての本に市販オーディオブックが存在するわけではありません。また、運転しながら、歩きながら、スマホを見るよりもオーディオブックを効いているほうが安全であることはいうまでもありません。b2v は、そうしたPDFを自分で聞ける形へ変換することを目的にしています。

> **このプロジェクトは、現時点では一般ユーザー向けのワンクリックアプリではありません。**
> 
> Python、Homebrew、ターミナル、ローカルLLM、Style-Bert-VITS2 などのセットアップが必要です。  
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
Style-Bert-VITS2
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
- Style-Bert-VITS2による日本語朗読
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
- Style-Bert-VITS2 / JP-Extra
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

b2v本体とStyle-Bert-VITS2は**別venv**にします。

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

# Style-Bert-VITS2 のセットアップ

Style-Bert-VITS2はb2v本体と依存関係を分離します。

想定ディレクトリ：

```text
b2v/
├── .venv/                 # b2v本体
├── b2v/
├── stylebert/
│   ├── .venv/             # Style-Bert専用
│   └── sbv2-src/          # Style-Bert-VITS2 source
└── ...
```

## 1. 専用venvを作る

```sh
mkdir -p stylebert
cd stylebert

/opt/homebrew/bin/python3.12 -m venv .venv
source .venv/bin/activate

python -m pip install -U pip
```

必ず確認してください。

```sh
which python3
python3 -V
echo $VIRTUAL_ENV
```

`$VIRTUAL_ENV` が `.../b2v/stylebert/.venv` になっていれば正しい環境です。

## 2. Style-Bert-VITS2 sourceを取得

```sh
git clone https://github.com/litagin02/Style-Bert-VITS2.git sbv2-src
cd sbv2-src
```

Style-Bert-VITS2は更新により依存関係が変化することがあります。  
**動作確認できた環境では、その時点の依存関係をlockして再利用することを推奨します。**

このプロジェクトに `stylebert/requirements-lock.txt` 等が含まれている場合は、まずそれを優先してください。

## 3. 推論環境

Style-Bert-VITS2の公式プロジェクトは機能が多く、学習・音声認識等の依存も含みます。b2vで必要なのは主に**推論**です。

環境により追加パッケージが必要になります。少なくとも本プロジェクトのApple Silicon環境では、次の系統を使用しました。

```sh
python -m pip install style-bert-vits2
python -m pip install onnxruntime accelerate
```

`initialize.py` が要求する場合：

```sh
python -m pip install PyYAML huggingface-hub
```

モデル/BERTデータを取得：

```sh
python initialize.py
```

完了後、例えば次のようなファイルができます。

```text
model_assets/
└── jvnv-M1-jp/
    ├── config.json
    ├── *.safetensors
    └── style_vectors.npy
```

## 4. Apple Siliconで遭遇した注意点

Style-Bert-VITS2周辺はPython / NumPy / PyTorch / pyopenjtalkの組み合わせによって挙動が変わります。

このため、**「READMEに書かれた最新バージョンへ全部更新する」より、「実際に音声生成に成功した環境を固定する」ことを推奨します。**

成功後：

```sh
python -m pip freeze > ../requirements-lock.txt
```

としておくと再構築しやすくなります。

### `pkg_resources` が見つからない

古い `pyopenjtalk` 系が `pkg_resources` を参照する場合があります。新しいsetuptoolsとの組み合わせで問題になる場合は、動作確認済みlockを優先してください。

### `numpy.dtype size changed`

例：

```text
ValueError: numpy.dtype size changed, may indicate binary incompatibility
```

NumPyとC拡張モジュールのABI不整合です。NumPyまたはpyopenjtalkを単独で無闇に更新せず、動作確認済み依存構成へ戻してください。

### `Input type (c10::Half) and bias type (float)`

Apple Silicon CPU推論で、日本語BERT特徴量がfloat16、Style-Bert側がfloat32になった場合に発生しました。

本プロジェクトの動作確認環境では、`style_bert_vits2/nlp/japanese/bert_feature.py` の該当BERT特徴量をCPUへ移す箇所でfloat32へ変換する修正を使用しています。

概念的には：

```python
.cpu()
```

を

```python
.cpu().float()
```

とします。

リポジトリにこのパッチが既に含まれている場合は追加修正しないでください。

---

# Style-Bert単体の動作確認

b2vへ接続する前に、Style-Bert-VITS2単体で短文をWAV化できることを確認してください。

成功時の目安：

```text
Using JP-Extra model
Model loaded successfully
Loaded the JP BERT model
Audio data generated successfully
```

WAVが生成されたらmacOSでは：

```sh
open test.wav
```

で試聴できます。

この単体試験が通ってからb2vを起動する方が、問題の切り分けが容易です。

---

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
| `B2V_STYLEBERT_URL` | `http://127.0.0.1:8603`       |

`B2V_DATA_DIR`直下にSQLiteと`books/`を保存します。外付けボリュームを接続してから起動してください。以下で`$B2V_DATA_DIR`と表記する場所は、環境変数未指定の場合も上記デフォルトを指します。

変更する場合は起動する各ターミナルで環境変数を設定します。`.env`ファイルの自動読込は行いません。

```sh
export B2V_DATA_DIR=/Volumes/RAID1-6TB/b2v-data
export B2V_API_URL=http://127.0.0.1:8600
export B2V_LLM_URL=http://127.0.0.1:8602
export B2V_STYLEBERT_URL=http://127.0.0.1:8603
```

起動スクリプトも同じURLからホスト・ポートを取得します。付属スクリプトはHTTP起動用です。ブラウザーは`B2V_API_URL`のURLで開いてください。以前の`B2V_PORT`・`B2V_LLM_PORT`は使用しません。URL変更はプロセス再起動後に反映されます。

# 起動

## まとめて起動・停止

```sh
cd /path/to/b2v
./run-all.sh
```

b2v・LLM・Style-Bertをバックグラウンドで起動し、応答を確認します。環境変数は個別起動と同じ共通設定を使います。ログは `logs/b2v.log`・`logs/llm.log`・`logs/stylebert.log` に追記します。起動記録は `run/` に保存します。

終了するとき：

```sh
./stop-all.sh
```

b2vへ通常終了を要求し、その終了後にLLM・Style-Bertを停止します。実行中の処理を待ち、WAV生成は現在のchunk終了後に止めます。待ち時間による自動強制終了はしません。停止待ちをCtrl+Cで中断しても、再度 `./stop-all.sh` を実行できます。

別の方法で起動済みのサービスは利用可能か確認しますが、停止管理へ取り込みません。管理対象外のb2vが稼働中なら、LLM・Style-Bertの停止も見合わせます。すべてを一括停止したい場合は、最初に個別起動したサービスを元のターミナルから終了し、その後 `./run-all.sh` で起動してください。

起動失敗時は成功と表示せず、該当ログを案内します。それまでに起動したサービスは保持します。必要なら `./stop-all.sh` で停止してください。起動確認は最大180秒です。再実行時は管理中のPID・開始時刻・実行引数とサービスの応答を照合します。

b2vは現在3つのローカルプロセスで構成します。

| ポート  | 用途               | 必須        |
| ---- | ---------------- | --------- |
| 8600 | b2v Webアプリ       | 必須        |
| 8602 | llama.cpp LLM    | 任意        |
| 8603 | Style-Bert-VITS2 | WAV生成時に必要 |

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

## Terminal 3 — Style-Bert-VITS2

```sh
cd /path/to/b2v
./run-stylebert.sh
```

使用中は必要なターミナルを開いたままにしてください。

終了時は、処理完了を待って `Ctrl+C` で停止します。

---

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

Style-Bert-VITS2を8603で起動してから実行します。

主な設定：

- Model
- Voice / Speaker
- Style
- Style Weight
- Speed
- Noise
- SDP Noise
- Pitch
- Intonation
- 最大chunk文字数

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

# Style-Bert-VITS2 朗読設定

最初は次を基準にします。

```text
Style:       Neutral
Style Weight: 1.0
Speed:        1.0
Noise:        0.6
SDP Noise:    0.8
Pitch:        1.0
Intonation:   1.0
```

## 抑揚が強すぎる場合

まず `Intonation` を下げます。

例：

```text
1.0 → 0.9 → 0.8 → 0.7
```

次に必要ならStyle Weightを調整します。

朗読ではモデルによって演技感が強く感じられることがあります。長時間聞く用途では、短いサンプルだけでなく実際に数十分聞いて設定を決めることを推奨します。

## Speed

b2vの画面では大きいほど速くなる方向です。

内部ではStyle-Bert-VITS2の：

```text
length = 1 / speed
```

へ変換します。

## Advanced

| 項目         | 意味                |
| ---------- | ----------------- |
| Noise      | 音声生成時のランダム性       |
| SDP Noise  | 音素長・発話タイミングの揺らぎ   |
| Pitch      | 声全体の高さ            |
| Intonation | 平均ピッチからの上下動＝抑揚の強さ |

まずNoiseは0.6、Pitchは1.0のままにし、Style / Speed / SDP Noise / Intonationを優先して比較するのがおすすめです。

これらはテストできますから、事前に設定しておいてください。

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

### Style-Bert-VITS2の環境構築が難しい場合がある

特にApple SiliconではPython/NumPy/PyTorch/pyopenjtalkの組み合わせに依存する問題が出る場合があります。動作した環境のlockを保存することを推奨します。

---

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

## 8602 / 8603へ接続できない

ポート：

```text
8600 b2v
8602 LLM
8603 Style-Bert
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

## Style-Bertで音声が出ない

まずb2vを経由せず、Style-Bert専用venvで短文WAV生成を確認してください。

問題を：

```text
b2v側
Style-Bert Server側
Python依存
TTSモデル
```

に切り分けるのが近道です。

---

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

Style-Bert-VITS2は別プロセスとして8603で動作し、b2vからHTTPで呼び出します。

b2v本体へStyle-Bertの重いPython依存を直接混ぜない構成です。

---

# テスト

```sh
.venv/bin/python -m pytest -q
```

OCR confidence、候補選択、低文字量ページ、人間編集、LLM処理、ページ除外、SQLite再読み込み、最終TXT等を検証します。

音声系はStyle-Bert ServerとFFmpegが必要です。

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

Style-Bert-VITS2本体、音声モデル、LLMモデル、Tesseract、FFmpegその他の依存物には、それぞれ別のライセンス・利用条件があります。したがって同梱しておりません。

特に音声モデルやLLMモデルは、コード本体と同じライセンスとは限りません。利用・再配布前に各配布元の条件を確認してください。

---

# 関連プロジェクト

- Style-Bert-VITS2  
  https://github.com/litagin02/Style-Bert-VITS2

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
→ Style-Bert-VITS2
→ WAV
→ MP3
```

まで一冊を通して生成できています。

今後の改善候補はありますが、まずは実際に作ったMP3を長時間聞き、必要になったものだけを追加する方針です。



2026/09/07 高尾　司
