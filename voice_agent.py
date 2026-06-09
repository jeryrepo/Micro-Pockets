"""
voice_agent.py
──────────────
Voice Agent — handles WhatsApp audio messages.

Flow:
  1. Receive audio media ID from WhatsApp webhook
  2. Download audio bytes via WhatsApp Media API
  3. Pass base64 audio to Gemini for transcription + language detection
  4. Return transcript + detected language to main.py

When moving to GCP:
  Replace _download_audio + base64 approach with GCS upload + URI passing.
"""

import os
import base64
import httpx
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

WA_ACCESS_TOKEN = os.getenv("WA_ACCESS_TOKEN")

client = genai.Client(
    vertexai=True,
    project=os.getenv("GCP_PROJECT_ID", "project-0d1e3eff-a4b4-476c-b36"),
    location=os.getenv("GCP_LOCATION", "us-central1")
)
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-preview-05-20")


# ─────────────────────────────────────────────────────────────────────
#  DOWNLOAD AUDIO FROM WHATSAPP
# ─────────────────────────────────────────────────────────────────────

async def _download_audio(media_id: str) -> bytes:
    """
    Download audio bytes from WhatsApp Media API.
    Step 1: Get the download URL from the media ID.
    Step 2: Download the actual audio bytes.
    """
    headers = {"Authorization": f"Bearer {WA_ACCESS_TOKEN}"}

    async with httpx.AsyncClient() as client_http:
        # Step 1 — get media URL
        meta_resp = await client_http.get(
            f"https://graph.facebook.com/v19.0/{media_id}",
            headers=headers
        )
        meta_resp.raise_for_status()
        download_url = meta_resp.json().get("url")

        if not download_url:
            raise ValueError(f"No URL returned for media_id {media_id}")

        # Step 2 — download audio
        audio_resp = await client_http.get(download_url, headers=headers)
        audio_resp.raise_for_status()
        return audio_resp.content


# ─────────────────────────────────────────────────────────────────────
#  TRANSCRIBE + DETECT LANGUAGE
# ─────────────────────────────────────────────────────────────────────

async def transcribe(media_id: str) -> dict:
    """
    Download and transcribe a WhatsApp voice note.

    Returns:
    {
      "transcript": str,       — what the user said
      "language":   str,       — detected language code e.g. "en", "ur", "ar"
      "language_name": str,    — human readable e.g. "English", "Urdu", "Arabic"
      "success":    bool
    }
    """
    try:
        audio_bytes  = await _download_audio(media_id)
        audio_b64    = base64.b64encode(audio_bytes).decode("utf-8")

        response = client.models.generate_content(
            model=MODEL,
            contents=[
                types.Part.from_bytes(
                    data=base64.b64decode(audio_b64),
                    mime_type="audio/ogg; codecs=opus"   # WhatsApp sends ogg/opus
                ),
                "Transcribe this audio message exactly as spoken. "
                "Then on a new line write: LANGUAGE: <language_code> (<language_name>). "
                "Example: LANGUAGE: ur (Urdu). "
                "Do not add any other text."
            ]
        )

        raw = response.text.strip()
        print(f"VOICE TRANSCRIPTION RAW: {raw}")

        # Parse transcript and language from response
        lines    = raw.split("\n")
        lang_line = next((l for l in lines if l.startswith("LANGUAGE:")), None)

        if lang_line:
            transcript = "\n".join(
                l for l in lines if not l.startswith("LANGUAGE:")
            ).strip()
            lang_part  = lang_line.replace("LANGUAGE:", "").strip()

            # Extract code and name e.g. "ur (Urdu)"
            import re
            match = re.match(r"(\w+)\s*\(([^)]+)\)", lang_part)
            if match:
                lang_code = match.group(1).lower()
                lang_name = match.group(2)
            else:
                lang_code = lang_part.split()[0].lower()
                lang_name = lang_part
        else:
            transcript = raw
            lang_code  = "en"
            lang_name  = "English"

        print(f"VOICE: lang={lang_code} transcript={transcript[:80]}")

        return {
            "transcript":    transcript,
            "language":      lang_code,
            "language_name": lang_name,
            "success":       True
        }

    except Exception as e:
        print(f"VOICE AGENT error: {e}")
        return {
            "transcript":    "",
            "language":      "en",
            "language_name": "English",
            "success":       False,
            "error":         str(e)
        }