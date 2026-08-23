# BypassHormuz daily briefing → video

Turns each day's **BYPASS: The Daily Situation Brief** podcast episode into a
captioned video narrated by the real Marcus Kade audio, and publishes it.

Source feed: <https://bypasshormuz.com/podcast/feed.xml>

## How it works

| Step | What happens | Cost |
| --- | --- | --- |
| 1 | Read the feed, take the newest episode (or `--date`) | free |
| 2 | Download the published MP3. **This is the voiceover** — no TTS is generated | free |
| 3 | Transcribe via AIMLAPI (`deepgram/nova-3`, smart_format) to recover the script with punctuation and word timestamps. Cached per episode | ~$0.01 once |
| 4 | Pick a sentence-aligned ~55s slice for Shorts, or use the whole briefing | free |
| 5 | MoneyPrinterPro builds scenes, pulls Pixabay stock footage, burns word-by-word subtitles and muxes the podcast audio | free on stock |
| 6 | Optional upload to YouTube / TikTok / Post Bridge, plus a webhook ping | free |

This is a **read-only consumer of the public feed**. It never writes to, deploys,
or otherwise touches the `bypass-monitor` Cloudflare Worker.

The voice in the video is byte-identical to the podcast, so the brand voice stays
consistent across audio and video with zero extra TTS spend.

## Setup

```bash
cp config.bypass.example.json config.json
# set openai_api_key to your AIMLAPI key, and pixabay_api_key
pip install -r requirements.txt
```

Add a YouTube account once (`python src/main.py`) so a Firefox profile and
account nickname exist, then:

```bash
# see what would be narrated, without rendering
python scripts/bypass_daily.py --dry-run

# render a ~55s Short from today's briefing
python scripts/bypass_daily.py --mode short

# render the full ~3.5 minute briefing
python scripts/bypass_daily.py --mode long

# render and publish
python scripts/bypass_daily.py --mode short --upload --crosspost
```

Useful flags: `--date 2026-08-21`, `--seconds 45`, `--account <nickname>`,
`--webhook <url>`.

## Daily automation

```cron
15 19 * * * cd /path/to/money-printer-pro && /usr/bin/python3 scripts/bypass_daily.py --mode short --upload >> logs/bypass.log 2>&1
```

The worker generates each episode shortly after 00:00 UTC, so any run after that
picks up the same day's brief. Re-running the same day reuses the cached
transcript and costs nothing.

## Notes and gotchas

- **Requires a real host.** This is Python + ffmpeg + MoviePy (and Selenium for
  YouTube upload). It cannot run on Cloudflare Workers. A small VM, a home
  machine, or a GitHub Actions runner is needed.
- **Transcription errors.** Generic STT mangles the briefing's transliterated
  place names. `TERM_CORRECTIONS` in `src/bypass_narrator.py` fixes the ones seen
  in production (Ceyhan, Baniyas, Bab el-Mandeb, Marcus Kade, strait/straight).
  Add to that dict when a new one appears rather than editing transcripts.
- **`stt_provider` is `script_based`** in the template config. The transcript is
  already in hand, so a second STT pass at render time would be wasted spend.
- **Stock over AI art.** `asset_strategy` is `stock` because real shipping, port
  and refinery footage reads as credible for a news brief, and it is free. Switch
  to `mixed` with an image provider if you want generated frames.
- **Segment quality.** `pick_segment()` only ever cuts on sentence boundaries and
  skips the host intro, so a Short never opens mid-thought. Check the `--dry-run`
  output before a first unattended run.
