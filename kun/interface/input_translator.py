"""Input translator for real-world content entering KUN.

This module intentionally keeps heavyweight detection lazy. Magika is imported
only when binary detection needs it, and all content understanding stays local
and deterministic until Claude wires the expensive LLM path.
"""

from __future__ import annotations

import csv
import io
import json
import re
import struct
import zipfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import anyio
from pydantic import BaseModel, Field, field_validator

InputKind = Literal[
    "plain_text",
    "json",
    "yaml",
    "markdown",
    "html",
    "xml",
    "sql",
    "code",
    "pdf",
    "csv",
    "xlsx",
    "image_jpg",
    "image_png",
    "image_webp",
    "audio_mp3",
    "audio_wav",
    "video_mp4",
    "archive_zip",
    "archive_tar",
    "executable",
    "binary_unknown",
]

_TEXT_KINDS: set[InputKind] = {
    "plain_text",
    "json",
    "yaml",
    "markdown",
    "html",
    "xml",
    "sql",
    "code",
}

_MAGIKA_LABELS: dict[str, InputKind] = {
    "pdf": "pdf",
    "csv": "csv",
    "xlsx": "xlsx",
    "xls": "xlsx",
    "png": "image_png",
    "jpg": "image_jpg",
    "jpeg": "image_jpg",
    "webp": "image_webp",
    "mp3": "audio_mp3",
    "wav": "audio_wav",
    "mp4": "video_mp4",
    "zip": "archive_zip",
    "tar": "archive_tar",
    "elf": "executable",
    "exe": "executable",
    "pebin": "executable",
}

_MIME_TYPES: dict[InputKind, str] = {
    "plain_text": "text/plain",
    "json": "application/json",
    "yaml": "application/x-yaml",
    "markdown": "text/markdown",
    "html": "text/html",
    "xml": "application/xml",
    "sql": "application/sql",
    "code": "text/x-code",
    "pdf": "application/pdf",
    "csv": "text/csv",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "image_jpg": "image/jpeg",
    "image_png": "image/png",
    "image_webp": "image/webp",
    "audio_mp3": "audio/mpeg",
    "audio_wav": "audio/wav",
    "video_mp4": "video/mp4",
    "archive_zip": "application/zip",
    "archive_tar": "application/x-tar",
    "executable": "application/octet-stream",
    "binary_unknown": "application/octet-stream",
}


class InputDescriptor(BaseModel):
    """Detected input type plus routing hints for the first KUN pipeline hop."""

    kind: InputKind
    mime_type: str
    confidence: float
    suggested_handler: str
    content_summary: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    detected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp_confidence(cls, value: Any) -> float:
        try:
            confidence = float(value)
        except (TypeError, ValueError):
            confidence = 0.0
        return min(1.0, max(0.0, confidence))


class RealWorldTranslator:
    """Detect input kind and recommend the next handler.

    # TODO: chat_handler / WS binary frame wire by Claude in V2.2
    # User upload -> ws.binary frame -> translator.detect -> route by suggested_handler.
    """

    def __init__(self) -> None:
        self._magika: Any | None = None
        self._magika_unavailable = False
        self._extractor = ContentExtractor()

    async def detect(self, raw: bytes | str | Path) -> InputDescriptor:
        """Detect text streams, filesystem paths, or raw bytes."""
        if isinstance(raw, Path):
            return await self.detect_file_kind(await anyio.Path(raw).read_bytes())
        if isinstance(raw, str):
            return await self.detect_text_kind(raw)
        return await self.detect_file_kind(raw)

    async def detect_text_kind(self, text: str) -> InputDescriptor:
        """Classify text with cheap deterministic rules."""
        stripped = text.strip()
        if not stripped:
            return self._descriptor("plain_text", 0.6, metadata={"detector": "empty_text"})

        if _looks_json(stripped):
            return self._descriptor("json", 0.97, metadata={"detector": "json_parse"})
        if _looks_html(stripped):
            return self._descriptor("html", 0.92, metadata={"detector": "html_rule"})
        if _looks_xml(stripped):
            return self._descriptor("xml", 0.9, metadata={"detector": "xml_rule"})
        if _looks_sql(stripped):
            return self._descriptor("sql", 0.88, metadata={"detector": "sql_rule"})
        if _looks_markdown(stripped):
            return self._descriptor("markdown", 0.86, metadata={"detector": "markdown_rule"})
        if _looks_code(stripped):
            return self._descriptor("code", 0.82, metadata={"detector": "code_rule"})
        if _looks_yaml(stripped):
            return self._descriptor("yaml", 0.78, metadata={"detector": "yaml_rule"})
        return self._descriptor("plain_text", 0.72, metadata={"detector": "plain_text_rule"})

    async def detect_file_kind(self, raw: bytes) -> InputDescriptor:
        """Detect binary/file content, using Magika when available and fallback signatures always."""
        fallback = _detect_by_signature(raw)
        magika_descriptor = self._detect_with_magika(raw)
        if magika_descriptor is not None:
            if (
                fallback.kind != "binary_unknown"
                and fallback.confidence >= magika_descriptor.confidence
            ):
                return fallback
            return magika_descriptor
        return fallback

    def suggest_handler(self, kind: InputKind, content: bytes | str = b"") -> str:
        """Recommend the first processing stage for a detected kind."""
        if kind.startswith("image_"):
            return "vision_llm"
        if kind == "pdf":
            return "pdf_extract"
        if kind in ("csv", "xlsx"):
            return "csv_query"
        if kind.startswith("audio_") or kind.startswith("video_"):
            return "transcribe"
        if kind in ("executable", "binary_unknown"):
            return "ask_user"
        if kind in _TEXT_KINDS:
            return "direct_llm"
        return "ask_user"

    async def detect_anchor_then_expand(
        self,
        raw: bytes | str | Path,
    ) -> AsyncIterator[InputDescriptor]:
        """Yield up to three rounds: fast detect, summary, deep placeholder."""
        content = _coerce_to_bytes(raw)
        descriptor = await self.detect(raw)
        descriptor.metadata["anchor_round"] = 1
        yield descriptor

        summary = await self._extractor.extract_summary(descriptor, content)
        round2 = descriptor.model_copy(
            update={
                "content_summary": summary,
                "metadata": {**descriptor.metadata, "anchor_round": 2, "expanded": "summary"},
            }
        )
        yield round2

        round3 = round2.model_copy(
            update={
                "metadata": {
                    **round2.metadata,
                    "anchor_round": 3,
                    "deep_understanding": "placeholder_no_llm_called",
                }
            }
        )
        yield round3

    def _descriptor(
        self,
        kind: InputKind,
        confidence: float,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> InputDescriptor:
        return InputDescriptor(
            kind=kind,
            mime_type=_MIME_TYPES[kind],
            confidence=confidence,
            suggested_handler=self.suggest_handler(kind),
            metadata=metadata or {},
        )

    def _detect_with_magika(self, raw: bytes) -> InputDescriptor | None:
        magika = self._load_magika()
        if magika is None:
            return None
        try:
            result = magika.identify_bytes(raw)
        except Exception:
            return None

        label = _magika_label(result)
        kind = _MAGIKA_LABELS.get(label)
        if kind is None:
            return None
        confidence = _magika_confidence(result)
        return self._descriptor(
            kind,
            confidence,
            metadata={"detector": "magika", "magika_label": label},
        )

    def _load_magika(self) -> Any | None:
        if self._magika_unavailable:
            return None
        if self._magika is not None:
            return self._magika
        try:
            from magika import Magika
        except Exception:
            self._magika_unavailable = True
            return None
        try:
            self._magika = Magika()
        except Exception:
            self._magika_unavailable = True
            return None
        return self._magika


class ContentExtractor:
    """Extract small deterministic summaries for detected content."""

    async def extract_summary(self, descriptor: InputDescriptor, raw: bytes) -> str:
        kind = descriptor.kind
        if kind in _TEXT_KINDS:
            return _decode_text(raw)[:500]
        if kind == "csv":
            return _summarize_csv(raw)
        if kind.startswith("image_"):
            return _summarize_image(kind, raw)
        if kind == "pdf":
            return _summarize_pdf(raw)
        if kind.startswith("audio_"):
            return _summarize_audio(kind, raw)
        if kind.startswith("video_"):
            return _summarize_video(kind, raw)
        return ""


def _descriptor(kind: InputKind, confidence: float, detector: str) -> InputDescriptor:
    translator = RealWorldTranslator()
    return translator._descriptor(kind, confidence, metadata={"detector": detector})


def _detect_by_signature(raw: bytes) -> InputDescriptor:
    if raw.startswith(b"%PDF-"):
        return _descriptor("pdf", 0.99, "signature")
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return _descriptor("image_png", 0.99, "signature")
    if raw.startswith(b"\xff\xd8\xff"):
        return _descriptor("image_jpg", 0.98, "signature")
    if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return _descriptor("image_webp", 0.98, "signature")
    if raw.startswith(b"ID3") or _has_mp3_frame_sync(raw):
        return _descriptor("audio_mp3", 0.94, "signature")
    if raw.startswith(b"RIFF") and raw[8:12] == b"WAVE":
        return _descriptor("audio_wav", 0.98, "signature")
    if len(raw) > 12 and raw[4:8] == b"ftyp":
        return _descriptor("video_mp4", 0.96, "signature")
    if raw.startswith(b"PK\x03\x04"):
        if _zip_contains_xlsx_parts(raw):
            return _descriptor("xlsx", 0.94, "zip_content")
        return _descriptor("archive_zip", 0.98, "signature")
    if _looks_tar(raw):
        return _descriptor("archive_tar", 0.9, "signature")
    if raw.startswith((b"MZ", b"\x7fELF")):
        return _descriptor("executable", 0.96, "signature")
    if _looks_csv_bytes(raw):
        return _descriptor("csv", 0.86, "csv_rule")
    if _looks_text_bytes(raw):
        text = _decode_text(raw)
        return _text_descriptor_sync(text)
    return _descriptor("binary_unknown", 0.35, "fallback")


def _text_descriptor_sync(text: str) -> InputDescriptor:
    stripped = text.strip()
    if _looks_json(stripped):
        return _descriptor("json", 0.96, "json_parse")
    if _looks_sql(stripped):
        return _descriptor("sql", 0.86, "sql_rule")
    if _looks_markdown(stripped):
        return _descriptor("markdown", 0.84, "markdown_rule")
    if _looks_code(stripped):
        return _descriptor("code", 0.8, "code_rule")
    return _descriptor("plain_text", 0.68, "text_bytes")


def _looks_json(text: str) -> bool:
    if not text.startswith(("{", "[")):
        return False
    try:
        json.loads(text)
    except json.JSONDecodeError:
        return False
    return True


def _looks_html(text: str) -> bool:
    return bool(re.search(r"(?is)^\s*<!doctype\s+html|<html[\s>]|<body[\s>]|<div[\s>]", text))


def _looks_xml(text: str) -> bool:
    return bool(re.search(r"(?is)^\s*<\?xml\b|^\s*<[A-Za-z_][\w:.-]*(\s[^>]*)?>.*</", text))


def _looks_sql(text: str) -> bool:
    return bool(
        re.search(
            r"(?is)^\s*(select|insert|update|delete|with|create|alter|drop)\b.*\b(from|into|table|set|values)\b",
            text,
        )
    )


def _looks_markdown(text: str) -> bool:
    return bool(
        re.search(
            r"(?m)^\s{0,3}#{1,6}\s+\S|^\s*[-*]\s+\S|```|!\[[^\]]*]\(|\[[^\]]+]\([^)]+\)",
            text,
        )
    )


def _looks_code(text: str) -> bool:
    return bool(
        re.search(
            r"(?m)^\s*(def|class|async def|function|const|let|var|import|from|package|public|private)\b|[{};]\s*$",
            text,
        )
    )


def _looks_yaml(text: str) -> bool:
    if not re.search(r"(?m)^\s*[\w.-]+\s*:\s*\S", text):
        return False
    try:
        import yaml

        parsed = yaml.safe_load(text)
    except Exception:
        return False
    return isinstance(parsed, dict)


def _looks_text_bytes(raw: bytes) -> bool:
    if not raw:
        return True
    sample = raw[:2048]
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return False
    control = sum(1 for byte in sample if byte < 32 and byte not in (9, 10, 13))
    return control / max(1, len(sample)) < 0.05


def _looks_csv_bytes(raw: bytes) -> bool:
    if not _looks_text_bytes(raw):
        return False
    text = _decode_text(raw)
    try:
        dialect = csv.Sniffer().sniff(text[:2048])
        rows = list(csv.reader(io.StringIO(text), dialect))
    except csv.Error:
        return False
    return len(rows) >= 2 and len(rows[0]) >= 2


def _decode_text(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _summarize_csv(raw: bytes) -> str:
    text = _decode_text(raw)
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return ""
    preview = rows[:6]
    header = ", ".join(preview[0])
    lines = [f"columns: {header}", f"preview_rows: {max(0, len(preview) - 1)}"]
    lines.extend(",".join(row) for row in preview[1:])
    return "\n".join(lines)[:500]


def _summarize_image(kind: InputKind, raw: bytes) -> str:
    dimensions = _image_dimensions(kind, raw)
    if dimensions is None:
        return f"{kind} image; dimensions unknown"
    width, height = dimensions
    return f"{kind} image; dimensions: {width}x{height}"


def _summarize_pdf(raw: bytes) -> str:
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(raw))
        if not reader.pages:
            return "PDF document; pages: 0"
        text = (reader.pages[0].extract_text() or "").strip()
        if text:
            return text[:500]
        return f"PDF document; pages: {len(reader.pages)}"
    except Exception:
        return "PDF document; text extraction unavailable"


def _summarize_audio(kind: InputKind, raw: bytes) -> str:
    if kind == "audio_mp3":
        return f"audio_mp3; bytes: {len(raw)}; duration unavailable"
    if kind == "audio_wav" and len(raw) >= 44:
        channels = int.from_bytes(raw[22:24], "little", signed=False)
        sample_rate = int.from_bytes(raw[24:28], "little", signed=False)
        data_size = int.from_bytes(raw[40:44], "little", signed=False)
        bits_per_sample = int.from_bytes(raw[34:36], "little", signed=False)
        bytes_per_second = sample_rate * channels * max(1, bits_per_sample // 8)
        duration = data_size / bytes_per_second if bytes_per_second else 0.0
        return f"audio_wav; duration: {duration:.2f}s; sample_rate: {sample_rate}Hz"
    return f"{kind}; bytes: {len(raw)}; metadata unavailable"


def _summarize_video(kind: InputKind, raw: bytes) -> str:
    return f"{kind}; bytes: {len(raw)}; metadata unavailable"


def _image_dimensions(kind: InputKind, raw: bytes) -> tuple[int, int] | None:
    if kind == "image_png" and len(raw) >= 24 and raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return struct.unpack(">II", raw[16:24])
    if kind == "image_jpg":
        return _jpeg_dimensions(raw)
    if kind == "image_webp" and len(raw) >= 30 and raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return _webp_dimensions(raw)
    return None


def _jpeg_dimensions(raw: bytes) -> tuple[int, int] | None:
    index = 2
    while index + 9 < len(raw):
        if raw[index] != 0xFF:
            index += 1
            continue
        marker = raw[index + 1]
        index += 2
        if marker in (0xD8, 0xD9):
            continue
        if index + 2 > len(raw):
            return None
        segment_length = int.from_bytes(raw[index : index + 2], "big")
        if marker in range(0xC0, 0xC4):
            height = int.from_bytes(raw[index + 3 : index + 5], "big")
            width = int.from_bytes(raw[index + 5 : index + 7], "big")
            return width, height
        index += segment_length
    return None


def _webp_dimensions(raw: bytes) -> tuple[int, int] | None:
    chunk = raw[12:16]
    if chunk == b"VP8X" and len(raw) >= 30:
        width = int.from_bytes(raw[24:27], "little") + 1
        height = int.from_bytes(raw[27:30], "little") + 1
        return width, height
    return None


def _has_mp3_frame_sync(raw: bytes) -> bool:
    return len(raw) >= 2 and raw[0] == 0xFF and raw[1] & 0xE0 == 0xE0


def _zip_contains_xlsx_parts(raw: bytes) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = set(archive.namelist())
    except zipfile.BadZipFile:
        return False
    return "[Content_Types].xml" in names and any(name.startswith("xl/") for name in names)


def _looks_tar(raw: bytes) -> bool:
    if len(raw) < 265:
        return False
    return raw[257:262] == b"ustar"


def _magika_label(result: Any) -> str:
    output = getattr(result, "output", result)
    for attr in ("label", "group", "ct_label"):
        value = getattr(output, attr, None)
        if isinstance(value, str):
            return value.lower()
    if isinstance(output, dict):
        for key in ("ct_label", "label", "group"):
            value = output.get(key)
            if isinstance(value, str):
                return value.lower()
    return ""


def _magika_confidence(result: Any) -> float:
    output = getattr(result, "output", result)
    for attr in ("score", "confidence"):
        value = getattr(output, attr, None)
        if isinstance(value, int | float):
            return min(1.0, max(0.0, float(value)))
    if isinstance(output, dict):
        for key in ("score", "confidence"):
            value = output.get(key)
            if isinstance(value, int | float):
                return min(1.0, max(0.0, float(value)))
    return 0.85


def _coerce_to_bytes(raw: bytes | str | Path) -> bytes:
    if isinstance(raw, bytes):
        return raw
    if isinstance(raw, Path):
        return raw.read_bytes()
    return raw.encode()


__all__ = ["ContentExtractor", "InputDescriptor", "InputKind", "RealWorldTranslator"]
