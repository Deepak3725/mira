"""Voice interface: $0 browser speech + honest provider stubs.

Speech recognition and synthesis run in the user's browser via the
Web Speech API (no keys, no per-minute cost, no audio stored
server-side). The server receives transcript text and runs the same
command pipeline as typed input, so text is the built-in fallback
when no microphone exists.

Meta Muse Voice Transcribe (`muse-voice-transcribe-1.0`,
$0.18/audio-hour via the Meta Model API) is represented as an
honest stub: server-side audio transcription is not wired up, and
the adapter refuses rather than faking transcripts. No live vendor
calls happen in this phase; the suite stays offline.
"""

from .commands import parse_command
from .providers import (
    BrowserVoiceProvider,
    MetaTranscribeProvider,
    MockVoiceProvider,
)
from .registry import available, default_provider, get_provider
from .session import handle_transcript
from .types import (
    Transcript,
    UnsupportedOperation,
    VoiceCapabilities,
    VoiceError,
)

__all__ = [
    "BrowserVoiceProvider",
    "MetaTranscribeProvider",
    "MockVoiceProvider",
    "Transcript",
    "UnsupportedOperation",
    "VoiceCapabilities",
    "VoiceError",
    "available",
    "default_provider",
    "get_provider",
    "handle_transcript",
    "parse_command",
]
