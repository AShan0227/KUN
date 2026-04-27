"""Shared input translation helpers for chat REST and WebSocket."""

from __future__ import annotations

import base64
import binascii
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, Field

from kun.interface.input_translator import ContentExtractor, InputDescriptor, RealWorldTranslator

MAX_ATTACHMENT_BYTES = 2_000_000


class Attachment(BaseModel):
    """Base64 encoded user attachment for chat entrypoints."""

    filename: str = "attachment"
    content_b64: str
    content_type: str | None = None


class AttachmentTranslation(BaseModel):
    filename: str
    descriptor: InputDescriptor
    extracted_text: str


class TranslatedInput(BaseModel):
    message: str
    attachments: list[AttachmentTranslation] = Field(default_factory=list)

    @property
    def descriptors(self) -> list[dict[str, Any]]:
        return [
            {
                "filename": attachment.filename,
                "kind": attachment.descriptor.kind,
                "mime_type": attachment.descriptor.mime_type,
                "confidence": attachment.descriptor.confidence,
                "suggested_handler": attachment.descriptor.suggested_handler,
                "content_summary": attachment.descriptor.content_summary,
                "metadata": attachment.descriptor.metadata,
            }
            for attachment in self.attachments
        ]


async def translate_chat_input(
    message: str,
    attachments: list[Attachment] | None = None,
    *,
    translator: RealWorldTranslator | None = None,
) -> TranslatedInput:
    """Translate optional attachments and append readable blocks to the user message."""
    translator = translator or RealWorldTranslator()
    translated_attachments: list[AttachmentTranslation] = []
    for attachment in attachments or []:
        translated_attachments.append(await translate_attachment(attachment, translator=translator))

    parts = [message.strip()] if message.strip() else []
    for translated_attachment in translated_attachments:
        parts.append(_attachment_prompt_block(translated_attachment))
    return TranslatedInput(message="\n\n".join(parts), attachments=translated_attachments)


async def translate_binary_input(
    raw: bytes,
    *,
    filename: str = "websocket.bin",
    translator: RealWorldTranslator | None = None,
) -> TranslatedInput:
    """Translate a raw WebSocket binary frame into a normal chat message."""
    encoded = base64.b64encode(raw).decode("ascii")
    return await translate_chat_input(
        "",
        [Attachment(filename=filename, content_b64=encoded)],
        translator=translator,
    )


async def translate_attachment(
    attachment: Attachment,
    *,
    translator: RealWorldTranslator,
) -> AttachmentTranslation:
    raw = _decode_attachment(attachment)
    descriptor = await translator.detect_file_kind(raw)
    descriptor.metadata.update(
        {
            "filename": attachment.filename,
            "declared_content_type": attachment.content_type,
        }
    )
    extracted_text = await _extract_supported_content(descriptor, raw)
    descriptor = descriptor.model_copy(update={"content_summary": extracted_text[:500]})
    return AttachmentTranslation(
        filename=attachment.filename,
        descriptor=descriptor,
        extracted_text=extracted_text,
    )


def _decode_attachment(attachment: Attachment) -> bytes:
    try:
        raw = base64.b64decode(attachment.content_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail="attachment content_b64 is invalid") from exc
    if len(raw) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(status_code=413, detail="attachment is too large")
    return raw


async def _extract_supported_content(descriptor: InputDescriptor, raw: bytes) -> str:
    handler = descriptor.suggested_handler
    if handler == "direct_llm":
        return _decode_text(raw)
    if handler in {"pdf_extract", "csv_query"}:
        summary = await ContentExtractor().extract_summary(descriptor, raw)
        return summary or f"{descriptor.kind} file; no extractable preview"
    if handler == "vision_llm":
        raise HTTPException(status_code=415, detail="image OCR is not wired yet")
    if handler == "transcribe":
        raise HTTPException(status_code=415, detail="audio/video transcription is not wired yet")
    raise HTTPException(status_code=415, detail=f"unsupported attachment kind: {descriptor.kind}")


def _attachment_prompt_block(attachment: AttachmentTranslation) -> str:
    descriptor = attachment.descriptor
    return (
        f"[Attachment: {attachment.filename}]\n"
        f"kind: {descriptor.kind}\n"
        f"mime_type: {descriptor.mime_type}\n"
        f"handler: {descriptor.suggested_handler}\n"
        f"confidence: {descriptor.confidence:.2f}\n"
        f"content:\n{attachment.extracted_text}"
    )


def _decode_text(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


__all__ = [
    "Attachment",
    "AttachmentTranslation",
    "TranslatedInput",
    "translate_attachment",
    "translate_binary_input",
    "translate_chat_input",
]
