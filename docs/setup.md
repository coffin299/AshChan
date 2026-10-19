# セットアップ

## 1. Python 3.11

`start.bat` は Python 3.11 専用です（`py -3.11` で起動します）。

```powershell
winget install Python.Python.3.11
```

既存の `.venv` が 3.11 以外で作られていた場合、`start.bat` が自動で作り直します。

## 2. Discord ボットの作成

1. [Discord Developer Portal](https://discord.com/developers/applications) で Application を作成
2. **Bot** タブでトークンを発行（`Reset Token`）→ `config.yaml` の `discord.token` へ
3. **Privileged Gateway Intents** で次を ON
   - `MESSAGE CONTENT INTENT`（会話・モデレーション・FixLink）
   - `SERVER MEMBERS INTENT`（ハニーポット・ロール判定・自動ロール・ようこそメッセージ）
4. **OAuth2 → URL Generator** でスコープ `bot` と `applications.commands` を選び、次の権限で招待

| 権限 | 用途 |
| --- | --- |
| View Channels / Send Messages / Embed Links / Read Message History | 会話・通知 |
| Add Reactions | クールダウン中の ⏳ |
| Manage Messages | 違反削除・ハニーポットの全投稿削除・FixLink の埋め込み非表示 |
| Moderate Members | タイムアウト |
| Kick Members / Ban Members | ハニーポット |
| View Audit Log | アンチnuke |
| Manage Server | AutoMod ルールの作成/更新 |
| Manage Roles | 自動ロール |

> ボットのロールは、処罰対象になりうるメンバーのロールより上に配置してください。

## 3. NVIDIA NIM の API キー

1. [build.nvidia.com](https://build.nvidia.com) にログインし API キー（`nvapi-...`）を発行
2. `config.yaml` の `llm.api_key` へ設定
3. Web検索を使う場合は Tool Calling 対応モデルを `llm.model` に指定（既定の `meta/llama-3.3-70b-instruct` は対応）
4. 混雑時用に `llm.models` へフォールバックモデルを並べられる（`model` 失敗時に上から順に試行）

## 4. GitHub 通知（使う場合のみ）

1. `config.yaml` の `github.enabled: true`、`github.secret` にランダムな長い文字列、送信先チャンネル（`issue_channel_id` / `issue_routes` / `release_channel_id` / `commit_channel_id`）を設定。コミット通知を使う場合は `github.events.push: true` も設定
2. ボットのポート（既定 `127.0.0.1:8080`）を外部から届くようにする
   - 推奨: [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) や [ngrok](https://ngrok.com/) で `http://127.0.0.1:8080` を公開
   - ポート開放する場合は `github.host` を `"0.0.0.0"` にし、ルーターとファイアウォールで該当ポートを許可
3. GitHub のリポジトリ → **Settings → Webhooks → Add webhook**

| 項目 | 値 |
| --- | --- |
| Payload URL | `https://<公開URL>/github/webhook` |
| Content type | `application/json` |
| Secret | `github.secret` と同じ文字列 |
| Events | Issue comments / Issues / Releases（必要なら Pull requests / Pull request review comments / Pushes も） |

登録直後に GitHub が送る `ping` に応答できれば、Webhook 画面に緑のチェックが付きます。
`/ash status` の「GitHub通知」で待受状態を確認できます。

## 5. VRCast サポート RAG（使う場合）

1. Discord にサポート用 **フォーラム** を作り、必要なら「解決済み」タグを用意する
2. `config.yaml` の `support` を設定:

| キー | 内容 |
| --- | --- |
| `support.enabled` | `true` |
| `support.forum_channel_ids` | フォーラムチャンネル ID |
| `support.admin_user_ids` | 失敗時・エスカレーション先 |
| `support.local_repo_path` | 任意。空なら GitHub リモートから取得（`support.repo`） |
| `support.repo` | 既定 `coffin299/VRCast` |
| `support.github_token` | GitHub API 用トークン（remote 取得の推奨）。`GITHUB_TOKEN` 環境変数でも可 |

3. 初回インデックス: ローカルなら `bootstrap_rag.bat`（LM Studio）、本番は `/ash rag rebuild-nim`（パス空でも GitHub から取得可）。ヘルプサイト（`webpage` ブランチ）も同時に取り込む
4. 以降は GitHub Releases Webhook または `/ash rag sync` で差分更新

ボットにはフォーラムでの「メッセージ送信」「スレッド管理」（タグ付与・アーカイブ）権限が必要です。

## 6. 起動

```bat
start.bat
```

| 実行回 | 動作 |
| --- | --- |
| 初回 | `.venv` 作成 → パッケージ導入 → `config.yaml` 生成して終了 |
| 2回目以降 | `requirements.txt` が変わっていれば再導入 → ボット起動 |

起動は `python -B -m ash`（`PYTHONDONTWRITEBYTECODE=1`）で行うため、`__pycache__` は作られません。

## 7. 最低限の設定項目

| キー | 内容 |
| --- | --- |
| `discord.token` | ボットトークン（必須） |
| `discord.guild_ids` | スラッシュコマンドを即時同期するサーバー ID |
| `discord.allowed_guild_ids` | 機能を許可するサーバー（空=全サーバー。指定時は LLM 等すべてその一覧のみ） |
| `notify.dev_channel_id` | 非公開ログ（エラー・モデレーション等） |
| `notify.public_channel_id` | 公開可ログ（起動/停止・RAG 完了等）。0 なら dev へフォールバック |
| `notify.emergency_dm_user_ids` | 緊急 DM 先ユーザー |
| `llm.api_key` | NVIDIA NIM の API キー |
| `honeypot.channel_ids` | 罠チャンネル |
| `honeypot.exempt_role_ids` | ハニーポットを回避できるロール |
| `autorole.enabled` / `autorole.role_ids` | 新規参加者に付与するロール |
| `welcome.enabled` / `welcome.channel_id` | ようこそメッセージと着地チャンネル |
| `support.enabled` / `forum_channel_ids` / `local_repo_path` | サポート RAG（任意） |

各項目の詳しい説明は `config.yaml` 内のコメントを参照してください。
設定を変えたら `/ash reload` で再起動なしに反映できます（トークン以外）。
