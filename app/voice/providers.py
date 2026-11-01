"""Voice providers: browser (real, client-side), mock, Meta stub."""

from __future__ import annotations

from .types import (
    Transcript,
    UnsupportedOperation,
    VoiceCapabilities,
    VoiceError,
)


class BrowserVoiceProvider:
    """Web Speech API in the user's browser: recognition and speech
    synthesis happen on the client; the server only receives text.

    Honest limits: Chrome sends recognition audio to Google's
    servers (needs internet); Firefox/Safari support varies; there
    is no server-side audio, so ``transcribe`` refuses.
    """

    name = "browser"

    capabilities = VoiceCapabilities(stt=True, tts=True,
                                     client_side=True)

    def accept_transcript(self, text: str) -> Transcript:
        cleaned = (text or "").strip()
        if not cleaned:
            raise VoiceError("Empty transcript.")
        return Transcript(text=cleaned[:4000], provider=self.name)

    def transcribe(self, audio: bytes) -> Transcript:
        raise UnsupportedOperation(
            "Browser transcription runs on the client; send the"
            " transcript text, not audio.")


class TextFallbackProvider(BrowserVoiceProvider):
    """Typed input through the same pipeline (no microphone needed)."""

    name = "text"


class MockVoiceProvider:
    """Scripted transcripts for tests and offline development."""

    name = "mock"

    capabilities = VoiceCapabilities(stt=True, tts=True, mock=True)

    def __init__(self, script: tuple[str, ...] = ()) -> None:
        self._script = list(script)
        self.accepted: list[str] = []

    def accept_transcript(self, text: str) -> Transcript:
        cleaned = (text or "").strip()
        if not cleaned:
            raise VoiceError("Empty transcript.")
        self.accepted.append(cleaned[:4000])
        return Transcript(text=cleaned[:4000], provider=self.name,
                          mock=True)

    def transcribe(self, audio: bytes) -> Transcript:
        if not audio:
            raise VoiceError("Empty audio.")
        if not self._script:
            raise VoiceError("Mock script exhausted; no transcript left.")
        return Transcript(text=self._script.pop(0), provider=self.name,
                          mock=True, audio_seconds=1.0)


#: Meta Model API id + public price (Sept 2026) for the day a real
#: adapter is wired. Audio is priced per minute, NOT per token, so
#: it deliberately stays out of the token-price tables in budget.py.
META_TRANSCRIBE_MODEL = "muse-voice-transcribe-1.0"
META_TRANSCRIBE_USD_PER_AUDIO_HOUR = 0.18


class MetaTranscribeProvider:
    """Honest stub for server-side Meta transcription.

    Refuses every call: no audio-upload endpoint, credentials, or
    streaming client exists yet. Use the browser provider for real
    $0 speech today.
    """

    name = "meta"

    capabilities = VoiceCapabilities(stt=True, streaming=True,
                                     diarization=True)

    def accept_transcript(self, text: str) -> Transcript:
        raise UnsupportedOperation(
            "Meta transcription is not configured: no audio pipeline"
            " or credentials exist. Use the browser provider.")

    def transcribe(self, audio: bytes) -> Transcript:
        raise UnsupportedOperation(
            "Meta transcription is not configured: no audio pipeline"
            " or credentials exist. Use the browser provider.")
