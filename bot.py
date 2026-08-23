import os
import re
import json
import asyncio
import logging
import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import discord
from discord.ext import commands, tasks

# ------------------------------------------------------------
# 設定
# ------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("reisho-bot")

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
COMMAND_PREFIX = os.environ.get("COMMAND_PREFIX", "!")
EMOJI_NAME = os.environ.get("REISHO_EMOJI_NAME", "reisho")  # サーバーのカスタム絵文字名
KEYWORDS_PATH = os.environ.get("KEYWORDS_PATH", "keywords.json")
COUNTS_PATH = os.environ.get("COUNTS_PATH", "reisho_counts.json")
PORT = int(os.environ.get("PORT", 8000))

# 「う」→「お」連投コンボの許容秒数
TEXT_COMBO_WINDOW_SECONDS = float(os.environ.get("REISHO_TEXT_COMBO_WINDOW_SECONDS", 60))
# 🇺 → 🇴 リアクション連続コンボの許容秒数
REACTION_COMBO_WINDOW_SECONDS = float(os.environ.get("REISHO_REACTION_COMBO_WINDOW_SECONDS", 30))

# リージョナルインジケーター U / O (Unicode)
REGIONAL_U = "\U0001F1FA"  # 🇺
REGIONAL_O = "\U0001F1F4"  # 🇴

# ------------------------------------------------------------
# 「解禁」演出の設定
# bot全体で一度でも冷笑を検知したら、参加している全サーバーでニックネームと
# アイコン(botアカウント自体の設定なので変更すると自動的に全サーバーへ反映される)を切り替える。
# ------------------------------------------------------------
STATE_PATH = os.environ.get("STATE_PATH", "bot_state.json")
LOCKED_NICK = os.environ.get("REISHO_LOCKED_NICK", "???")
UNLOCKED_NICK = os.environ.get("REISHO_UNLOCKED_NICK", "冷笑検知bot")
# 解禁後に使うアイコン画像のパス。リポジトリ直下にこのファイルを置いてください。
ICON_PATH = os.environ.get("REISHO_ICON_PATH", "assets/reisho_icon.jpg")

if not DISCORD_TOKEN:
    raise RuntimeError("環境変数 DISCORD_TOKEN が設定されていません。")

# ------------------------------------------------------------
# キーワード設定の読み込み
# ------------------------------------------------------------
def load_keyword_config(path: str):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    threshold = data.get("threshold", 3)
    compiled = []
    for item in data.get("patterns", []):
        try:
            regex = re.compile(item["pattern"], re.IGNORECASE)
        except re.error as e:
            log.warning("パターンのコンパイルに失敗しました: %s (%s)", item.get("pattern"), e)
            continue
        compiled.append((regex, item.get("weight", 1)))
    return threshold, compiled


THRESHOLD, PATTERNS = load_keyword_config(KEYWORDS_PATH)
log.info("キーワード設定を読み込みました: %d件のパターン, しきい値=%d", len(PATTERNS), THRESHOLD)


def calc_reisho_score(text: str) -> int:
    """メッセージ本文から冷笑スコアを計算する"""
    score = 0
    for regex, weight in PATTERNS:
        if regex.search(text):
            score += weight
    return score


# ------------------------------------------------------------
# カウント永続化 (簡易JSONファイル)
# 注意: Koyebのデフォルトのディスクは再デプロイ時に消える場合があります。
# 長期集計が必要な場合は Persistent Volume か外部DBの利用を検討してください。
# ------------------------------------------------------------
_counts_lock = asyncio.Lock()


def _load_counts() -> dict:
    if not os.path.exists(COUNTS_PATH):
        return {}
    try:
        with open(COUNTS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        log.warning("カウントファイルの読み込みに失敗したため初期化します。")
        return {}


def _save_counts(counts: dict) -> None:
    tmp_path = COUNTS_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(counts, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, COUNTS_PATH)


async def increment_count(guild_id: int, user_id: int) -> int:
    async with _counts_lock:
        counts = _load_counts()
        g = counts.setdefault(str(guild_id), {})
        g[str(user_id)] = g.get(str(user_id), 0) + 1
        _save_counts(counts)
        return g[str(user_id)]


async def get_ranking(guild_id: int, limit: int | None = None):
    async with _counts_lock:
        counts = _load_counts()
        g = counts.get(str(guild_id), {})
        ranked = sorted(g.items(), key=lambda kv: kv[1], reverse=True)
        return ranked[:limit] if limit else ranked


# ------------------------------------------------------------
# 「解禁」状態の永続化 (bot全体で1つだけ持つフラグ)
# ------------------------------------------------------------
_state_lock = asyncio.Lock()


def _load_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return {"unlocked": False}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        log.warning("状態ファイルの読み込みに失敗したため初期化します。")
        return {"unlocked": False}


def _save_state(state: dict) -> None:
    tmp_path = STATE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, STATE_PATH)


async def is_unlocked() -> bool:
    async with _state_lock:
        return _load_state().get("unlocked", False)


async def mark_unlocked() -> bool:
    """未解禁 -> 解禁 に変更する。実際に変更が起きた場合のみ True を返す。"""
    async with _state_lock:
        state = _load_state()
        if state.get("unlocked", False):
            return False
        state["unlocked"] = True
        _save_state(state)
        return True


# ------------------------------------------------------------
# Discord Bot 本体
# ------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True  # Developer PortalでもMESSAGE CONTENT INTENTを有効にすること
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents, help_command=None)

# (channel_id, author_id) -> {"content": "う"|"お", "timestamp": datetime}
# 「う」→「お」(または「お」→「う」) の単文字連投コンボ検知用の状態
_single_char_state: dict[tuple[int, int], dict] = {}

# (message_id, user_id) -> [(emoji_str, timestamp), ...]
# 🇺 → 🇴 リアクション連続コンボ検知用の状態
_reaction_sequence_state: dict[tuple[int, int], list] = {}

# botが接続(オンライン化)した時刻。これより前に投稿されたメッセージは検知対象外にする。
# (再接続時にDiscordから欠落メッセージが再配信されるケースへの保険も兼ねる)
BOT_READY_AT = None  # type: datetime.datetime | None


async def _set_nick(guild: discord.Guild, nick: str) -> None:
    if guild.me is None:
        return
    if guild.me.nick == nick:
        return  # 既に反映済み
    try:
        await guild.me.edit(nick=nick)
    except discord.Forbidden:
        log.warning(
            "サーバー「%s」でニックネームを変更する権限がありません（「ニックネームの変更」権限を確認してください）。",
            guild.name,
        )
    except discord.HTTPException as e:
        log.warning("サーバー「%s」でのニックネーム変更に失敗しました: %s", guild.name, e)


async def apply_appearance_to_guild(guild: discord.Guild) -> None:
    """現在の解禁状態に合わせて、そのサーバーでのニックネームを揃える"""
    nick = UNLOCKED_NICK if await is_unlocked() else LOCKED_NICK
    await _set_nick(guild, nick)


async def unlock_bot_globally() -> None:
    """
    bot全体で初めて冷笑を検知したときに1回だけ実行される。
    ・全参加サーバーのニックネームを 冷笑検知bot に変更
    ・botアイコンを解禁後アイコンに変更（アイコンはbotアカウント自体の設定なので
      1回変更するだけで自動的に全サーバーに反映される）
    """
    became_unlocked = await mark_unlocked()
    if not became_unlocked:
        return  # 既に解禁済み

    log.info("🎉 初めての冷笑を検知しました。ニックネームとアイコンを解禁します。")

    # ニックネームを全サーバーで変更
    for guild in bot.guilds:
        await _set_nick(guild, UNLOCKED_NICK)

    # アイコン(グローバル)を変更
    if os.path.exists(ICON_PATH):
        try:
            with open(ICON_PATH, "rb") as f:
                avatar_bytes = f.read()
            await bot.user.edit(avatar=avatar_bytes)
            log.info("botのアイコンを解禁後アイコンに変更しました。")
        except discord.HTTPException as e:
            log.warning(
                "アイコンの変更に失敗しました（Discordのレート制限の可能性があります）: %s", e
            )
    else:
        log.warning(
            "アイコン画像が見つかりません: %s 。ニックネームのみ変更しました。"
            "画像を配置して再起動すると、次に解禁条件を満たしたタイミングで反映されます"
            "（既に解禁済みの場合は手動での再アップロードが必要です）。",
            ICON_PATH,
        )

@bot.event
async def on_guild_join(guild: discord.Guild):
    log.info("新しいサーバーに参加しました: %s", guild.name)
    await apply_appearance_to_guild(guild)

    if update_presence.is_running():
        await update_presence()

@bot.event
async def on_guild_remove(guild: discord.Guild):
    log.info("サーバーから退出しました: %s", guild.name)

    if update_presence.is_running():
        await update_presence()


@bot.event
async def on_message(message: discord.Message):
    # Bot自身(このbot含む全てのbot)の発言は検知しない
    if message.author.bot:
        return
    # DMは対象外(サーバーのカスタム絵文字が使えないため)
    if message.guild is None:
        return
    # bot接続前(再接続時の欠落メッセージ再配信を含む)に投稿されたメッセージは検知しない
    if BOT_READY_AT is not None and message.created_at < BOT_READY_AT:
        return

    content = message.content
    stripped = content.strip()

    # --- 「う」「お」単文字連投コンボ検知 ---
    if stripped in ("う", "お"):
        key = (message.channel.id, message.author.id)
        prev = _single_char_state.get(key)
        now = discord.utils.utcnow()
        if (
            prev is not None
            and prev["content"] != stripped
            and (now - prev["timestamp"]).total_seconds() <= TEXT_COMBO_WINDOW_SECONDS
        ):
            combo_text = f"{prev['content']}→{stripped}（連続投稿）"
            await credit_reisho(
                guild=message.guild,
                reaction_target=message,
                reply_target=message,
                credited_user=message.author,
                content_preview=combo_text,
            )
            _single_char_state.pop(key, None)
        else:
            _single_char_state[key] = {"content": stripped, "timestamp": now}
        # 単文字コンボはこれで完結させ、通常のスコア判定はスキップする
        await bot.process_commands(message)
        return

    score = calc_reisho_score(content)

    if score >= THRESHOLD:
        content_preview = content
        if len(content_preview) > 300:
            content_preview = content_preview[:300] + "…"
        await credit_reisho(
            guild=message.guild,
            reaction_target=message,
            reply_target=message,
            credited_user=message.author,
            content_preview=content_preview,
        )

    # コマンド処理も忘れずに実行
    await bot.process_commands(message)


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    # DM / bot自身のリアクションは対象外
    if payload.guild_id is None:
        return
    if payload.member is not None and payload.member.bot:
        return
    if bot.user is not None and payload.user_id == bot.user.id:
        return

    emoji_str = payload.emoji.name
    if emoji_str not in (REGIONAL_U, REGIONAL_O):
        return

    now = discord.utils.utcnow()
    key = (payload.message_id, payload.user_id)
    seq = _reaction_sequence_state.setdefault(key, [])
    seq.append((emoji_str, now))
    # 古いエントリを掃除
    seq[:] = [
        (e, t) for (e, t) in seq
        if (now - t).total_seconds() <= REACTION_COMBO_WINDOW_SECONDS
    ]

    if len(seq) >= 2 and seq[-2][0] == REGIONAL_U and seq[-1][0] == REGIONAL_O:
        guild = bot.get_guild(payload.guild_id)
        if guild is None:
            return
        channel = guild.get_channel(payload.channel_id) or bot.get_channel(payload.channel_id)
        if channel is None:
            return
        try:
            target_message = await channel.fetch_message(payload.message_id)
        except discord.HTTPException:
            return

        reactor = payload.member or guild.get_member(payload.user_id)
        if reactor is None:
            return

        await credit_reisho(
            guild=guild,
            reaction_target=target_message,
            reply_target=target_message,
            credited_user=reactor,
            content_preview="🇺🇴 リアクション（連続）",
        )
        _reaction_sequence_state.pop(key, None)


async def credit_reisho(
    guild: discord.Guild,
    reaction_target: discord.Message,
    reply_target: discord.Message,
    credited_user: discord.abc.User,
    content_preview: str,
):
    """冷笑を検知したユーザーにリアクション・通知・カウントを行う共通処理"""

    # サーバー内のカスタム絵文字 :reisho: を探す
    emoji = discord.utils.get(guild.emojis, name=EMOJI_NAME)

    try:
        if emoji is not None:
            await reaction_target.add_reaction(emoji)
        else:
            await reaction_target.add_reaction("😏")
            log.warning(
                "サーバー「%s」にカスタム絵文字 :%s: が見つかりません。Unicode絵文字で代替しました。",
                guild.name, EMOJI_NAME,
            )
    except discord.HTTPException as e:
        log.warning("リアクション付与に失敗しました: %s", e)

    total = await increment_count(guild.id, credited_user.id)

    reply_text = (
        f"{credited_user.mention} 冷笑を検知しました！　"
        f"内容：{content_preview}\n"
        f"(このサーバーでの累計冷笑回数: {total}回)"
    )

    try:
        await reply_target.reply(reply_text, mention_author=True)
    except discord.HTTPException as e:
        log.warning("リプライ送信に失敗しました: %s", e)

    # bot全体で初めての検知なら、ニックネーム/アイコンを解禁する
    await unlock_bot_globally()


# ------------------------------------------------------------
# コマンド: ランキング
# ------------------------------------------------------------
MAX_RANKING_DISPLAY = 25  # Embed descriptionの長さを考慮した表示上限

@tasks.loop(seconds=30)
async def update_presence():
    total_members = sum(
        guild.member_count or 0
        for guild in bot.guilds
    )

    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.Game(
            name=f"{len(bot.guilds)}サーバー｜{total_members}名を監視中"
        )
    )


@update_presence.before_loop
async def before_update_presence():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    global BOT_READY_AT

    BOT_READY_AT = discord.utils.utcnow()

    log.info(
        "ログイン完了: %s (id=%s) / 基準時刻=%s",
        bot.user,
        bot.user.id,
        BOT_READY_AT.isoformat()
    )

    for guild in bot.guilds:
        await apply_appearance_to_guild(guild)

    if not update_presence.is_running():
        update_presence.start()

@bot.command(name="ranking")
async def ranking_cmd(ctx: commands.Context):
    ranked = await get_ranking(ctx.guild.id)  # 全件取得

    if not ranked:
        await ctx.reply("まだ冷笑は検知されていません。平和ですね。")
        return

    total_all = sum(count for _, count in ranked)
    display_list = ranked[:MAX_RANKING_DISPLAY]

    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, (user_id, count) in enumerate(display_list):
        member = ctx.guild.get_member(int(user_id))
        name = member.display_name if member else f"(不明なユーザー: {user_id})"
        prefix = medals[i] if i < len(medals) else f"`{i + 1:>2}位`"
        share = (count / total_all * 100) if total_all else 0
        lines.append(f"{prefix} **{name}** — {count}回 ({share:.1f}%)")

    embed = discord.Embed(
        title="🏆 冷笑検知ランキング",
        description=(
            f"サーバー「{ctx.guild.name}」\n"
            f"検知対象者数: {len(ranked)}人 / 合計検知数: {total_all}回\n\n"
            + "\n".join(lines)
        ),
        color=discord.Color.orange(),
    )

    if len(ranked) > MAX_RANKING_DISPLAY:
        embed.set_footer(text=f"上位{MAX_RANKING_DISPLAY}人のみ表示しています（全{len(ranked)}人中）")

    await ctx.reply(embed=embed)


@bot.command(name="reisho_help")
async def help_cmd(ctx: commands.Context):
    text = (
        "**冷笑検知bot ヘルプ**\n"
        f"- `{COMMAND_PREFIX}ranking` : このサーバーの冷笑回数ランキングを詳細表示（全員分）\n"
        f"- 冷笑っぽい発言を検知すると `:{EMOJI_NAME}:` でリアクション＆リプライで通知します\n"
        "- 「う」→「お」（または「お」→「う」）を1文字ずつ連投すると検知します\n"
        "- メッセージに 🇺 → 🇴 の順でリアクションすると、リアクションした人が検知されます\n"
        "- 検知ワードは keywords.json で調整できます"
    )
    await ctx.reply(text)


# ------------------------------------------------------------
# Koyeb用ヘルスチェックサーバー (Webサービスとしてデプロイする場合に必要)
# ------------------------------------------------------------
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass  # アクセスログを抑制


def run_health_server():
    server = HTTPServer(("0.0.0.0", PORT), _HealthHandler)
    log.info("ヘルスチェックサーバーを起動しました: port=%d", PORT)
    server.serve_forever()


def main():
    health_thread = Thread(target=run_health_server, daemon=True)
    health_thread.start()
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
