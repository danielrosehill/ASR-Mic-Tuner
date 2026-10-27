"""AI review of a measurement through OpenRouter.

The model gets the measured numbers (always) and, only if the user enables
it, a 16 kHz mono MP3 of the sample. Audio needs an audio-capable model
(Gemini via OpenRouter); numbers alone work with any chat model.

Measured facts in, advice out: the prompt tells the model which figures were
measured so it does not invent acoustic observations it cannot support.
"""

import base64
import json
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request

from . import APP, VERSION

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
KEY_ENDPOINT = "https://openrouter.ai/api/v1/key"

SYSTEM_PROMPT = """You are an audio engineer who sets up microphones for \
speech recognition (dictation, ASR). You are given measurements taken by a \
Linux tool called ASR Mic Tuner: the device, its real analog gain in dB, and the \
results of analysing a recording (room tone and/or a voice sample). \
Optionally you also receive the recording itself.

Rules:
- Base every claim on the supplied numbers or, if present, the audio. If you \
  infer something, say it is an inference.
- Analog gain is set by ALSA control index; PipeWire's percentage is not the \
  analog gain. Clipping is analog damage that no downstream filter repairs.
- Targets: speech peaks -18 to -10 dBFS (aim -12), noise floor at or below \
  -50 dBFS (ideally -60), SNR >= 20 dB (ideally 30), no clipped runs, \
  bandwidth above 7 kHz.
- Prefer physical fixes (distance, angle, room, mic choice) over processing \
  when gain is not the problem.
- Be concise and concrete.

Reply with a JSON object only:
{"verdict": "one sentence",
 "score_comment": "one sentence on the readiness score, if given",
 "recommendations": [{"priority": "high|medium|low", "title": "...", "detail": "..."}],
 "suggested_gain_db": number or null,
 "asr_chain_suggestions": ["optional processing for the dictation app, e.g. high-pass 80 Hz"],
 "voice_notes": "observations about the speaker's voice relevant to mic setup, or null"}"""


class AIError(Exception):
    pass


def api_key(settings):
    return (settings.get("openrouter_api_key") or os.environ.get("OPENROUTER_API_KEY") or "").strip()


def _post(url, payload, key, timeout=120):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/danielrosehill/ASR-Mic-Tuner",
            "X-Title": f"{APP} {VERSION}",
        },
        method="POST" if payload is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:400]
        raise AIError(f"OpenRouter HTTP {exc.code}: {body}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AIError(f"OpenRouter unreachable: {exc}") from exc


def check_key(settings):
    """Verify the key in one call. Returns a short human description."""
    key = api_key(settings)
    if not key:
        raise AIError("No OpenRouter key set (Settings, or OPENROUTER_API_KEY).")
    data = _post(KEY_ENDPOINT, None, key, timeout=20).get("data", {})
    usage = data.get("usage")
    limit = data.get("limit")
    tier = "free tier" if data.get("is_free_tier") else "paid"
    return f"Key OK ({tier}; usage ${usage:.2f}" + (f" of ${limit:.2f})" if limit else ")")


def _encode_audio(path):
    if not path or not shutil.which("ffmpeg"):
        return None
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        out = tmp.name
    try:
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(path), "-ac", "1",
             "-ar", "16000", "-b:a", "48k", out],
            capture_output=True,
        )
        if proc.returncode != 0:
            return None
        with open(out, "rb") as fh:
            return base64.b64encode(fh.read()).decode()
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass


def _strip_bands(metrics):
    """Band tables are long; keep them but at one decimal to save tokens."""
    if not isinstance(metrics, dict):
        return metrics
    slim = dict(metrics)
    if "bands" in slim:
        slim["bands"] = {f"{b['hz']:g}": b["db"] for b in slim["bands"]}
    if isinstance(slim.get("hum"), dict):
        slim["hum"] = {k: v for k, v in slim["hum"].items() if k != "lines"}
    return slim


def review(settings, context, sample_path=None):
    """Ask the model for a review. `context` is a dict of facts (device,
    gain, measurement metrics, room profile, recent history). Returns the
    parsed JSON reply, with `_model` and `_usage` added."""
    key = api_key(settings)
    if not key:
        raise AIError("No OpenRouter key set (Settings, or OPENROUTER_API_KEY).")
    model = settings.get("openrouter_model") or "google/gemini-3.8-flash"

    facts = dict(context)
    for k in ("measurement", "room_profile"):
        if isinstance(facts.get(k), dict):
            facts[k] = _strip_bands(facts[k])
    content = [{"type": "text",
                "text": "Measurements (JSON):\n" + json.dumps(facts, indent=1)}]
    if settings.get("ai_include_audio") and sample_path:
        audio = _encode_audio(sample_path)
        if audio:
            content.append({"type": "input_audio",
                            "input_audio": {"data": audio, "format": "mp3"}})
            content[0]["text"] += "\n\nThe recording is attached."

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
    }
    data = _post(ENDPOINT, payload, key)
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AIError(f"Unexpected reply: {json.dumps(data)[:300]}") from exc
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    try:
        reply = json.loads(text[text.find("{"): text.rfind("}") + 1])
    except json.JSONDecodeError:
        reply = {"verdict": text}
    reply["_model"] = data.get("model", model)
    reply["_usage"] = data.get("usage")
    return reply
