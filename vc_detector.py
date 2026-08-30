import os
import io
import asyncio
import logging
import unicodedata
from collections import defaultdict
import discord
from discord.ext import voice_recv
import faster_whisper
from gtts import gTTS
from pydub import AudioSegment
from pydub.generators import Sine

log = logging.getLogger("reisho-bot")

# CPU環境向けに軽量なtinyモデルを使用
try:
    whisper_model = faster_whisper.WhisperModel("tiny", device="cpu", compute_type="int8")
except Exception as e:
    log.error(f"faster-whisper 初期化エラー: {e}")
    whisper_model = None

COMPILED_PATTERNS = []

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
            out.append(re.escape(ch) if 're' in globals() else ch) # 安全のため
            i += 1
    return "".join(out)

import re
def rebuild_compiled_patterns(chat_words_data: dict):
    global COMPILED_PATTERNS
    compiled = []
    for item in chat_words_data.get("patterns", []):
        try:
            compiled.append({
                "regex": re.compile(item["pattern"], re.IGNORECASE),
                "weight": item.get("weight", 1),
            })
        except: pass
    COMPILED_PATTERNS = compiled

def calc_chat_score(text: str):
    normalized = unicodedata.normalize("NFKC", text)
    normalized = re.sub(r"[\u200b\u200c\u200d\ufeff\s]", "", normalized)
    score, match_count = 0, 0
    for item in COMPILED_PATTERNS:
        matches = list(item["regex"].finditer(normalized))
        if matches:
            score += item["weight"] * len(matches)
            match_count += len(matches)
    return score, match_count

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

class ReishoAudioSink(voice_recv.AudioSink):
    def __init__(self, vc, text_channel, guild: discord.Guild, vc_words_data, on_detect_cb):
        super().__init__()
        self.vc = vc
        self.text_channel = text_channel
        self.guild = guild
        self.vc_words_data = vc_words_data
        self.on_detect_cb = on_detect_cb
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
            asyncio.run_coroutine_threadsafe(self.process_audio(user, pcm_data), asyncio.get_event_loop())

    async def process_audio(self, user, pcm_data):
        if whisper_model is None or not self.vc_words_data: return

        audio_segment = AudioSegment(data=bytes(pcm_data), sample_width=2, frame_rate=48000, channels=2)
        audio_segment = audio_segment.set_frame_rate(16000).set_channels(1)
        wav_io = io.BytesIO()
        audio_segment.export(wav_io, format="wav")
        wav_io.seek(0)

        segments, _ = await asyncio.get_event_loop().run_in_executor(None, lambda: whisper_model.transcribe(wav_io, language="ja"))
        text = "".join([s.text for s in segments]).strip()

        if text:
            matched = any(w in text for w in self.vc_words_data)
            if matched:
                await self.on_detect_cb(self.guild, self.text_channel, user, text)
