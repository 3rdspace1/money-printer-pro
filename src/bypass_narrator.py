"""
BypassHormuz narration adapter for MoneyPrinterPro.

Pulls the daily "BYPASS: The Daily Situation Brief" podcast episode
(https://bypasshormuz.com/podcast/feed.xml), uses the already-generated
Marcus Kade audio as the video voiceover, and reconstructs the narration
script via AIMLAPI speech-to-text so MoneyPrinterPro can build scenes,
image prompts and word-accurate subtitles from it.

Design notes
------------
* No TTS is re-generated. The published MP3 IS the narration, so the video
  voice is identical to the podcast and costs nothing extra.
* No changes are required to the bypass-monitor Cloudflare Worker. The feed
  and the MP3 are already public.
* Deepgram nova-3 via AIMLAPI is used with smart_format, which returns
  punctuated text plus word-level timestamps. Those timestamps are what let
  us cut a clean Short out of a ~4 minute briefing.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import requests

try:  # pragma: no cover - depends on optional local TTS deps
    from classes.Tts import TTS as _TTSBase
except Exception:  # noqa: BLE001
    # KittenTTS / soundfile are only needed when speech is actually
    # synthesised. This pipeline never synthesises, so fall back to a plain
    # base class and stay importable on a lean install.
    _TTSBase = object

FEED_URL = "https://bypasshormuz.com/podcast/feed.xml"
AIMLAPI_STT_CREATE = "https://api.aimlapi.com/v1/stt/create"
AIMLAPI_STT_POLL = "https://api.aimlapi.com/v1/stt/{gid}"
DEFAULT_STT_MODEL = "deepgram/nova-3"

# The briefing is dense with transliterated place names that generic STT
# mangles. These are deterministic post-fixes, applied case-insensitively
# on word boundaries, so the script the video is built from reads correctly.
TERM_CORRECTIONS: dict[str, str] = {
    r"marcus\s+cade": "Marcus Kade",
    r"marcus\s+kaid": "Marcus Kade",
    r"\bsehane\b": "Ceyhan",
    r"\bseyhan\b": "Ceyhan",
    r"\bjahan\b": "Ceyhan",
    r"\bbaniyaz\b": "Baniyas",
    r"\bbanias\b": "Baniyas",
    r"\bkirkuk\b": "Kirkuk",
    r"\bhormuz\b": "Hormuz",
    r"\bbab\s+el\s+mandeb\b": "Bab el-Mandeb",
    r"\bbab\s+al\s+mandeb\b": "Bab el-Mandeb",
    r"\bbob\s+el\s+mandeb\b": "Bab el-Mandeb",
    r"\bbab\s+el\s+mandev\b": "Bab el-Mandeb",
    r"\bstraight\s+of\s+hormuz\b": "Strait of Hormuz",
    r"\b(the|same|that)\s+straight\b": r"\1 strait",
    r"\byambu\b": "Yanbu",
    r"\btartus\b": "Tartus",
    r"\blatakia\b": "Latakia",
    r"\bgwadar\b": "Gwadar",
    r"\bi\s*m\s*e\s*c\b": "IMEC",
    r"\bb\s*r\s*i\b": "BRI",
    r"\bk\s*r\s*g\b": "KRG",
    r"\bp\s*m\s*f\b": "PMF",
    r"\bo\s*p\s*e\s*c\b": "OPEC",
}


class BypassNarratorError(RuntimeError):
    """Raised when the BypassHormuz narration source cannot be prepared."""


@dataclass
class Episode:
    """A single published briefing episode."""

    date: str
    title: str
    description: str
    audio_url: str
    duration: int
    guid: str = ""

    @property
    def subject(self) -> str:
        return self.title.strip()


@dataclass
class Narration:
    """A prepared narration: an audio file plus the matching script."""

    episode: Episode
    audio_path: str
    script: str
    words: list[dict] = field(default_factory=list)
    start: float = 0.0
    end: float = 0.0

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


# --------------------------------------------------------------------------
# Feed
# --------------------------------------------------------------------------

def fetch_episodes(feed_url: str = FEED_URL, timeout: int = 30) -> list[Episode]:
    """Reads the podcast feed and returns episodes, newest first."""
    response = requests.get(feed_url, timeout=timeout)
    response.raise_for_status()

    itunes = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
    root = ET.fromstring(response.content)
    episodes: list[Episode] = []

    for item in root.iter("item"):
        enclosure = item.find("enclosure")
        if enclosure is None:
            continue
        audio_url = enclosure.get("url", "")
        match = re.search(r"(\d{4}-\d{2}-\d{2})", audio_url)
        duration_node = item.findtext(f"{itunes}duration") or "0"
        try:
            duration = int(float(duration_node))
        except ValueError:
            duration = 0
        episodes.append(
            Episode(
                date=match.group(1) if match else "",
                title=(item.findtext("title") or "").strip(),
                description=(item.findtext("description") or "").strip(),
                audio_url=audio_url,
                duration=duration,
                guid=(item.findtext("guid") or "").strip(),
            )
        )

    if not episodes:
        raise BypassNarratorError(f"No episodes found in feed: {feed_url}")
    return episodes


def get_episode(date: str | None = None, feed_url: str = FEED_URL) -> Episode:
    """Returns the episode for `date` (YYYY-MM-DD), or the latest one."""
    episodes = fetch_episodes(feed_url)
    if not date:
        return episodes[0]
    for episode in episodes:
        if episode.date == date:
            return episode
    raise BypassNarratorError(
        f"No episode for {date}. Available: {', '.join(e.date for e in episodes[:7])}"
    )


def download_audio(episode: Episode, target_dir: str, timeout: int = 120) -> str:
    """Downloads the episode MP3 and returns the local path."""
    os.makedirs(target_dir, exist_ok=True)
    path = os.path.join(target_dir, f"bypass-{episode.date or 'latest'}.mp3")
    if os.path.exists(path) and os.path.getsize(path) > 1024:
        return path

    with requests.get(episode.audio_url, stream=True, timeout=timeout) as response:
        response.raise_for_status()
        with open(path, "wb") as handle:
            for chunk in response.iter_content(chunk_size=1 << 16):
                handle.write(chunk)
    return path


# --------------------------------------------------------------------------
# Speech to text
# --------------------------------------------------------------------------

def _apply_corrections(text: str) -> str:
    for pattern, replacement in TERM_CORRECTIONS.items():
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text


def transcribe(
    audio_path: str,
    api_key: str,
    model: str = DEFAULT_STT_MODEL,
    poll_interval: float = 6.0,
    max_wait: float = 600.0,
) -> tuple[str, list[dict]]:
    """
    Transcribes `audio_path` with AIMLAPI and returns (script, words).

    `words` entries are {"word", "start", "end"} in seconds, which the caller
    uses for segmenting and for word-accurate subtitles.
    """
    if not api_key:
        raise BypassNarratorError("AIMLAPI key is required for transcription.")

    headers = {"Authorization": f"Bearer {api_key}"}
    with open(audio_path, "rb") as handle:
        response = requests.post(
            AIMLAPI_STT_CREATE,
            headers=headers,
            data={
                "model": model,
                "smart_format": "true",
                "punctuate": "true",
                "paragraphs": "true",
            },
            files={"audio": handle},
            timeout=180,
        )
    response.raise_for_status()
    generation_id = response.json().get("generation_id")
    if not generation_id:
        raise BypassNarratorError(f"STT submit returned no id: {response.text[:200]}")

    deadline = time.time() + max_wait
    payload: dict = {}
    while time.time() < deadline:
        poll = requests.get(
            AIMLAPI_STT_POLL.format(gid=generation_id), headers=headers, timeout=60
        )
        poll.raise_for_status()
        payload = poll.json()
        status = str(payload.get("status", "")).lower()
        if status in {"completed", "succeeded"}:
            break
        if status in {"failed", "error"}:
            raise BypassNarratorError(f"STT failed: {json.dumps(payload)[:300]}")
        time.sleep(poll_interval)
    else:
        raise BypassNarratorError("STT timed out.")

    try:
        alternative = payload["result"]["results"]["channels"][0]["alternatives"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise BypassNarratorError(f"Unexpected STT payload shape: {exc}") from exc

    script = _apply_corrections(str(alternative.get("transcript", "")).strip())
    if not script:
        raise BypassNarratorError("STT returned an empty transcript.")

    words = [
        {
            "word": _apply_corrections(str(item.get("punctuated_word") or item.get("word", ""))),
            "start": float(item.get("start", 0.0)),
            "end": float(item.get("end", 0.0)),
        }
        for item in alternative.get("words", [])
    ]
    return script, words


def _cache_path(workdir: str, episode: Episode) -> str:
    return os.path.join(workdir, f"bypass-{episode.date or 'latest'}.transcript.json")


def transcribe_cached(
    audio_path: str,
    episode: Episode,
    api_key: str,
    workdir: str,
    model: str = DEFAULT_STT_MODEL,
) -> tuple[str, list[dict]]:
    """Transcribes once per episode and reuses the cached result afterwards."""
    path = _cache_path(workdir, episode)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            cached = json.load(handle)
        return cached["script"], cached.get("words", [])

    script, words = transcribe(audio_path, api_key=api_key, model=model)
    os.makedirs(workdir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"script": script, "words": words}, handle, ensure_ascii=False)
    return script, words


# --------------------------------------------------------------------------
# ffmpeg helpers
# --------------------------------------------------------------------------

def _ffmpeg() -> str:
    binary = shutil.which("ffmpeg")
    if not binary:
        raise BypassNarratorError("ffmpeg is required but was not found on PATH.")
    return binary


def trim_audio(source: str, target: str, start: float, end: float) -> str:
    """Cuts [start, end] out of `source` into `target` (wav, 44.1k mono)."""
    os.makedirs(os.path.dirname(os.path.abspath(target)) or ".", exist_ok=True)
    command = [
        _ffmpeg(), "-y", "-loglevel", "error",
        "-ss", f"{max(0.0, start):.3f}",
    ]
    if end > start:
        command += ["-to", f"{end:.3f}"]
    command += ["-i", source, "-vn", "-ac", "1", "-ar", "44100", target]
    subprocess.run(command, check=True)
    return target


# --------------------------------------------------------------------------
# Segmentation
# --------------------------------------------------------------------------

_SENTENCE_END = re.compile(r"[.!?]$")


def pick_segment(
    words: list[dict],
    target_seconds: float = 55.0,
    min_seconds: float = 30.0,
    skip_intro_seconds: float = 6.0,
) -> tuple[str, float, float, list[dict]]:
    """
    Picks the best contiguous ~`target_seconds` slice of the briefing for a
    Short, snapped to sentence boundaries so it never cuts mid-thought.

    Returns (script, start, end, words_in_segment).
    """
    if not words:
        raise BypassNarratorError("Cannot segment without word timestamps.")

    # Candidate starts: the first word after the host intro, then every word
    # that follows a sentence end.
    starts = [
        index
        for index, item in enumerate(words)
        if index == 0 or _SENTENCE_END.search(words[index - 1]["word"] or "")
    ]
    starts = [i for i in starts if words[i]["start"] >= skip_intro_seconds] or [0]

    best: tuple[float, int, int] | None = None
    for start_index in starts:
        start_time = words[start_index]["start"]
        end_index = start_index
        for index in range(start_index, len(words)):
            if words[index]["end"] - start_time > target_seconds:
                break
            if _SENTENCE_END.search(words[index]["word"] or ""):
                end_index = index
        if end_index <= start_index:
            continue
        duration = words[end_index]["end"] - start_time
        if duration < min_seconds:
            continue
        # Prefer the slice closest to the target length; break ties earlier.
        score = abs(target_seconds - duration)
        if best is None or score < best[0]:
            best = (score, start_index, end_index)

    if best is None:
        # Fall back to a hard cut at the target length.
        start_index = starts[0]
        end_index = start_index
        start_time = words[start_index]["start"]
        while end_index + 1 < len(words) and words[end_index + 1]["end"] - start_time <= target_seconds:
            end_index += 1
        best = (0.0, start_index, end_index)

    _, start_index, end_index = best
    chunk = words[start_index : end_index + 1]
    start = chunk[0]["start"]
    end = chunk[-1]["end"]
    # Re-run corrections on the joined text: multi-word fixes such as
    # "Bab el-Mandeb" can never match when applied word by word.
    script = _apply_corrections(
        " ".join(item["word"] for item in chunk if item["word"]).strip()
    )
    rebased = [
        {"word": item["word"], "start": item["start"] - start, "end": item["end"] - start}
        for item in chunk
    ]
    return script, start, end, rebased


# --------------------------------------------------------------------------
# TTS shim
# --------------------------------------------------------------------------

class PrerecordedTTS(_TTSBase):
    """
    Drop-in TTS replacement that emits an existing recording instead of
    synthesising new speech.

    MoneyPrinterPro's pipeline always calls `synthesize(text, output_file)`,
    so this keeps the whole render path untouched while ensuring the video
    is narrated by the real Marcus Kade podcast audio.
    """

    def __init__(self, source_audio: str, start: float = 0.0, end: float = 0.0) -> None:
        self.source_audio = source_audio
        self.start = float(start)
        self.end = float(end)

    def synthesize(self, text: str, output_file: str, **_: object) -> str:  # noqa: D401
        trim_audio(self.source_audio, output_file, self.start, self.end)
        return output_file


# --------------------------------------------------------------------------
# Top level
# --------------------------------------------------------------------------

def prepare_narration(
    workdir: str,
    api_key: str,
    date: str | None = None,
    mode: str = "short",
    target_seconds: float = 55.0,
    stt_model: str = DEFAULT_STT_MODEL,
) -> Narration:
    """
    Prepares a ready-to-render narration from the BypassHormuz feed.

    mode="short" cuts a sentence-aligned ~`target_seconds` slice for
    Shorts/TikTok. mode="long" uses the whole briefing.
    """
    episode = get_episode(date)
    audio_path = download_audio(episode, workdir)
    script, words = transcribe_cached(audio_path, episode, api_key, workdir, stt_model)

    if mode == "long" or not words:
        return Narration(
            episode=episode,
            audio_path=audio_path,
            script=script,
            words=words,
            start=0.0,
            end=float(episode.duration or 0.0),
        )

    segment_script, start, end, segment_words = pick_segment(
        words, target_seconds=target_seconds
    )
    return Narration(
        episode=episode,
        audio_path=audio_path,
        script=segment_script,
        words=segment_words,
        start=start,
        end=end,
    )
