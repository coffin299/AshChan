# 機能一覧

## 会話（NVIDIA NIM）

- トリガー: ボットへのメンション、またはボットの発言へのリプライ
- キャラ設定は `prompt.md`（`llm.prompt_file`）。末尾に Discord 運用ルールと現在時刻（JST）を自動付与
- 応答はストリーミングで、`llm.stream_edit_interval_sec`（既定2秒）ごとにメッセージを書き換え
- 1900文字を超えたら次のメッセージに続きを書く
- チャンネル単位で直近 `llm.history_turns` 往復を記憶（`data/ashchan.db`）
- 同じユーザーの連続呼び出しは `llm.user_cooldown_sec` の間 ⏳ リアクションで無視
- NIM がエラーのときは dev 通知せず、`llm.fallback_replies` からランダムに1つ返す
- ボットの発言は誰にもメンション通知を飛ばしません

## Web検索（DuckDuckGo）

- アッシュ（LLM）が必要と判断したとき `web_search` ツールを呼び出す（Function Calling）
- 検索中は、それまでの回答の末尾に `search.indicator`（既定 `検索中...🔍 {query}`）を表示
- 検索が終わると表示を消して回答の続きを流す
- 1返信あたり最大 `search.max_calls_per_reply` 回（既定3回）
- `search.show_sources` が ON なら、回答の末尾に参照 URL を小さく付記
- 絵文字は文字そのもの（🔍 など）で書く。`:mag:` のようなショートコードはボット投稿では変換されない
- カスタム絵文字を使う場合は `<:名前:絵文字ID>` 形式で書く

## モデレーション

| 検知 | 設定 |
| --- | --- |
| メンション過多 | `moderation.mention_limit` |
| 招待リンク | `moderation.block_invites` / `invite_patterns` |
| 疑わしいリンク（IPロガー・短縮URL・偽Discord/Steam等） | `moderation.block_suspicious_links` / `suspicious_link_patterns` |
| NGワード（詐欺定型） | `moderation.ng_words` |

- 違反したら「削除 + `timeout_minutes` 分のタイムアウト」、内容は dev 通知
- 編集で違反内容に書き換えた場合も検知
- 管理者・サーバーオーナー・`moderation.exempt_role_ids` は対象外
- 連投（短時間の連続投稿）は通常会話と区別できないため検知しない

### AutoMod 連携

起動時・`/ash reload` 時に `[Ash]` で始まる AutoMod ルールを作成/更新します。

| ルール | 内容 | 設定 |
| --- | --- | --- |
| `[Ash] NGワード` | `ng_words` を部分一致でブロック | `automod.keyword_rule` |
| `[Ash] メンションスパム` | `mention_limit` 超えをブロック | `automod.mention_spam_rule` |
| `[Ash] スパム疑い` | Discord 独自のスパム内容判定 | `automod.spam_rule` |

## アンチnuke

監査ログを「サーバー × 実行者 × 操作」単位で集計し、`antinuke.window_seconds` 秒以内に閾値以上の操作があれば
dev 通知チャンネルと緊急 DM 先に通知します（自動処罰はしません）。

| 操作 | 既定の閾値 |
| --- | --- |
| チャンネル作成 / 削除 | 5 / 3 |
| ロール作成 / 削除 | 5 / 3 |
| BAN / キック | 3 / 3 |
| Webhook 作成 | 3 |
| 危険権限（管理者等）を持つロールの付与・危険権限の追加 | 1 |

- このボット・サーバーオーナー・`antinuke.whitelist_user_ids` は対象外
- 同じ実行者・操作の通知は `alert_cooldown_seconds` の間繰り返さない

## ハニーポット

`honeypot.channel_ids` に投稿したユーザーを段階的に処罰します。

| 回数 | 処置 | 理由 |
| --- | --- | --- |
| 1〜3回目 | 投稿を全削除 → キック | `ハニーポット検知(n回目)` |
| 4回目 | BAN（直近7日のメッセージ削除） | `ハニーポット検知(4回目)` |

- キック回数は `honeypot.max_kicks`（既定3）。回数は DB に保存され、再参加しても累積
- 全投稿削除は「ボットが記録した投稿インデックス」+「各チャンネル履歴の走査（`purge_scan_days` 日分）」の2段構え
- `honeypot.exempt_role_ids` のロールを持つユーザー・ボット・オーナーは回避（カウントもしない）
- キックは dev 通知、BAN は dev 通知 + 緊急 DM

## FixLink

- `twitter.com` / `x.com` の投稿リンクを `fxtwitter.com` に置き換えて返信（通知なし）
- 元の投稿の埋め込みを非表示（`fixlink.suppress_original_embed`）
- `<https://...>` で埋め込みを止めたリンク、スポイラー `||...||`、コード内のリンクは対象外
- `fixlink.rules` に追加すれば Instagram / TikTok / Pixiv なども対応可能（config にコメントアウト例あり）

## ようこそメッセージ

- 新規参加者へ、アッシュの口調で `welcome.max_lines`（既定3）行以内の挨拶を LLM で生成して送信
- 送信先は `welcome.channel_id`（0 ならサーバーのシステムメッセージチャンネル）
- 通常のメッセージとして送るので、着地チャンネルを見られる全員に表示される
- 先頭で本人をメンション（メンション通知が飛ぶのは本人のみ。@everyone 等は飛ばない）
- `welcome.wait_for_screening` が ON なら、メンバーシップ審査を通過した時点で挨拶
- `welcome.extra_instructions` でサーバー案内（例: 質問は #help）を混ぜられる
- NIM エラー時、または1分あたり `max_llm_per_minute` 回を超えた大量参加時は `fallback_messages` の定型文
- ボットの参加には挨拶しない
- 既定は OFF（`welcome.enabled: true` で有効化）

## 自動ロール

- 新規参加者に `autorole.role_ids` のロールを付与（ボットには `autorole.bot_role_ids`）
- `autorole.wait_for_screening` が ON なら、メンバーシップ審査（ルール同意）を通過した時点で付与
- ボットより上位のロール・連携ロール・存在しないロール ID は付与せず、dev 通知で知らせる
- 既定は OFF（`autorole.enabled: true` で有効化）

## GitHub 通知（Webhook）

GitHub の Webhook をボット内の HTTP サーバーで受け取り、Discord チャンネルへ埋め込みで通知します。

| イベント | 内容 | 設定（既定） |
| --- | --- | --- |
| `issue_comment` | Issue / PR への新規コメント | `github.events.issue_comment`（ON） |
| `issues` | Issue の作成・クローズ・再オープン | `github.events.issues`（ON） |
| `pull_request` | PR の作成・クローズ・マージ・再オープン | `github.events.pull_request`（OFF） |
| `pull_request_review_comment` | PR の差分行へのレビューコメント | `github.events.pull_request_review_comment`（OFF） |
| `release` | リリースの公開（プレリリース含む） | `github.events.release`（ON） |
| `push` | ブランチへのコミット push（タグ・ブランチ削除は除く） | `github.events.push`（OFF） |

### 送信先の振り分け

| 通知 | 振り分け順 |
| --- | --- |
| Issue / PR（コメント・作成・クローズ等すべて） | `issue_routes`（番号一致）→ `issue_channel_id` → `default_channel_id` |
| リリース | `release_channel_id` → `default_channel_id` |
| コミット（push） | `commit_channel_id` → `default_channel_id` |

`issue_routes` に番号を書くと、その Issue へのコメントも、作成・クローズ・再オープンも同じチャンネルに届きます。

```yaml
github:
  issue_channel_id: 111111111111111111     # #11 以外の Issue
  issue_routes:
    - repo: "coffin299/AshChan"            # 省略すると全リポジトリに適用
      numbers: [11]
      channel_id: 222222222222222222       # #11 専用
  release_channel_id: 333333333333333333   # リリース
  commit_channel_id: 444444444444444444    # コミット（events.push: true が必要）
  commit_branches: ["main"]                # 空 [] なら全ブランチ
```

コミット通知は 1 回の push を 1 件の埋め込みにまとめ、「短縮ハッシュ（リンク）・件名・作者」を最大 `max_commits` 件まで並べます（超過分は「…ほか n 件」）。タイトルのリンクは差分比較ページです。

### その他

- `X-Hub-Signature-256` で署名を検証し、`github.secret` 未設定ならサーバー自体を起動しない
- 本文中のテンプレート用 HTML コメント（`<!-- -->`）は除去し、`max_body_chars`（リリースは `max_release_body_chars`）で省略
- dependabot などボットの投稿は `ignore_bots` で除外（リリースは GitHub Actions 公開が多いため常に通知）
- 同じ配信の再送は無視
- 外部から届く必要があるので、Cloudflare Tunnel / ngrok 等のトンネルかポート開放が必要（[setup.md](setup.md) 参照）

## Discord ステータス（いま何してるか）

- `presence.enabled` で ON/OFF（既定 ON）
- カスタムステータスに短文を出し、`presence.rotate_sec` ごとにローテ
- 優先度: RAG ジョブ → サポート中（`サポート中/{フォーラム名}`、複数なら順に）→ 会話中 → 待機文言
- 待機文言は `presence.idle_statuses`（例: 通常待機 / 暇だよ）

## VRCast サポートフォーラム（RAG）

- `support.enabled` と `support.forum_channel_ids` で対象フォーラムを指定
- **新規スレ作成時**: メンションなしで自動応答（軽い自己紹介 + 情報要求）
- **以降**: ボット発言へのリプライのみ応答（通常の ChatCog は対象スレを無視）
- 応答中はトークンストリームではなく「いま何してるか」のステータス表示
- 完了後は Components V2: 本文 + `footer_hint` + 「解決した」「開発者に聞く」（**最新メッセージのみ**）
- `.txt` / `.log` 添付を読み取り、プロンプトに注入
- LLM / RAG 失敗時は 30 秒間隔で最大 3 回再試行。だめなら管理者メンション + 失敗 V2（ボタン無効）+ スレ `failed`
- 「開発者に聞く」時は会話履歴と `.txt`/`.log` から **開発者向け引き継ぎメモ**（症状・環境・再現・ログ要点・未解決点など）を LLM で生成してメンション付き投稿
- ナレッジ: `coffin299/VRCast` の docs + ソース（`include_globs` / `exclude_globs`）
- 初回: `bootstrap_rag.bat`（LM Studio）または `python -m ash.rag bootstrap`
- 本番揃え: `/ash rag rebuild-nim`（NIM 全文再構築、40RPM）
- リリース Webhook: 対象 repo の published で差分 sync

## 管理コマンド

すべて管理者権限が必要で、結果は本人にだけ表示されます。

| コマンド | 内容 |
| --- | --- |
| `/ash status` | 稼働時間・レイテンシ・LLM 疎通・各機能の ON/OFF・GitHub Webhook・サポート RAG |
| `/ash reload` | `config.yaml` と `prompt.md` を再読込し、AutoMod 再同期と GitHub Webhook サーバーの再起動 |
| `/ash honeypot count <user>` | ハニーポット検知回数を表示 |
| `/ash honeypot reset <user>` | ハニーポット検知回数を 0 に戻す |
| `/ash rag status` | RAG のタグ・チャンク数・埋め込みモデル |
| `/ash rag sync [tag]` | 最新（または指定）リリースまで差分更新 |
| `/ash rag rebuild-nim [tag]` | NIM で全文作り直し（初回ローカル後の本番用） |
| `/ash rag cancel` | 実行中の RAG ジョブをキャンセル |

## 通知先

| 種類 | 送信先 | 内容 |
| --- | --- | --- |
| dev 通知 | `notify.dev_channel_id` | 起動/停止、例外、モデレーション、AutoMod 同期、ハニーポット、アンチnuke |
| 緊急通知 | dev 通知 + `notify.emergency_dm_user_ids` への DM | アンチnuke 検知、ハニーポット BAN |
