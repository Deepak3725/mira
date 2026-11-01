"""Voice value types."""

from __future__ import annotations

from dataclasses import dataclass, field


class VoiceError(Exception):
    """A voice operation failed honestly (no transcript invented)."""


class UnsupportedOperation(VoiceError):
    """The provider cannot do this operation at all."""


@dataclass(frozen=True)
class VoiceCapabilities:
    """What a provider can do. ``client_side`` means the browser (or
    another client) performs the step and the server only sees text."""

    stt: bool = False
    tts: bool = False
    streaming: bool = False
    diarization: bool = False
    client_side: bool = False
    mock: bool = False


@dataclass(frozen=True)
class Transcript:
    """One recognized utterance. ``mock`` distinguishes scripted test
    transcripts from real browser recognition."""

    text: str
    provider: str
    mock: bool = False
    audio_seconds: float = 0.0
    language: str = ""
    extra: dict = field(default_factory=dict)
