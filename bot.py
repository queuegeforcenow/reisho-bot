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

import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.ext import voice_recv

import faster_whisper
from gtts import gTTS
from pydub import AudioSegment
from pydub.generators import Sine

# ------------------------------------------------------------
# 設定
# ------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("reisho-bot")

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
COMMAND_PREFIX = os.environ.get("COMMAND_PREFIX", "!")
JSON_CHANNEL_ID = os.environ.get("JSON_CHANNEL_ID")  # JSON保存・読み込み用チャンネル
PORT = int(os.environ.get("PORT", 8000))
EMOJI_NAME = os.environ.get("REISHO_EMOJI_NAME", "reisho") 

TEXT_COMBO_WINDOW_SECONDS = float(os.environ.get("REISHO_TEXT_COMBO_WINDOW_SECONDS", 60))
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
# JSON永続化クラス (チャット/VC分離対応)
# ------------------------------------------------------------
class DiscordJsonStore:
    def __init__(self, filename, default_data, title_text):
        self.filename = filename
        self.data = default_data
        self.title_text = title_text
        self.msg_id = None
        self.lock = asyncio.Lock()
        self.channel = None

    async def sync_down(self, bot_instance, channel_id):
        if not channel_id:
            if os.path.exists(self.filename):
                try:
                    with open(self.filename, 'r', encoding='utf-8') as f:
                        self.data = json.load(f)
                except Exception: pass
            return

        self.channel = bot_instance.get_channel(int(channel_id))
        if not self.channel:
            try: self.channel = await bot_instance.fetch_channel(int(channel_id))
            except: return

        async for msg in self.channel.history(limit=50):
            if msg.author.id == bot_instance.user.id and msg.attachments:
                for att in msg.attachments:
                    if att.filename == self.filename:
                        try:
                            content = await att.read()
                            self.data = json.loads(content.decode('utf-8'))
                            self.msg_id = msg.id
                            return
                        except Exception as e:
                            log.error(f"{self.filename}の読み込みエラー: {e}")

    async def save(self):
        async with self.lock:
            if not self.channel:
                with open(self.filename, 'w', encoding='utf-8') as f:
                    json.dump(self.data, f, ensure_ascii=False, indent=2)
                return
            
            payload = json.dumps(self.data, ensure_ascii=False, indent=2).encode('utf-8')
            file = discord.File(io.BytesIO(payload), filename=self.filename)
            
            if self.msg_id:
                try:
                    msg = await self.channel.fetch_message(self.msg_id)
                    await msg.edit(attachments=[file])
                    return
                except discord.NotFound:
                    self.msg_id = None

            new_msg = await self.channel.send(content=self.title_text, file=file)
            self.msg_id = new_msg.id

chat_counts_store = DiscordJsonStore("chat_counts.json", {}, "📊 チャット冷笑ランキング (自動保存)")
vc_counts_store = DiscordJsonStore("vc_counts.json", {}, "🎙️ VC冷笑ランキング (自動保存)")
chat_words_store = DiscordJsonStore("chat_words.json", {"threshold": 3, "patterns": []}, "📝 チャット用冷笑ワード (ファイル送信で上書き更新可能)")
vc_words_store = DiscordJsonStore("vc_words.json", [], "🗣️ VC用冷笑ワード (ファイル送信で上書き更新可能)")

# ------------------------------------------------------------
# テキスト正規化・スコア計算
# ------------------------------------------------------------
COMPILED_PATTERNS = []

def normalize_for_match(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    normalized = re.sub(r"[\u200b\u200c\u200d\ufeff\s]", "", normalized)
    return normalized

def normalize_single_char(text: str):
    raw = text.strip()
    if raw == REGIONAL_U: return "う"
    if raw == REGIONAL_O: return "お"
    normalized = unicodedata.normalize("NFKC", text)
    core = re.sub(r"[^\w]", "", normalized).replace("ー", "")
    if core in ("う", "ウ", "ぅ", "ゥ", "u", "U"): return "う"
    if core in ("お", "オ", "ぉ", "ォ", "o", "O"): return "お"
    return None

def build_flexible_pattern(word: str) -> str:
    out = []
    i = 0
    while i < len(word):
        ch = word[i]
        if ch in "wWｗＷ":
            j = i
            while j < len(word) and word[j] in "wWｗＷ": j += 1
            out.append(f"[wWｗＷ]{{{j - i},}}")
            i = j
        else:
            out.append(re.escape(ch))
            i += 1
    return "".join(out)

def rebuild_compiled_patterns():
    global COMPILED_PATTERNS
    compiled = []
    for item in chat_words_store.data.get("patterns", []):
        try: compiled.append({"regex": re.compile(item["pattern"], re.IGNORECASE), "weight": item.get("weight", 1)})
        except: pass
    COMPILED_PATTERNS = compiled

def calc_chat_score(text: str):
    normalized = normalize_for_match(text)
    score, match_count = 0, 0
    for item in COMPILED_PATTERNS:
        matches = list(item["regex"].finditer(normalized))
        if matches:
            score += item["weight"] * len(matches)
            match_count += len(matches)
    return score, match_count

# ------------------------------------------------------------
# 音声生成 (ピンポン音 + TTS)
# ------------------------------------------------------------
def create_notification_audio(username: str, text_content: str = "") -> str:
    tone1 = Sine(523.25).to_audio_segment(duration=300).apply_gain(-10)
    tone2 = Sine(659.25).to_audio_segment(duration=500).apply_gain(-10)
    ping_pong = tone1 + tone2
    
    tts_text = f"{username}さんが冷笑をしました。内容、{text_content}" if text_content else f"{username}さんが冷笑をしました。"
    tts_file = f"tts_{username}_{discord.utils.utcnow().timestamp()}.mp3"
    gTTS(text=tts_text, lang='ja').save(tts_file)
    
    tts_audio = AudioSegment.from_mp3(tts_file)
    combined = ping_pong + AudioSegment.silent(duration=200) + tts_audio
    
    output_file = f"notify_{username}_{discord.utils.utcnow().timestamp()}.wav"
    combined.export(output_file, format="wav")
    
    if os.path.exists(tts_file): os.remove(tts_file)
    return output_file

# ------------------------------------------------------------
# 解禁状態管理
# ------------------------------------------------------------
_state_lock = asyncio.Lock()
def _load_state() -> dict:
    if not os.path.exists(STATE_PATH): return {"unlocked": False}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f: return json.load(f)
    except: return {"unlocked": False}

def _save_state(state: dict):
    with open(STATE_PATH + ".tmp", "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(STATE_PATH + ".tmp", STATE_PATH)

async def is_unlocked() -> bool:
    async with _state_lock: return _load_state().get("unlocked", False)

async def unlock_bot_globally():
    async with _state_lock:
        state = _load_state()
        if state.get("unlocked", False): return
        state["unlocked"] = True
        _save_state(state)
    
    for guild in bot.guilds:
        if guild.me.nick != UNLOCKED_NICK:
            try: await guild.me.edit(nick=UNLOCKED_NICK)
            except: pass

    if os.path.exists(ICON_PATH):
        try:
            with open(ICON_PATH, "rb") as f: await bot.user.edit(avatar=f.read())
        except: pass

# ------------------------------------------------------------
# Discord Bot 本体
# ------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True 
intents.guilds = True
intents.members = True
intents.voice_states = True

whisper_model = faster_whisper.WhisperModel("tiny", device="cpu", compute_type="int8")
bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents, help_command=None)

_single_char_state = {}
_reaction_sequence_state = {}
BOT_READY_AT = None

@tasks.loop(seconds=30)
async def update_presence():
    await bot.change_presence(activity=discord.Game(name=f"{len(bot.guilds)}サーバーを監視中"))

@bot.event
async def on_ready():
    global BOT_READY_AT
    BOT_READY_AT = discord.utils.utcnow()
    
    await chat_counts_store.sync_down(bot, JSON_CHANNEL_ID)
    await vc_counts_store.sync_down(bot, JSON_CHANNEL_ID)
    await chat_words_store.sync_down(bot, JSON_CHANNEL_ID)
    await vc_words_store.sync_down(bot, JSON_CHANNEL_ID)
    rebuild_compiled_patterns()

    for guild in bot.guilds:
        nick = UNLOCKED_NICK if await is_unlocked() else LOCKED_NICK
        if guild.me.nick != nick:
            try: await guild.me.edit(nick=nick)
            except: pass

    if not update_presence.is_running(): update_presence.start()
    log.info(f"ログイン完了: {bot.user}")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None: return
    if BOT_READY_AT and message.created_at < BOT_READY_AT: return

    # JSONファイルによるワード直接更新
    if JSON_CHANNEL_ID and str(message.channel.id) == JSON_CHANNEL_ID and message.attachments:
        for att in message.attachments:
            if att.filename == "chat_words.json":
                content = await att.read()
                chat_words_store.data = json.loads(content.decode('utf-8'))
                await chat_words_store.save()
                rebuild_compiled_patterns()
                await message.reply("✅ `chat_words.json` を読み込みました。")
                return
            elif att.filename == "vc_words.json":
                content = await att.read()
                vc_words_store.data = json.loads(content.decode('utf-8'))
                await vc_words_store.save()
                await message.reply("✅ `vc_words.json` を読み込みました。")
                return

    # 単文字コンボ
    normalized_char = normalize_single_char(message.content)
    if normalized_char:
        key = (message.channel.id, message.author.id)
        prev = _single_char_state.get(key)
        now = discord.utils.utcnow()
        if prev and prev["content"] != normalized_char and (now - prev["timestamp"]).total_seconds() <= TEXT_COMBO_WINDOW_SECONDS:
            combo_text = f"{prev['content']}→{normalized_char}（連続投稿）"
            await credit_reisho(message.guild, message, message.author, combo_text, target="chat", amount=1)
            _single_char_state.pop(key, None)
        else:
            _single_char_state[key] = {"content": normalized_char, "timestamp": now}
        await bot.process_commands(message)
        return

    # チャットスコア判定
    score, match_count = calc_chat_score(message.content)
    if score >= chat_words_store.data.get("threshold", 3):
        await credit_reisho(message.guild, message, message.author, message.content[:100], target="chat", amount=max(match_count, 1))

    await bot.process_commands(message)

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if not payload.guild_id or (payload.member and payload.member.bot): return
    emoji_str = payload.emoji.name
    if emoji_str not in (REGIONAL_U, REGIONAL_O): return

    now = discord.utils.utcnow()
    key = (payload.message_id, payload.user_id)
    seq = _reaction_sequence_state.setdefault(key, [])
    seq.append((emoji_str, now))
    seq[:] = [(e, t) for (e, t) in seq if (now - t).total_seconds() <= REACTION_COMBO_WINDOW_SECONDS]

    if len(seq) >= 2 and seq[-2][0] == REGIONAL_U and seq[-1][0] == REGIONAL_O:
        guild = bot.get_guild(payload.guild_id)
        channel = guild.get_channel(payload.channel_id)
        if not channel: return
        try: target_message = await channel.fetch_message(payload.message_id)
        except: return
        
        reactor = payload.member or guild.get_member(payload.user_id)
        if reactor:
            await credit_reisho(guild, target_message, reactor, "🇺🇴 リアクションコンボ", target="chat", amount=1)
        _reaction_sequence_state.pop(key, None)

async def credit_reisho(guild, reply_target, user, preview, target="chat", amount=1):
    emoji = discord.utils.get(guild.emojis, name=EMOJI_NAME)
    try: await reply_target.add_reaction(emoji or "😏")
    except: pass

    store = chat_counts_store if target == "chat" else vc_counts_store
    gid, uid = str(guild.id), str(user.id)
    store.data.setdefault(gid, {})
    store.data[gid][uid] = store.data[gid].get(uid, 0) + amount
    await store.save()
    total = store.data[gid][uid]

    extra = f"（今回 +{amount}）" if amount > 1 else ""
    kind = "🎙️ **VC**" if target == "vc" else ""
    await reply_target.reply(f"{kind} {user.mention} 冷笑を検知しました！ 内容：{preview}{extra}\n(累計: {total}回)", mention_author=True)
    await unlock_bot_globally()

# ------------------------------------------------------------
# プレフィックスコマンド (管理者強制コマンド)
# ------------------------------------------------------------
@bot.command(name="sync")
@commands.has_permissions(administrator=True)
async def sync_cmd(ctx: commands.Context, target: str = None):
    """
    !sync と打つと現在のサーバーに即座に同期。
    !sync global と打つと全サーバーへ同期（反映に時間がかかります）。
    """
    await ctx.send("🔄 スラッシュコマンドを同期中...")
    try:
        if target == "global":
            synced = await bot.tree.sync()
            await ctx.send(f"✅ グローバルに {len(synced)} 個のコマンドを同期しました。(反映まで最大1時間)")
        else:
            bot.tree.copy_global_to(guild=ctx.guild)
            synced = await bot.tree.sync(guild=ctx.guild)
            await ctx.send(f"✅ このサーバーに {len(synced)} 個のコマンドを即時同期しました！")
    except Exception as e:
        await ctx.send(f"❌ 同期エラー: {e}")

@bot.command(name="shoki")
@commands.has_permissions(administrator=True)
async def shoki_cmd(ctx: commands.Context):
    _save_state({"unlocked": False})
    for guild in bot.guilds:
        try: await guild.me.edit(nick=LOCKED_NICK)
        except: pass
    try: await bot.user.edit(avatar=None)
    except: pass
    await ctx.reply("解禁状態、ニックネーム、アイコンを初期状態に戻しました。")

# ------------------------------------------------------------
# スラッシュコマンド群
# ------------------------------------------------------------
@bot.tree.command(name="chat_ranking", description="チャットでの冷笑検知ランキングを表示")
async def chat_ranking(interaction: discord.Interaction):
    await _display_ranking(interaction, chat_counts_store.data, "🏆 チャット冷笑ランキング")

@bot.tree.command(name="vc_ranking", description="VCでの冷笑検知ランキングを表示")
async def vc_ranking(interaction: discord.Interaction):
    await _display_ranking(interaction, vc_counts_store.data, "🎙️ VC冷笑ランキング")

async def _display_ranking(interaction, data_dict, title):
    gid = str(interaction.guild.id)
    g_data = data_dict.get(gid, {})
    if not g_data: return await interaction.response.send_message("まだ検知されていません。", ephemeral=False)
    
    ranked = sorted(g_data.items(), key=lambda kv: kv[1], reverse=True)
    total_all = sum(v for _, v in ranked)
    
    lines = []
    medals = ["🥇", "🥈", "🥉"]
    for i, (uid, count) in enumerate(ranked[:25]):
        member = interaction.guild.get_member(int(uid))
        name = member.display_name if member else f"(不明: {uid})"
        prefix = medals[i] if i < len(medals) else f"`{i + 1:>2}位`"
        lines.append(f"{prefix} **{name}** — {count}回 ({(count/total_all*100):.1f}%)")
    
    embed = discord.Embed(title=title, description=f"合計検知数: {total_all}回\n\n" + "\n".join(lines), color=discord.Color.orange())
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="add_chat_reisho_word", description="チャット用のワードを追加(管理者限定)")
@app_commands.checks.has_permissions(administrator=True)
async def add_chat_reisho_word(interaction: discord.Interaction, word: str, weight: int = 3):
    word = word.strip()
    pattern_str = build_flexible_pattern(word)
    chat_words_store.data.setdefault("patterns", []).append({"pattern": pattern_str, "weight": weight, "comment": word})
    await chat_words_store.save()
    rebuild_compiled_patterns()
    await interaction.response.send_message(f"✅ チャット用に「{word}」を追加しました。(重み: {weight})", ephemeral=True)

@bot.tree.command(name="add_vc_reisho_word", description="VC用のワードを追加(管理者限定)")
@app_commands.checks.has_permissions(administrator=True)
async def add_vc_reisho_word(interaction: discord.Interaction, word: str):
    word = word.strip()
    if word not in vc_words_store.data:
        vc_words_store.data.append(word)
        await vc_words_store.save()
        await interaction.response.send_message(f"🎙️ VC用に「{word}」を追加しました。", ephemeral=True)
    else:
        await interaction.response.send_message("既に登録されています。", ephemeral=True)

@bot.tree.command(name="reisho_chat_list_word", description="チャット用ワード一覧を表示")
async def reisho_chat_list_word(interaction: discord.Interaction):
    words = chat_words_store.data.get("patterns", [])
    if not words: return await interaction.response.send_message("未登録です。", ephemeral=True)
    await interaction.response.send_message("**チャット用一覧**\n" + "\n".join([f"・{w['comment']} (重み: {w['weight']})" for w in words]), ephemeral=True)

@bot.tree.command(name="reisho_vc_list_word", description="VC用ワード一覧を表示")
async def reisho_vc_list_word(interaction: discord.Interaction):
    words = vc_words_store.data
    if not words: return await interaction.response.send_message("未登録です。", ephemeral=True)
    await interaction.response.send_message("**VC用一覧**\n" + "\n".join([f"・{w}" for w in words]), ephemeral=True)

@bot.tree.command(name="reisho_chat_remove_word", description="チャット用ワードを削除(管理者限定)")
@app_commands.checks.has_permissions(administrator=True)
async def reisho_chat_remove_word(interaction: discord.Interaction, word: str):
    patterns = chat_words_store.data.get("patterns", [])
    new_patterns = [p for p in patterns if p.get("comment") != word]
    if len(patterns) == len(new_patterns): return await interaction.response.send_message("見つかりませんでした。", ephemeral=True)
    chat_words_store.data["patterns"] = new_patterns
    await chat_words_store.save()
    rebuild_compiled_patterns()
    await interaction.response.send_message(f"🗑️ チャット用から「{word}」を削除しました。", ephemeral=True)

@bot.tree.command(name="reisho_vc_remove_word", description="VC用ワードを削除(管理者限定)")
@app_commands.checks.has_permissions(administrator=True)
async def reisho_vc_remove_word(interaction: discord.Interaction, word: str):
    if word not in vc_words_store.data: return await interaction.response.send_message("見つかりませんでした。", ephemeral=True)
    vc_words_store.data.remove(word)
    await vc_words_store.save()
    await interaction.response.send_message(f"🗑️ VC用から「{word}」を削除しました。", ephemeral=True)

@bot.tree.command(name="reset_ranking", description="【危険】ランキングデータを初期化し、全サーバーに通知します")
@app_commands.choices(target=[app_commands.Choice(name="チャット", value="chat"), app_commands.Choice(name="VC", value="vc")])
@app_commands.checks.has_permissions(administrator=True)
async def reset_ranking(interaction: discord.Interaction, target: app_commands.Choice[str]):
    await interaction.response.defer(ephemeral=False)
    
    if target.value == "chat":
        chat_counts_store.data = {}
        await chat_counts_store.save()
    else:
        vc_counts_store.data = {}
        await vc_counts_store.save()
        
    target_name = "チャット" if target.value == "chat" else "VC"
    success_count = 0
    failed_guilds = []

    for guild in bot.guilds:
        ch = guild.system_channel or next((c for c in guild.text_channels if c.permissions_for(guild.me).send_messages), None)
        if ch:
            try:
                embed = discord.Embed(
                    title="📢 ランキングリセットのお知らせ",
                    description=f"管理者の操作により、全サーバーの**{target_name}冷笑ランキング**がリセットされました！\n今日からまた新たなカウントが始まります。",
                    color=discord.Color.red()
                )
                await ch.send(embed=embed)
                success_count += 1
            except: failed_guilds.append(guild.name)
        else: failed_guilds.append(guild.name)

    result_msg = f"✅ 全サーバーの {target_name} ランキングデータをリセットしました！\n({success_count} サーバーに通知完了)"
    if failed_guilds: result_msg += f"\n⚠️ 以下のサーバーは通知送信に失敗しました:\n{', '.join(failed_guilds)}"
    await interaction.followup.send(result_msg)

# ------------------------------------------------------------
# VCロジック (トグル式 / Whisper処理)
# ------------------------------------------------------------
class ReishoAudioSink(voice_recv.AudioSink):
    def __init__(self, vc, text_channel, guild: discord.Guild):
        super().__init__()
        self.vc = vc
        self.text_channel = text_channel
        self.guild = guild
        self.user_buffers = {}
        
    def wants_opus(self): return False

    def cleanup(self):
        self.user_buffers.clear()

    def write(self, user, data):
        if not user or user.bot: return
        if user.id not in self.user_buffers: self.user_buffers[user.id] = bytearray()
        self.user_buffers[user.id].extend(data.pcm)
        
        if len(self.user_buffers[user.id]) > 3840 * 50 * 3:
            pcm_data = self.user_buffers.pop(user.id)
            asyncio.run_coroutine_threadsafe(self.process_audio(user, pcm_data), bot.loop)

    async def process_audio(self, user, pcm_data):
        audio_segment = AudioSegment(data=bytes(pcm_data), sample_width=2, frame_rate=48000, channels=2)
        audio_segment = audio_segment.set_frame_rate(16000).set_channels(1)
        wav_io = io.BytesIO()
        audio_segment.export(wav_io, format="wav")
        wav_io.seek(0)

        segments, _ = await bot.loop.run_in_executor(None, lambda: whisper_model.transcribe(wav_io, language="ja"))
        text = "".join([s.text for s in segments]).strip()

        if text and vc_words_store.data:
            matched = any(w in text for w in vc_words_store.data)
            if matched:
                # メンション通知とカウント処理を呼び出し
                class DummyMessage:
                    async def add_reaction(self, emoji): pass
                    async def reply(self, *args, **kwargs):
                        await self.channel.send(*args, **kwargs)
                
                dummy = DummyMessage()
                dummy.channel = self.text_channel
                
                await credit_reisho(self.guild, dummy, user, text, target="vc", amount=1)

                # ピンポン音とTTSの再生
                audio_file = create_notification_audio(user.display_name, text)
                if not self.vc.is_playing():
                    source = discord.FFmpegPCMAudio(audio_file)
                    def after_play(error):
                        if os.path.exists(audio_file): os.remove(audio_file)
                    self.vc.play(source, after=after_play)

@bot.tree.command(name="vc_join", description="VCに参加/退出を切り替えます（トグル式）")
async def vc_join(interaction: discord.Interaction):
    if interaction.guild.voice_client:
        interaction.guild.voice_client.stop_listening()
        await interaction.guild.voice_client.disconnect()
        await interaction.response.send_message(embed=discord.Embed(description="🚪 VCから退出しました。", color=discord.Color.red()))
    else:
        if not interaction.user.voice: 
            return await interaction.response.send_message("先にVCに参加してください。", ephemeral=True)
        
        vc_client = await interaction.user.voice.channel.connect(cls=voice_recv.VoiceRecvClient)
        await interaction.response.send_message(embed=discord.Embed(description=f"🎙️ `{interaction.user.voice.channel.name}` に接続しました。冷笑のリアルタイム検知を開始します。", color=discord.Color.green()))
        
        vc_client.listen(ReishoAudioSink(vc_client, interaction.channel, interaction.guild))

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args): pass

if __name__ == "__main__":
    Thread(target=lambda: HTTPServer(("0.0.0.0", PORT), _HealthHandler).serve_forever(), daemon=True).start()
    bot.run(DISCORD_TOKEN)
