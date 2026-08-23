# 冷笑検知bot

Discordサーバー内の皮肉・見下すような発言（冷笑）をキーワード/パターンマッチで検知し、
サーバーのカスタム絵文字 `:reisho:` でリアクション＋リプライ通知するbotです。
検知回数はサーバーごと・ユーザーごとに記録され、`!ranking` でランキング表示できます。

## 機能概要

- キーワード/パターンの重み付けスコア方式で冷笑を検知（`keywords.json` で調整可能）
  - 標準搭載ワード例: `うお` / `うおｗ` / `かっけー` / `かっけーｗ` / `かっこよ` / `かっこよｗ` /
    `お、おう` / `きちー` / `きちーｗ` など
- 合計スコアが `threshold` を超えたら発火
- 発火時: サーバーのカスタム絵文字 `:reisho:` でリアクション + 「◯◯さん 冷笑を検知しました！内容：〜」とリプライ
- **特殊コンボ検知①**: 同一ユーザーが「う」→「お」（または「お」→「う」）を1文字ずつ連投すると検知
- **特殊コンボ検知②**: メッセージに 🇺（regional_indicator_u）→ 🇴（regional_indicator_o）の順でリアクションを付けると、
  **リアクションした人** が検知される（メッセージ投稿者ではない点に注意）
- bot自身の発言・リアクション（このbotを含む全bot）は検知対象外
- `!ranking` : サーバー内の冷笑回数ランキングを**全員分**詳細表示（順位・名前・回数・全体に占める割合）
- `!reisho_help` : ヘルプ表示
- Koyebでの常時稼働向けに、ヘルスチェック用の簡易HTTPサーバーを同梱
- **🎭 解禁演出**: bot全体で一度でも冷笑を検知するまでは、ニックネームが `???`。
  初めて検知した瞬間に、参加している**全サーバー**でニックネームが `冷笑検知bot` に、
  アイコンが `assets/reisho_icon.jpg`（同梱の「冷笑」ロゴ画像）に変わる
- **起動前の過去メッセージは検知しない**: bot接続時刻より前に投稿されたメッセージは無視します
  （通信の瞬断による再接続時に、Discord側から欠落メッセージが再配信されるケースもガード済み）

### コンボ検知のタイムアウト

環境変数で猶予秒数を調整できます。

- `REISHO_TEXT_COMBO_WINDOW_SECONDS` : 「う」→「お」連投の許容秒数（既定60秒）
- `REISHO_REACTION_COMBO_WINDOW_SECONDS` : 🇺→🇴リアクションの許容秒数（既定30秒）

### 解禁演出の仕組みについて

- **判定単位**: bot全体で一度でも冷笑を検知したら解禁（サーバーごとの個別判定ではありません）
- **ニックネーム**はサーバーごとの設定なので、解禁時に参加している全サーバーへ順番に反映します
- **アイコン**はbotアカウント自体の設定（グローバル）なので、1回変更するだけで自動的に全サーバーに反映されます
- 解禁状態は `bot_state.json` に保存されます（Koyebのディスクが再デプロイ時にリセットされる場合、解禁状態も失われる点は「データの永続化について」の節と同様です）
- アイコン画像は `assets/reisho_icon.jpg` に同梱しています。別の画像に差し替えたい場合は同じパスに上書きするか、
  環境変数 `REISHO_ICON_PATH` で別パスを指定してください
- ニックネームの文言は環境変数 `REISHO_LOCKED_NICK`（既定 `???`）、`REISHO_UNLOCKED_NICK`（既定 `冷笑検知bot`）で変更できます

## 1. Discord Bot の準備

1. [Discord Developer Portal](https://discord.com/developers/applications) で新規Applicationを作成
2. 「Bot」タブでBotを作成し、**TOKEN** を控える（後で環境変数 `DISCORD_TOKEN` に設定）
3. 「Bot」タブの **Privileged Gateway Intents** で **MESSAGE CONTENT INTENT** を ON にする
   （これをONにしないとメッセージ本文が読めず、検知できません）
4. 「OAuth2 > URL Generator」で
   - SCOPES: `bot`
   - BOT PERMISSIONS: `Read Messages/View Channels`, `Send Messages`, `Add Reactions`,
     `Read Message History`, `Change Nickname`（解禁演出でニックネームを変更するために必要）
   を選び、生成されたURLからサーバーに招待する

## 2. カスタム絵文字 `:reisho:` の準備

サーバーの設定 > 絵文字 から、名前が `reisho` のカスタム絵文字を1つ登録してください
（絵文字名は環境変数 `REISHO_EMOJI_NAME` で変更可能）。
見つからない場合は自動的に 😏 にフォールバックします。

## 3. ローカルでの動作確認

```bash
pip install -r requirements.txt
export DISCORD_TOKEN="あなたのトークン"
python bot.py
```

## 4. Koyebへのデプロイ

このプロジェクトには `Dockerfile` を同梱しているので、そのままKoyebにデプロイできます。

1. このフォルダをGitHubリポジトリにpush
2. Koyebのダッシュボードで「Create Service」→ GitHubリポジトリを選択
3. Builder: **Dockerfile** を選択（自動検出されます）
4. 環境変数 (Environment variables) に以下を設定
   - `DISCORD_TOKEN` : Botのトークン（必須・Secretとして設定推奨）
   - `REISHO_EMOJI_NAME` : カスタム絵文字名（省略時 `reisho`）
   - `PORT` : Koyebが自動設定するので基本触らなくてOK
5. Service type は **Web Service** のままでOK（ヘルスチェック用に `bot.py` 内で簡易HTTPサーバーを起動しています）
   - Health check path は `/` のままで問題ありません
6. デプロイ後、Botがサーバーにオンライン表示されれば成功です

### 注意: データの永続化について

`reisho_counts.json` にランキング用のカウントを保存していますが、Koyebの通常のディスクは
**再デプロイ時にリセットされる**場合があります。長期的にランキングを保持したい場合は、
以下のいずれかを検討してください。

- Koyebの **Persistent Volume** 機能をアタッチする
- SQLite/Postgresなど外部データベースに保存先を変更する（要コード修正）

## 5. 検知ワードのチューニング

`keywords.json` を編集することで、検知パターンと重み、しきい値を自由に調整できます。

```json
{
  "threshold": 3,
  "patterns": [
    { "pattern": "\\(棒\\)", "weight": 3 },
    { "pattern": "w{3,}", "weight": 1 }
  ]
}
```

- `pattern` : 正規表現（Pythonの `re` モジュール構文）
- `weight` : マッチした場合に加算されるスコア
- 1メッセージ中の合計スコアが `threshold` 以上で検知発火

誤検知が多い場合はしきい値を上げる、逆に反応が鈍い場合は下げる・パターンを追加する、
といった形で調整してください。キーワードマッチ方式のため完璧な精度は出ませんが、
まずはこの方式で運用しつつ様子を見るのがおすすめです。
