import os
import re
import io
import json
import asyncio
import logging
import datetime
import unicodedata
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from gtts import gTTS
from pydub import AudioSegment
from pydub.generators import Sine
import faster_whisper
from discord.ext import voice_recv

import discord
from discord import app_commands
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
COUNTS_PATH = os.environ.get("COUNTS_PATH", "reisho_counts.json")  # ログチャンネル未設定時のフォールバック先
PORT = int(os.environ.get("PORT", 8000))
# 指定すると、このサーバーIDにだけスラッシュコマンドを即時反映する(テスト用)。
# 未指定の場合はグローバル同期(全サーバー反映まで最大1時間程度かかることがある)。
DEV_GUILD_ID = os.environ.get("DEV_GUILD_ID")
# 指定すると、ランキングデータ(JSON)をこのチャンネルにファイル添付で保存・読み込みする。
# 未指定の場合は従来通りローカルファイル(COUNTS_PATH)に保存する。
RANKING_LOG_CHANNEL_ID = os.environ.get("RANKING_LOG_CHANNEL_ID")
RANKING_LOG_FILENAME = "reisho_counts.json"

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
# 回避防止用のテキスト正規化
# ------------------------------------------------------------
def normalize_for_match(text: str) -> str:
    """
    キーワード判定用にテキストを正規化する。
    ・NFKC正規化で全角/半角(ｗ→w、半角カナ→全角カナ等)を統一
    ・空白を全て除去(文字の間にスペースを挟む回避策を無効化)
    ・ゼロ幅文字を除去
    """
    normalized = unicodedata.normalize("NFKC", text)
    normalized = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", normalized)  # ゼロ幅文字
    normalized = re.sub(r"\s+", "", normalized)  # 空白除去
    return normalized


def normalize_single_char(text: str):
    """
    「う」「お」単文字コンボ判定用。
    以下をすべて同一視して判定する(回避防止の対象拡大):
    ・ひらがな/カタカナの大小("う/ぅ/ウ/ゥ"、"お/ぉ/オ/ォ")
    ・半角/全角のローマ字 u, o (大文字小文字問わず)
    ・絵文字の🇺・🇴を単体で送った場合
    記号・絵文字装飾・空白・伸ばし棒は除去して判定する。
    """
    raw = text.strip()
    # 絵文字🇺・🇴を単体で送った場合もそれぞれ「う」「お」として扱う
    if raw == REGIONAL_U:
        return "う"
    if raw == REGIONAL_O:
        return "お"

    normalized = unicodedata.normalize("NFKC", text)
    core = re.sub(r"[^\w]", "", normalized)  # 記号・絵文字・空白を除去
    core = core.replace("ー", "")  # 装飾的な伸ばし棒を除去(回避防止)

    if core in ("う", "ウ", "ぅ", "ゥ"):
        return "う"
    if core in ("お", "オ", "ぉ", "ォ"):
        return "お"
    if core.lower() == "u":
        return "う"
    if core.lower() == "o":
        return "お"
    return None


def build_flexible_pattern(word: str) -> str:
    """
    ユーザーが/reisho_add_wordで入力した単語から、
    w/ｗの連続を柔軟に許容する正規表現文字列を作る。
    (例: "きちー" -> "きちー"、"きちーw" -> "きちー[wWｗＷ]{1,}")
    """
    out = []
    i = 0
    while i < len(word):
        ch = word[i]
        if ch in "wWｗＷ":
            j = i
            while j < len(word) and word[j] in "wWｗＷ":
                j += 1
            count = j - i
            out.append(f"[wWｗＷ]{{{count},}}")
            i = j
        else:
            out.append(re.escape(ch))
            i += 1
    return "".join(out)


# ------------------------------------------------------------
# キーワード設定の読み込み・保存
# ------------------------------------------------------------
def load_keyword_data(path: str) -> dict:
    if not os.path.exists(path):
        return {"threshold": 3, "patterns": []}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_keyword_data() -> None:
    tmp_path = KEYWORDS_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(KEYWORD_DATA, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, KEYWORDS_PATH)


KEYWORD_DATA = load_keyword_data(KEYWORDS_PATH)
THRESHOLD = KEYWORD_DATA.get("threshold", 3)
COMPILED_PATTERNS: list[dict] = []


def rebuild_compiled_patterns() -> None:
    """KEYWORD_DATAから正規表現を再コンパイルしてCOMPILED_PATTERNSを更新する"""
    global COMPILED_PATTERNS
    compiled = []
    for item in KEYWORD_DATA.get("patterns", []):
        try:
            regex = re.compile(item["pattern"], re.IGNORECASE)
        except re.error as e:
            log.warning("パターンのコンパイルに失敗しました: %s (%s)", item.get("pattern"), e)
            continue
        compiled.append({
            "regex": regex,
            "weight": item.get("weight", 1),
            "comment": item.get("comment", ""),
            "pattern": item["pattern"],
            "custom": item.get("custom", False),
        })
    COMPILED_PATTERNS = compiled
    log.info("キーワード設定を読み込みました: %d件のパターン, しきい値=%d", len(COMPILED_PATTERNS), THRESHOLD)


rebuild_compiled_patterns()


def calc_reisho_score(text: str):
    """
    メッセージ本文から冷笑スコアを計算する。
    同じパターンが1メッセージ内で複数回出現した場合は、その分スコアと
    ヒット回数(match_count)を積み増す(=連続ヒットほどカウントが伸びる)。
    """
    normalized = normalize_for_match(text)
    score = 0
    match_count = 0
    for item in COMPILED_PATTERNS:
        matches = list(item["regex"].finditer(normalized))
        if matches:
            score += item["weight"] * len(matches)
            match_count += len(matches)
    return score, match_count

# ------------------------------------------------------------
# VC通知用音声の生成処理 (ピンポン音 + TTS)
# ------------------------------------------------------------
def create_notification_audio(username: str) -> str:
    """ピンポン音とTTSを合成してWAVファイルを出力する"""
    # 1. ピンポン音（ド・ミのサイン波で簡易作成）
    tone1 = Sine(523.25).to_audio_segment(duration=300).apply_gain(-10)  # C5
    tone2 = Sine(659.25).to_audio_segment(duration=500).apply_gain(-10)  # E5
    ping_pong = tone1 + tone2
    
    # 2. TTS生成
    tts_text = f"{username}さんが冷笑をしました。"
    tts = gTTS(text=tts_text, lang='ja')
    tts_file = f"tts_{username}.mp3"
    tts.save(tts_file)
    
    # 3. 結合処理
    tts_audio = AudioSegment.from_mp3(tts_file)
    combined = ping_pong + AudioSegment.silent(duration=200) + tts_audio
    
    output_file = f"notify_{username}.wav"
    combined.export(output_file, format="wav")
    
    # 一時ファイルの削除
    if os.path.exists(tts_file):
        os.remove(tts_file)
    return output_file


# ------------------------------------------------------------
# カウント永続化
# RANKING_LOG_CHANNEL_ID が設定されていれば、指定のDiscordチャンネルに
# JSONファイルを添付したメッセージとして保存・読み込みする(Koyebの再デプロイで
# ディスクが消えても失われない)。未設定の場合は従来通りローカルファイルに保存する。
# ------------------------------------------------------------
_counts_lock = asyncio.Lock()
_counts_cache: dict = {}
_counts_loaded = False
_counts_message_id: int | None = None
_counts_channel: discord.abc.Messageable | None = None


def _load_counts_local() -> dict:
    if not os.path.exists(COUNTS_PATH):
        return {}
    try:
        with open(COUNTS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        log.warning("カウントファイルの読み込みに失敗したため初期化します。")
        return {}


def _save_counts_local(counts: dict) -> None:
    tmp_path = COUNTS_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(counts, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, COUNTS_PATH)


async def _resolve_counts_channel():
    """ランキング保存用チャンネルのオブジェクトを取得(一度取得したらキャッシュする)"""
    global _counts_channel
    if not RANKING_LOG_CHANNEL_ID:
        return None
    if _counts_channel is not None:
        return _counts_channel
    channel = bot.get_channel(int(RANKING_LOG_CHANNEL_ID))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(RANKING_LOG_CHANNEL_ID))
        except discord.HTTPException as e:
            log.warning("ランキング保存用チャンネル(%s)の取得に失敗しました: %s", RANKING_LOG_CHANNEL_ID, e)
            return None
    _counts_channel = channel
    return channel


async def _ensure_counts_loaded() -> None:
    """起動時に1回だけランキングデータを読み込む(ログチャンネル優先)"""
    global _counts_cache, _counts_message_id, _counts_loaded
    if _counts_loaded:
        return

    if not RANKING_LOG_CHANNEL_ID:
        _counts_cache = _load_counts_local()
        _counts_loaded = True
        log.info("ランキングデータをローカルファイルから読み込みました(%s)。", COUNTS_PATH)
        return

    channel = await _resolve_counts_channel()
    if channel is None:
        # チャンネル取得に失敗した場合はローカルファイルにフォールバック
        _counts_cache = _load_counts_local()
        _counts_loaded = True
        return

    try:
        async for msg in channel.history(limit=50):
            if bot.user is None or msg.author.id != bot.user.id:
                continue
            for attachment in msg.attachments:
                if attachment.filename == RANKING_LOG_FILENAME:
                    try:
                        data = await attachment.read()
                        _counts_cache = json.loads(data.decode("utf-8"))
                    except (json.JSONDecodeError, discord.HTTPException) as e:
                        log.warning("ランキングデータの読み込みに失敗しました: %s", e)
                        _counts_cache = {}
                    _counts_message_id = msg.id
                    _counts_loaded = True
                    log.info("ランキングデータをログチャンネルから読み込みました(message_id=%s)。", msg.id)
                    return
    except discord.HTTPException as e:
        log.warning("ランキングログチャンネルの履歴取得に失敗しました: %s", e)

    log.info("ログチャンネルに既存のランキングデータが見つからなかったため、新規に開始します。")
    _counts_cache = {}
    _counts_loaded = True


async def _persist_counts() -> None:
    """現在のランキングデータを保存する(ログチャンネル優先、未設定/失敗時はローカルファイル)"""
    global _counts_message_id

    if RANKING_LOG_CHANNEL_ID:
        channel = await _resolve_counts_channel()
        if channel is not None:
            payload = json.dumps(_counts_cache, ensure_ascii=False, indent=2).encode("utf-8")

            async def _send_new_message():
                global _counts_message_id
                f = discord.File(io.BytesIO(payload), filename=RANKING_LOG_FILENAME)
                new_msg = await channel.send(
                    content="📊 冷笑ランキングデータ(bot自動保存・このメッセージは編集/削除しないでください)",
                    file=f,
                )
                _counts_message_id = new_msg.id

            try:
                if _counts_message_id is not None:
                    try:
                        msg = await channel.fetch_message(_counts_message_id)
                        f = discord.File(io.BytesIO(payload), filename=RANKING_LOG_FILENAME)
                        await msg.edit(attachments=[f])
                    except discord.NotFound:
                        await _send_new_message()
                else:
                    await _send_new_message()
                return
            except discord.HTTPException as e:
                log.warning("ランキングログチャンネルへの保存に失敗しました。ローカルファイルに保存します: %s", e)

    # ログチャンネル未設定 or 保存失敗時はローカルファイルにフォールバック
    _save_counts_local(_counts_cache)


async def increment_count(guild_id: int, user_id: int, amount: int = 1) -> int:
    """回数を加算して即座に保存する(毎回保存)"""
    async with _counts_lock:
        await _ensure_counts_loaded()
        g = _counts_cache.setdefault(str(guild_id), {})
        g[str(user_id)] = g.get(str(user_id), 0) + amount
        await _persist_counts()
        return g[str(user_id)]


async def get_ranking(guild_id: int, limit: int | None = None):
    async with _counts_lock:
        await _ensure_counts_loaded()
        g = _counts_cache.get(str(guild_id), {})
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

intents.voice_states = True  # VCの状態取得を有効化

# VC音声認識用Whisperモデル (CPU環境向けに軽量なtinyモデルを使用)
whisper_model = faster_whisper.WhisperModel("tiny", device="cpu", compute_type="int8")

bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents, help_command=None)

# (channel_id, author_id) -> {"content": "う"|"お", "timestamp": datetime}
# 「う」→「お」(または「お」→「う」) の単文字連投コンボ検知用の状態
_single_char_state: dict[tuple[int, int], dict] = {}

# (message_id, user_id) -> [(emoji_str, timestamp), ...]
# 🇺 → 🇴 リアクション連続コンボ検知用の状態
_reaction_sequence_state: dict[tuple[int, int], list] = {}

# botが接続(オンライン化)した時刻。これより前に投稿されたメッセージは検知対象外にする。
BOT_READY_AT = None  # type: datetime.datetime | None

# スラッシュコマンドを既に同期したか(起動の度に何度も同期しないためのフラグ)
_commands_synced = False


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

    for guild in bot.guilds:
        await _set_nick(guild, UNLOCKED_NICK)

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
            "アイコン画像が見つかりません: %s 。ニックネームのみ変更しました。",
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

    # --- 「う」「お」単文字連投コンボ検知(記号・絵文字装飾での回避を防止) ---
    normalized_char = normalize_single_char(content)
    if normalized_char is not None:
        key = (message.channel.id, message.author.id)
        prev = _single_char_state.get(key)
        now = discord.utils.utcnow()
        if (
            prev is not None
            and prev["content"] != normalized_char
            and (now - prev["timestamp"]).total_seconds() <= TEXT_COMBO_WINDOW_SECONDS
        ):
            combo_text = f"{prev['content']}→{normalized_char}（連続投稿）"
            await credit_reisho(
                guild=message.guild,
                reaction_target=message,
                reply_target=message,
                credited_user=message.author,
                content_preview=combo_text,
                amount=1,
            )
            _single_char_state.pop(key, None)
        else:
            _single_char_state[key] = {"content": normalized_char, "timestamp": now}
        # 単文字コンボはこれで完結させ、通常のスコア判定はスキップする
        await bot.process_commands(message)
        return

    score, match_count = calc_reisho_score(content)

    if score >= THRESHOLD:
        content_preview = content
        if len(content_preview) > 300:
            content_preview = content_preview[:300] + "…"
        # 同じワードが複数回出現していれば、その分カウントを多く加算する
        amount = max(match_count, 1)
        await credit_reisho(
            guild=message.guild,
            reaction_target=message,
            reply_target=message,
            credited_user=message.author,
            content_preview=content_preview,
            amount=amount,
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
            amount=1,
        )
        _reaction_sequence_state.pop(key, None)


async def credit_reisho(
    guild: discord.Guild,
    reaction_target: discord.Message,
    reply_target: discord.Message,
    credited_user: discord.abc.User,
    content_preview: str,
    amount: int = 1,
):
    """冷笑を検知したユーザーにリアクション・通知・カウントを行う共通処理"""

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

    total = await increment_count(guild.id, credited_user.id, amount=amount)

    extra_note = f"（今回 +{amount}）" if amount > 1 else ""
    reply_text = (
        f"{credited_user.mention} 冷笑を検知しました！　"
        f"内容：{content_preview}{extra_note}\n"
        f"(このサーバーでの累計冷笑回数: {total}回)"
    )

    try:
        await reply_target.reply(reply_text, mention_author=True)
    except discord.HTTPException as e:
        log.warning("リプライ送信に失敗しました: %s", e)

    # bot全体で初めての検知なら、ニックネーム/アイコンを解禁する
    await unlock_bot_globally()

# bot全体で初めての検知なら、ニックネーム/アイコンを解禁する
    await unlock_bot_globally()

# ============================================================
# ↓↓↓ ここから④の追加コードを貼り付ける ↓↓↓
# ============================================================

# ------------------------------------------------------------
# VC音声受信・冷笑検知クラス
# ------------------------------------------------------------
class ReishoAudioSink(voice_recv.AudioSink):
    def __init__(self, vc, text_channel, guild: discord.Guild):
        super().__init__()
        self.vc = vc
        self.text_channel = text_channel
        self.guild = guild
        self.user_buffers = {}
    
    def wants_opus(self):
        return False  # PCM(解凍済み音声)で受信

    def cleanup(self):
        # 終了時のクリーンアップ処理（必須）
        self.user_buffers.clear()

    def write(self, user, data):
        if not user or user.bot:
            return
            
        if user.id not in self.user_buffers:
            self.user_buffers[user.id] = bytearray()
            
        self.user_buffers[user.id].extend(data.pcm)
        
        # 約3秒分（48kHz, 2ch, 16bit = 3840bytes * 50fps * 3秒）溜まったら文字起こしへ回す
        if len(self.user_buffers[user.id]) > 3840 * 50 * 3:
            pcm_data = self.user_buffers.pop(user.id)
            asyncio.run_coroutine_threadsafe(
                self.process_audio(user, pcm_data),
                bot.loop
            )

    async def process_audio(self, user, pcm_data):
        # 16kHz モノラル WAVに変換（Whisper最適化）
        audio_segment = AudioSegment(
            data=bytes(pcm_data),
            sample_width=2,
            frame_rate=48000,
            channels=2
        )
        audio_segment = audio_segment.set_frame_rate(16000).set_channels(1)
        wav_io = io.BytesIO()
        audio_segment.export(wav_io, format="wav")
        wav_io.seek(0)

        # 文字起こし実行 (重いため非同期スレッドで処理)
        segments, _ = await bot.loop.run_in_executor(
            None, 
            lambda: whisper_model.transcribe(wav_io, language="ja")
        )
        text = "".join([s.text for s in segments]).strip()

        if text:
            score, match_count = calc_reisho_score(text)
            if score >= THRESHOLD:
                amount = max(match_count, 1)
                
                # 既存のカウント保存システムへ統合
                total = await increment_count(self.guild.id, user.id, amount=amount)
                
                # チャート通知メッセージ
                extra_note = f"（今回 +{amount}）" if amount > 1 else ""
                await self.text_channel.send(
                    f"🎙️ **VC冷笑検知**\n"
                    f"{user.mention} 冷笑を検知しました！ 内容：「{text}」{extra_note}\n"
                    f"(このサーバーでの累計冷笑回数: {total}回)"
                )
                
                # アイコン/ニックネーム解禁チェック
                await unlock_bot_globally()

                # VCでピンポン音＋TTS音声を再生
                audio_file = create_notification_audio(user.display_name)
                if not self.vc.is_playing():
                    source = discord.FFmpegPCMAudio(audio_file)
                    
                    def after_play(error):
                        if os.path.exists(audio_file):
                            os.remove(audio_file)

                    self.vc.play(source, after=after_play)

# ============================================================
# ↑↑↑ ここまでが④の追加コード ↑↑↑
# ============================================================


# ------------------------------------------------------------
# スラッシュコマンド: 冷笑ワードの追加/削除/一覧
# (標準搭載ワードは保護され、追加したワードのみ削除可能)
# ------------------------------------------------------------

# ------------------------------------------------------------
# スラッシュコマンド: 冷笑ワードの追加/削除/一覧
# (標準搭載ワードは保護され、追加したワードのみ削除可能)
# ------------------------------------------------------------
@bot.tree.command(name="reisho_add_word", description="冷笑検知ワードを追加します(管理者限定)")
@app_commands.describe(word="追加したい単語やフレーズ", weight="重み(省略時3。しきい値以上なら単体でも検知)")
@app_commands.checks.has_permissions(administrator=True)
async def reisho_add_word(interaction: discord.Interaction, word: str, weight: int = 3):
    if interaction.guild is None:
        await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
        return

    word = word.strip()
    if not word:
        await interaction.response.send_message("空の単語は追加できません。", ephemeral=True)
        return

    pattern_str = build_flexible_pattern(word)

    for item in KEYWORD_DATA.get("patterns", []):
        if item["pattern"] == pattern_str:
            await interaction.response.send_message(f"「{word}」は既に登録されています。", ephemeral=True)
            return

    KEYWORD_DATA.setdefault("patterns", []).append({
        "pattern": pattern_str,
        "weight": weight,
        "comment": word,
        "custom": True,
    })
    save_keyword_data()
    rebuild_compiled_patterns()

    await interaction.response.send_message(
        f"✅「{word}」を冷笑ワードに追加しました。(重み: {weight})", ephemeral=True
    )


@bot.tree.command(name="reisho_remove_word", description="追加した冷笑検知ワードを削除します(管理者限定)")
@app_commands.describe(word="削除したい単語(/reisho_add_word で追加した時と同じ文字列)")
@app_commands.checks.has_permissions(administrator=True)
async def reisho_remove_word(interaction: discord.Interaction, word: str):
    if interaction.guild is None:
        await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
        return

    word = word.strip()
    before = len(KEYWORD_DATA.get("patterns", []))
    KEYWORD_DATA["patterns"] = [
        item for item in KEYWORD_DATA.get("patterns", [])
        if not (item.get("custom") and item.get("comment") == word)
    ]
    after = len(KEYWORD_DATA["patterns"])

    if before == after:
        await interaction.response.send_message(
            f"「{word}」という追加ワードは見つかりませんでした（標準搭載ワードは削除できません）。",
            ephemeral=True,
        )
        return

    save_keyword_data()
    rebuild_compiled_patterns()
    await interaction.response.send_message(f"🗑️「{word}」を削除しました。", ephemeral=True)


@bot.tree.command(name="reisho_list_words", description="追加した冷笑検知ワードの一覧を表示します")
async def reisho_list_words(interaction: discord.Interaction):
    custom_words = [
        f"・{item['comment']} (重み:{item['weight']})"
        for item in KEYWORD_DATA.get("patterns", [])
        if item.get("custom")
    ]
    if not custom_words:
        await interaction.response.send_message("追加ワードはまだ登録されていません。", ephemeral=True)
        return
    await interaction.response.send_message(
        "**追加された冷笑ワード一覧**\n" + "\n".join(custom_words), ephemeral=True
    )


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "このコマンドは管理者権限を持つユーザーのみ実行できます。"
    else:
        msg = f"エラーが発生しました: {error}"
        log.warning("スラッシュコマンドエラー: %s", error)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


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
    global BOT_READY_AT, _commands_synced

    BOT_READY_AT = discord.utils.utcnow()

    log.info(
        "ログイン完了: %s (id=%s) / 基準時刻=%s",
        bot.user,
        bot.user.id,
        BOT_READY_AT.isoformat()
    )

    for guild in bot.guilds:
        await apply_appearance_to_guild(guild)

    await _ensure_counts_loaded()

    if not update_presence.is_running():
        update_presence.start()

    if not _commands_synced:
        try:
            if DEV_GUILD_ID:
                guild_obj = discord.Object(id=int(DEV_GUILD_ID))
                bot.tree.copy_global_to(guild=guild_obj)
                synced = await bot.tree.sync(guild=guild_obj)
                log.info("スラッシュコマンドをテストサーバーに同期しました(即時反映): %d件", len(synced))
            else:
                synced = await bot.tree.sync()
                log.info("スラッシュコマンドをグローバル同期しました(反映まで最大1時間程度): %d件", len(synced))
            _commands_synced = True
        except discord.HTTPException as e:
            log.warning("スラッシュコマンドの同期に失敗しました: %s", e)


@bot.command(name="shoki")
@commands.has_permissions(administrator=True)  # 管理者権限を持つユーザーのみ実行可能
async def shoki_cmd(ctx: commands.Context):
    async with _state_lock:
        state = _load_state()
        state["unlocked"] = False
        _save_state(state)

    for guild in bot.guilds:
        await _set_nick(guild, LOCKED_NICK)

    try:
        await bot.user.edit(avatar=None)
        log.info("botのアイコンをデフォルトに初期化しました。")
    except discord.HTTPException as e:
        log.warning("アイコンの初期化に失敗しました: %s", e)

    await ctx.reply("解禁状態、ニックネーム、アイコンを初期状態に戻しました。")


@shoki_cmd.error
async def shoki_cmd_error(ctx: commands.Context, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.reply("このコマンドは管理者権限を持つユーザーのみ実行できます。")


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
        "- 同じワードが1メッセージ内で複数回出てくると、その分カウントも多く加算されます\n"
        "- 「う」→「お」（または「お」→「う」）を1文字ずつ連投すると検知します（記号や絵文字での装飾では回避できません）\n"
        "- メッセージに 🇺 → 🇴 の順でリアクションすると、リアクションした人が検知されます\n"
        "- `/reisho_add_word` `/reisho_remove_word` `/reisho_list_words` : 冷笑ワードの追加/削除/一覧（追加・削除は管理者限定）"
    )
    await ctx.reply(text)

# ------------------------------------------------------------
# コマンド: VC接続 / 退出
# ------------------------------------------------------------
@bot.command(name="vc_join")
async def vc_join_cmd(ctx: commands.Context):
    if not ctx.author.voice:
        await ctx.reply("先にボイスチャンネルに入った状態で実行してください。")
        return

    channel = ctx.author.voice.channel
    try:
        vc = await channel.connect(cls=voice_recv.VoiceRecvClient)
        vc.listen(ReishoAudioSink(vc, ctx.channel, ctx.guild))
        await ctx.reply(f"🎙️ `{channel.name}` に接続しました。冷笑のリアルタイム検知を開始します。")
    except discord.ClientException:
        await ctx.reply("すでにボイスチャンネルに接続されています。")

@bot.command(name="vc_leave")
async def vc_leave_cmd(ctx: commands.Context):
    if ctx.voice_client:
        ctx.voice_client.stop_listening()
        await ctx.voice_client.disconnect()
        await ctx.reply("ボイスチャンネルから退出しました。")
    else:
        await ctx.reply("ボイスチャンネルに接続していません。")


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
