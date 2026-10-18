# AshChan

VRChat の ASH ちゃん風キャラクター「アッシュ」が住む Discord ボットです。
会話（NVIDIA NIM）に加えて、モデレーション・アンチnuke・ハニーポット・FixLink を1つにまとめています。
単体サーバーでの運用を想定しています。

## 機能

| 機能 | 概要 |
| --- | --- |
| 会話 | メンション/リプライに NVIDIA NIM でストリーミング応答（2秒ごとに更新）。キャラ設定は [prompt.md](prompt.md) |
| Web検索 | アッシュが必要と判断したら DuckDuckGo で検索。検索中は `検索中...🔍 <クエリ>` を表示 |
| モデレーション | メンション過多・招待リンク・疑わしいリンク・NGワードを削除+タイムアウト。Discord AutoMod ルールも自動作成 |
| アンチnuke | 監査ログを実行者単位で集計し、大量削除・大量BAN・危険権限付与などを dev 通知+緊急DM |
| ハニーポット | 罠チャンネルに投稿したら 1〜3回目: 全投稿削除+キック、4回目: BAN（理由「ハニーポット検知(n回目)」） |
| FixLink | Twitter/X のリンクを `fxtwitter.com` に置き換えて返信し、元の埋め込みを非表示 |
| ようこそメッセージ | 新規参加者へアッシュの口調で 1〜3 行の挨拶を LLM で生成し、着地チャンネルへ送信 |
| 自動ロール | 新規参加者に指定ロールを自動付与（メンバーシップ審査の通過待ちにも対応） |
| GitHub 通知 | GitHub Webhook を受信し、Issue / PR のコメント・状態変化、リリース、コミットを通知。Issue 番号ごと・リリース用・コミット用に送信先チャンネルを指定可能（署名検証あり） |
| VRCast サポート RAG | 指定フォーラムの新規スレに自動応答。リプライで継続。コードベース RAG + Components V2（解決／開発者エスカレーション）。初回は `bootstrap_rag.bat`（LM Studio）、本番は `/ash rag rebuild-nim` |
| 管理コマンド | `/ash status` `/ash reload` `/ash honeypot …` `/ash rag status|sync|rebuild-nim|cancel` |

詳細は [docs/features.md](docs/features.md) を参照してください。

## クイックスタート（Windows）

1. Python 3.11 をインストール（`winget install Python.Python.3.11`）
2. `start.bat` を実行 → `.venv` 作成・パッケージ導入・`config.yaml` 生成
3. `config.yaml` の `discord.token` と `llm.api_key` などを編集
4. もう一度 `start.bat` を実行して起動

Discord Developer Portal での設定や必要な権限は [docs/setup.md](docs/setup.md) を参照してください。

## ファイル構成

```
AshChan/
├── start.bat              # 起動スクリプト（初回は .venv / config.yaml を自動生成）
├── requirements.txt
├── config.default.yaml    # 設定テンプレート（全項目に詳細コメント付き）
├── config.yaml            # 自動生成・Git 管理外
├── prompt.md              # アッシュのキャラ設定（システムプロンプト）
├── ash/
│   ├── __main__.py        # エントリポイント（python -B -m ash）
│   ├── bot.py             # Bot 本体
│   ├── config.py          # 設定の読み込み
│   ├── db.py              # SQLite（data/ashchan.db）
│   ├── notifier.py        # dev 通知 / 緊急DM
│   ├── utils.py
│   ├── llm/               # NIM クライアント・DDG 検索・ストリーミング表示
│   ├── rag/               # VRCast RAG（bootstrap / diff / retrieve）
│   └── cogs/              # chat / support / moderation / … / github / admin
├── bootstrap_rag.bat      # LM Studio 向け初回 RAG（英語メッセージ）
├── prompt_support.md      # サポートモード追加プロンプト
├── data/                  # 実行時データ（Git 管理外）
├── logs/                  # ログ（Git 管理外）
└── docs/                  # ドキュメント
```

## セキュリティ

- `config.yaml` にはトークンと API キーが入ります。コミット・共有しないでください（`.gitignore` 済み）。
- 環境変数 `ASH_DISCORD_TOKEN` / `NVIDIA_API_KEY` / `GITHUB_WEBHOOK_SECRET` を設定すると、config.yaml の値より優先されます。
- ハニーポットは BAN を伴うので、まずテストサーバーで動作を確認してください。

## ライセンス

[LICENSE](LICENSE) を参照してください。
