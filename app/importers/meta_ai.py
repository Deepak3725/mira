"""Meta AI importer: honestly unsupported until a machine-readable
export format is available.

Research (2026-10-31) found no verified Meta AI conversation-export
format: the apps export chats as PDF (not machine-readable), and
"Download Your Information" is a broad account archive whose
conversation payload is unverified. This importer therefore never
claims a file; it exists so the registry can report Meta AI by name
with guidance instead of a generic error.
"""

from __future__ import annotations

from ..memories import MemoryError
from .base import Importer, _Ctx
from .types import ImportResult


class MetaAIImporter(Importer):
    name = "meta_ai"
    label = "Meta AI"
    route_hint = ("No machine-readable Meta AI export is available; "
                  "PDF exports cannot be imported.")
    filenames = ()

    def detect(self, data, filename: str) -> bool:
        return False

    def parse_item(self, item, filename: str, ctx: _Ctx) -> None:
        raise MemoryError(
            "Meta AI exports aren't supported yet: Meta offers no "
            "machine-readable conversation export (PDF only). If Meta "
            "adds a JSON export, upload that file here.")

    def parse_data(self, data, filename: str) -> ImportResult:
        raise MemoryError(
            "Meta AI exports aren't supported yet: Meta offers no "
            "machine-readable conversation export (PDF only). If Meta "
            "adds a JSON export, upload that file here.")
