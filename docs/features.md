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

### 送信先の振り分け

| 通知 | 振り分け順 |
| --- | --- |
| Issue / PR（コメント・作成・クローズ等すべて） | `issue_routes`（番号一致）→ `issue_channel_id` → `default_channel_id` |
| リリース | `release_channel_id` → `default_channel_id` |

`issue_routes` に番号を書くと、その Issue へのコメントも、作成・クローズ・再オープンも同じチャンネルに届きます。

```yaml
github:
  issue_channel_id: 111111111111111111     # #11 以外の Issue
  issue_routes:
    - repo: "coffin299/AshChan"            # 省略すると全リポジトリに適用
      numbers: [11]
      channel_id: 222222222222222222       # #11 専用
  release_channel_id: 333333333333333333   # リリース
```

### その他

- `X-Hub-Signature-256` で署名を検証し、`github.secret` 未設定ならサーバー自体を起動しない
- 本文中のテンプレート用 HTML コメント（`<!-- -->`）は除去し、`max_body_chars`（リリースは `max_release_body_chars`）で省略
- dependabot などボットの投稿は `ignore_bots` で除外（リリースは GitHub Actions 公開が多いため常に通知）
- 同じ配信の再送は無視
- 外部から届く必要があるので、Cloudflare Tunnel / ngrok 等のトンネルかポート開放が必要（[setup.md](setup.md) 参照）

## 管理コマンド

すべて管理者権限が必要で、結果は本人にだけ表示されます。

| コマンド | 内容 |
| --- | --- |
| `/ash status` | 稼働時間・レイテンシ・LLM 疎通・各機能の ON/OFF・GitHub Webhook の待受状態 |
| `/ash reload` | `config.yaml` と `prompt.md` を再読込し、AutoMod 再同期と GitHub Webhook サーバーの再起動 |
| `/ash honeypot count <user>` | ハニーポット検知回数を表示 |
| `/ash honeypot reset <user>` | ハニーポット検知回数を 0 に戻す |

## 通知先

| 種類 | 送信先 | 内容 |
| --- | --- | --- |
| dev 通知 | `notify.dev_channel_id` | 起動/停止、例外、モデレーション、AutoMod 同期、ハニーポット、アンチnuke |
| 緊急通知 | dev 通知 + `notify.emergency_dm_user_ids` への DM | アンチnuke 検知、ハニーポット BAN |
