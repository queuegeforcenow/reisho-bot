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
from discord import app_commands

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

REGIONAL_U = "\U0001F1FA"  # 🇺
REGIONAL_O = "\U0001F1F4"  # 🇴

STATE_PATH = os.environ.get("STATE_PATH", "bot_state.json")
LOCKED_NICK = os.environ.get("REISHO_LOCKED_NICK", "???")
UNLOCKED_NICK = os.environ.get("REISHO_UNLOCKED_NICK", "冷笑検知bot")
ICON_PATH = os.environ.get("REISHO_ICON_PATH", "assets/reisho_icon.jpg")

if not DISCORD_TOKEN:
    raise RuntimeError("環境変数 DISCORD_TOKEN が設定されていません。")

# ------------------------------------------------------------
# キーワード設定の読み込みと保存
# ------------------------------------------------------------
def load_keyword_config(path: str):
    if not os.path.exists(path):
        default_data = {"threshold": 3, "patterns": []}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(default_data, f, ensure_ascii=False, indent=2)
        return default_data["threshold"], [], default_data

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    threshold = data.get("threshold", 3)
    compiled = []
    for item in data.get("patterns", []):
        try:
            regex = re.compile(item["pattern"], re.IGNORECASE)
            compiled.append((regex, item.get("weight", 1)))
        except re.error as e:
            log.warning("パターンのコンパイルに失敗しました: %s (%s)", item.get("pattern"), e)
    return threshold, compiled, data

def save_keyword_config(path: str, data: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

THRESHOLD, PATTERNS, RAW_CONFIG = load_keyword_config(KEYWORDS_PATH)
log.info("キーワード設定を読み込みました: %d件のパターン, しきい値=%d", len(PATTERNS), THRESHOLD)

def calc_reisho_score(text: str) -> tuple[int, int]:
    """メッセージ本文から冷笑スコアとマッチ回数を計算する"""
    score = 0
    match_count = 0
    
    # 回避対策: 空白や改行、ゼロ幅文字をすべて除去して判定を逃れられないようにする
    normalized = re.sub(r'[\s\u200B-\u200D\uFEFF]+', '', text)
    
    for regex, weight in PATTERNS:
        # 正規化前と正規化後の両方でチェックし、多くマッチした方を採用
        matches_normal = list(regex.finditer(text))
        matches_stripped = list(regex.finditer(normalized))
        matches = matches_normal if len(matches_normal) > len(matches_stripped) else matches_stripped
        
        if matches:
            count = len(matches)
            score += weight * count
            match_count += count
            
    return score, match_count

# ------------------------------------------------------------
# カウント永続化 (JSONファイル)
# ------------------------------------------------------------
_counts_lock = asyncio.Lock()

def _load_counts() -> dict:
    if not os.path.exists(COUNTS_PATH):
        return {}
    try:
        with open(COUNTS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}

def _save_counts(counts: dict) -> None:
    tmp_path = COUNTS_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(counts, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, COUNTS_PATH)

async def increment_count(guild_id: int, user_id: int, amount: int = 1) -> int:
    async with _counts_lock:
        counts = _load_counts()
        g = counts.setdefault(str(guild_id), {})
        g[str(user_id)] = g.get(str(user_id), 0) + amount
        _save_counts(counts)
        return g[str(user_id)]

async def get_ranking(guild_id: int, limit: int | None = None):
    async with _counts_lock:
        counts = _load_counts()
        g = counts.get(str(guild_id), {})
        ranked = sorted(g.items(), key=lambda kv: kv[1], reverse=True)
        return ranked[:limit] if limit else ranked

# ------------------------------------------------------------
# Discord Bot 本体
# ------------------------------------------------------------
class ReishoBot(commands.Bot):
    async def setup_hook(self):
        # スラッシュコマンドをサーバーに同期
        await self.tree.sync()
        log.info("スラッシュコマンドを同期しました。")

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = ReishoBot(command_prefix=COMMAND_PREFIX, intents=intents, help_command=None)

# 連投コンボ状態 (channel_id, author_id) -> [{"content": str, "timestamp": datetime}, ...]
_single_char_state: dict[tuple[int, int], list] = {}
_reaction_sequence_state: dict[tuple[int, int], list] = {}

BOT_READY_AT = None

# ... (unlock等の状態永続化・_set_nickの処理は既存のままなので省略せず残します) ...
_state_lock = asyncio.Lock()
def _load_state() -> dict:
    if not os.path.exists(STATE_PATH): return {"unlocked": False}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f: return json.load(f)
    except: return {"unlocked": False}

def _save_state(state: dict) -> None:
    tmp_path = STATE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f: json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, STATE_PATH)

async def is_unlocked() -> bool:
    async with _state_lock: return _load_state().get("unlocked", False)

async def mark_unlocked() -> bool:
    async with _state_lock:
        state = _load_state()
        if state.get("unlocked", False): return False
        state["unlocked"] = True
        _save_state(state)
        return True

async def _set_nick(guild: discord.Guild, nick: str) -> None:
    if guild.me is None or guild.me.nick == nick: return
    try: await guild.me.edit(nick=nick)
    except discord.Forbidden: pass
    except discord.HTTPException: pass

async def apply_appearance_to_guild(guild: discord.Guild) -> None:
    nick = UNLOCKED_NICK if await is_unlocked() else LOCKED_NICK
    await _set_nick(guild, nick)

async def unlock_bot_globally() -> None:
    if not await mark_unlocked(): return
    log.info("🎉 初めての冷笑を検知しました。ニックネームとアイコンを解禁します。")
    for guild in bot.guilds:
        await _set_nick(guild, UNLOCKED_NICK)
    if os.path.exists(ICON_PATH):
        try:
            with open(ICON_PATH, "rb") as f: avatar_bytes = f.read()
            await bot.user.edit(avatar=avatar_bytes)
        except: pass

@bot.event
async def on_guild_join(guild: discord.Guild):
    await apply_appearance_to_guild(guild)
    if update_presence.is_running(): await update_presence()

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None: return
    if BOT_READY_AT is not None and message.created_at < BOT_READY_AT: return

    content = message.content
    # 回避対策：空白などを除去したもので単文字コンボをチェック
    stripped_for_combo = re.sub(r'[\s\u200B-\u200D\uFEFF]+', '', content)

# --- 「う」「お」単文字連投コンボ検知 ---
    # Discordの絵文字コード直打ち (:regional_indicator_u: など) を実際の絵文字に変換
    check_text = content.replace(":regional_indicator_u:", "🇺").replace(":regional_indicator_o:", "🇴")
    stripped_for_combo = normalize_text_for_reisho(check_text)

    # 「う」系、「お」系のみで構成されているか（w、草、😅 が付いていてもアウトにする）
    U_PATTERN = re.compile(r'^[うぅuU🇺]+[wWｗＷ草😅]*$')
    O_PATTERN = re.compile(r'^[おぉoO🇴]+[wWｗＷ草😅]*$')

    is_u = bool(U_PATTERN.match(stripped_for_combo))
    is_o = bool(O_PATTERN.match(stripped_for_combo))

    if is_u or is_o:
        key = (message.channel.id, message.author.id)
        history = _single_char_state.setdefault(key, [])
        now = discord.utils.utcnow()
        
        # 制限時間内の履歴だけ残す
        history[:] = [h for h in history if (now - h["timestamp"]).total_seconds() <= TEXT_COMBO_WINDOW_SECONDS]
        
        # UかOかを記録
        char_type = "U" if is_u else "O"
        history.append({"type": char_type, "timestamp": now})
        
        has_u = any(h["type"] == "U" for h in history)
        has_o = any(h["type"] == "O" for h in history)
        
        if has_u and has_o:
            combo_text = "う→お（絵文字・w付き・英語などの連投）"
            await credit_reisho(
                guild=message.guild,
                reaction_target=message,
                reply_target=message,
                credited_user=message.author,
                content_preview=combo_text,
                times=1
            )
            _single_char_state.pop(key, None)
        await bot.process_commands(message)
        return

    score, match_count = calc_reisho_score(content)

    if score >= THRESHOLD:
        content_preview = content[:300] + "…" if len(content) > 300 else content
        # 連続マッチしていたら、マッチした回数分だけ冷笑回数を倍増する
        times = max(1, match_count)
        
        await credit_reisho(
            guild=message.guild,
            reaction_target=message,
            reply_target=message,
            credited_user=message.author,
            content_preview=content_preview,
            times=times
        )

    await bot.process_commands(message)


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    # (既存のリアクション検知処理そのまま)
    if payload.guild_id is None: return
    if payload.member is not None and payload.member.bot: return
    if bot.user is not None and payload.user_id == bot.user.id: return
    emoji_str = payload.emoji.name
    if emoji_str not in (REGIONAL_U, REGIONAL_O): return

    now = discord.utils.utcnow()
    key = (payload.message_id, payload.user_id)
    seq = _reaction_sequence_state.setdefault(key, [])
    seq.append((emoji_str, now))
    seq[:] = [(e, t) for (e, t) in seq if (now - t).total_seconds() <= REACTION_COMBO_WINDOW_SECONDS]

    if len(seq) >= 2 and seq[-2][0] == REGIONAL_U and seq[-1][0] == REGIONAL_O:
        guild = bot.get_guild(payload.guild_id)
        if not guild: return
        channel = guild.get_channel(payload.channel_id) or bot.get_channel(payload.channel_id)
        if not channel: return
        try: target_message = await channel.fetch_message(payload.message_id)
        except discord.HTTPException: return
        reactor = payload.member or guild.get_member(payload.user_id)
        if not reactor: return

        await credit_reisho(
            guild=guild,
            reaction_target=target_message,
            reply_target=target_message,
            credited_user=reactor,
            content_preview="🇺🇴 リアクション（連続）",
            times=1
        )
        _reaction_sequence_state.pop(key, None)


async def credit_reisho(
    guild: discord.Guild,
    reaction_target: discord.Message,
    reply_target: discord.Message,
    credited_user: discord.abc.User,
    content_preview: str,
    times: int = 1,
):
    emoji = discord.utils.get(guild.emojis, name=EMOJI_NAME)
    try:
        await reaction_target.add_reaction(emoji if emoji else "😏")
    except discord.HTTPException:
        pass

    # 検知回数を一気に加算
    total = await increment_count(guild.id, credited_user.id, times)

    combo_text = f"（連続検知！ {times}回分加算）" if times > 1 else ""
    reply_text = (
        f"{credited_user.mention} 冷笑を検知しました！{combo_text}\n"
        f"内容：{content_preview}\n"
        f"(このサーバーでの累計冷笑回数: {total}回)"
    )

    try: await reply_target.reply(reply_text, mention_author=True)
    except discord.HTTPException: pass

    await unlock_bot_globally()


# ------------------------------------------------------------
# スラッシュコマンド (キーワード追加・削除・データバックアップ)
# ------------------------------------------------------------
@bot.tree.command(name="add_word", description="冷笑検知キーワードを追加します")
@app_commands.describe(pattern="正規表現または単語", weight="重み(デフォルト3)", comment="説明(任意)")
@app_commands.default_permissions(administrator=True)
async def add_word(interaction: discord.Interaction, pattern: str, weight: int = 3, comment: str = ""):
    global THRESHOLD, PATTERNS, RAW_CONFIG
    try:
        re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        await interaction.response.send_message(f"正規表現が不正です: {e}", ephemeral=True)
        return
        
    RAW_CONFIG["patterns"].append({
        "pattern": pattern,
        "weight": weight,
        "comment": comment
    })
    save_keyword_config(KEYWORDS_PATH, RAW_CONFIG)
    THRESHOLD, PATTERNS, RAW_CONFIG = load_keyword_config(KEYWORDS_PATH)
    await interaction.response.send_message(f"キーワード `{pattern}` を追加しました！\n(重み: {weight})", ephemeral=True)

@bot.tree.command(name="list_words", description="冷笑検知キーワードの一覧を表示します")
@app_commands.default_permissions(administrator=True)
async def list_words(interaction: discord.Interaction):
    if not RAW_CONFIG.get("patterns"):
        await interaction.response.send_message("キーワードが登録されていません。", ephemeral=True)
        return
        
    lines = []
    for i, p in enumerate(RAW_CONFIG["patterns"]):
        lines.append(f"【{i}】 {p['pattern']} (重み: {p['weight']}) - {p.get('comment', '')}")
    
    text = "\n".join(lines)
    if len(text) > 1900: text = text[:1900] + "...\n(省略されました)"
    await interaction.response.send_message(f"```\n{text}\n```\n※削除したい場合は `/remove_word index:番号` を使ってください。", ephemeral=True)

@bot.tree.command(name="remove_word", description="冷笑検知キーワードを削除します")
@app_commands.describe(index="削除するキーワードのインデックス(/list_wordsで確認)")
@app_commands.default_permissions(administrator=True)
async def remove_word(interaction: discord.Interaction, index: int):
    global THRESHOLD, PATTERNS, RAW_CONFIG
    if index < 0 or index >= len(RAW_CONFIG.get("patterns", [])):
        await interaction.response.send_message("指定された番号のキーワードは見つかりません。", ephemeral=True)
        return
        
    removed = RAW_CONFIG["patterns"].pop(index)
    save_keyword_config(KEYWORDS_PATH, RAW_CONFIG)
    THRESHOLD, PATTERNS, RAW_CONFIG = load_keyword_config(KEYWORDS_PATH)
    await interaction.response.send_message(f"キーワード `{removed['pattern']}` を削除しました。", ephemeral=True)

@bot.tree.command(name="backup_data", description="ランキングのJSONファイルをバックアップとして取得します")
@app_commands.default_permissions(administrator=True)
async def backup_data(interaction: discord.Interaction):
    if not os.path.exists(COUNTS_PATH):
        await interaction.response.send_message("データファイルがまだ作成されていません。", ephemeral=True)
        return
    file = discord.File(COUNTS_PATH, filename="reisho_counts_backup.json")
    await interaction.response.send_message("現在のランキングデータです。\nサーバー再起動でデータが飛んだ際は、これを手元に保存しておいてください。", file=file, ephemeral=True)

# ------------------------------------------------------------
# 既存のコマンド類
# ------------------------------------------------------------
MAX_RANKING_DISPLAY = 25

@tasks.loop(seconds=30)
async def update_presence():
    total_members = sum(g.member_count or 0 for g in bot.guilds)
    await bot.change_presence(status=discord.Status.online, activity=discord.Game(name=f"{len(bot.guilds)}サーバー｜{total_members}名を監視中"))

@update_presence.before_loop
async def before_update_presence():
    await bot.wait_until_ready()

@bot.event
async def on_ready():
    global BOT_READY_AT
    BOT_READY_AT = discord.utils.utcnow()
    log.info("ログイン完了: %s", bot.user)
    for guild in bot.guilds: await apply_appearance_to_guild(guild)
    if not update_presence.is_running(): update_presence.start()

@bot.command(name="shoki")
@commands.has_permissions(administrator=True)
async def shoki_cmd(ctx: commands.Context):
    async with _state_lock:
        state = _load_state()
        state["unlocked"] = False
        _save_state(state)
    for guild in bot.guilds: await _set_nick(guild, LOCKED_NICK)
    try: await bot.user.edit(avatar=None)
    except: pass
    await ctx.reply("解禁状態、ニックネーム、アイコンを初期状態に戻しました。")

@bot.command(name="say")
@commands.has_permissions(administrator=True)
async def say_text_cmd(ctx: commands.Context, *, text: str):
    # コマンドを打った本人のメッセージを消す（権限がある場合）
    try:
        await ctx.message.delete()
    except discord.HTTPException:
        pass
    
    # Botとしてメッセージを送信
    await ctx.send(text)

@bot.command(name="ranking")
async def ranking_cmd(ctx: commands.Context):
    ranked = await get_ranking(ctx.guild.id)
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
        description=(f"サーバー「{ctx.guild.name}」\n検知対象者数: {len(ranked)}人 / 合計検知数: {total_all}回\n\n" + "\n".join(lines)),
        color=discord.Color.orange(),
    )
    if len(ranked) > MAX_RANKING_DISPLAY:
        embed.set_footer(text=f"上位{MAX_RANKING_DISPLAY}人のみ表示しています（全{len(ranked)}人中）")
    await ctx.reply(embed=embed)

# ------------------------------------------------------------
# Discordチャンネルを使った完全自動バックアップ＆復元システム
# ------------------------------------------------------------
BACKUP_CHANNEL_ID = int(os.environ.get("BACKUP_CHANNEL_ID", "0"))
_last_backup_message_id = None

async def auto_backup_to_discord():
    """JSONファイルを指定チャンネルに自動送信する"""
    global _last_backup_message_id
    if BACKUP_CHANNEL_ID == 0:
        return
        
    channel = bot.get_channel(BACKUP_CHANNEL_ID)
    if not channel:
        return

    try:
        # 古いバックアップメッセージがあれば削除（チャンネルが埋まらないようにする）
        if _last_backup_message_id:
            try:
                old_msg = await channel.fetch_message(_last_backup_message_id)
                await old_msg.delete()
            except discord.HTTPException:
                pass

        # 最新のJSONファイルを送信
        if os.path.exists(COUNTS_PATH):
            file = discord.File(COUNTS_PATH, filename="reisho_counts.json")
            msg = await channel.send("🔄 【自動バックアップ】ランキングデータ", file=file)
            _last_backup_message_id = msg.id
    except Exception as e:
        log.error(f"自動バックアップに失敗しました: {e}")

async def auto_restore_from_discord():
    """起動時にDiscordチャンネルから最新のJSONをダウンロードして復元する"""
    if BACKUP_CHANNEL_ID == 0:
        return
        
    channel = bot.get_channel(BACKUP_CHANNEL_ID)
    if not channel:
        return
        
    try:
        # チャンネルの最新メッセージを最大10件遡ってファイルを探す
        async for msg in channel.history(limit=10):
            if msg.author == bot.user and msg.attachments:
                att = msg.attachments[0]
                if att.filename.endswith('.json'):
                    file_bytes = await att.read()
                    data = json.loads(file_bytes.decode('utf-8'))
                    
                    # データをローカルに保存
                    with open(COUNTS_PATH, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                        
                    log.info("✅ 起動時の自動データ復元が完了しました！")
                    return
    except Exception as e:
        log.error(f"自動復元に失敗しました: {e}")

@bot.command(name="reisho_help")
async def help_cmd(ctx: commands.Context):
    text = (
        "**冷笑検知bot ヘルプ**\n"
        f"- `{COMMAND_PREFIX}ranking` : ランキングを詳細表示\n"
        f"- `/add_word`, `/remove_word`, `/list_words` : 禁止ワードの管理 (スラッシュコマンド・管理者のみ)\n"
        f"- `/backup_data` : ランキングデータをJSONとしてダウンロード (管理者のみ)\n"
        "- 間にスペースなどを入れても無効化されます。また、複数回連呼した場合はペナルティが倍増します。"
    )
    await ctx.reply(text)

# --- ヘルスチェックサーバー ---
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args): pass

def run_health_server():
    server = HTTPServer(("0.0.0.0", PORT), _HealthHandler)
    server.serve_forever()

def main():
    Thread(target=run_health_server, daemon=True).start()
    bot.run(DISCORD_TOKEN)

if __name__ == "__main__":
    main()
