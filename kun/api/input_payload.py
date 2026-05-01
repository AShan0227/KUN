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
    compiler_asset_id: str | None = None
    compiler_status: str | None = None
    compiler_kind: str | None = None
    compiler_hermes_packet: str | None = None


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
                "compiler_asset_id": attachment.compiler_asset_id,
                "compiler_status": attachment.compiler_status,
                "compiler_kind": attachment.compiler_kind,
            }
            for attachment in self.attachments
        ]


async def translate_chat_input(
    message: str,
    attachments: list[Attachment] | None = None,
    *,
    tenant_id: str | None = None,
    store_compiled_assets: bool = False,
    translator: RealWorldTranslator | None = None,
) -> TranslatedInput:
    """Translate optional attachments and append readable blocks to the user message."""
    translator = translator or RealWorldTranslator()
    translated_attachments: list[AttachmentTranslation] = []
    for attachment in attachments or []:
        translated_attachments.append(
            await translate_attachment(
                attachment,
                translator=translator,
                tenant_id=tenant_id,
                store_compiled_asset=store_compiled_assets,
            )
        )

    parts = [message.strip()] if message.strip() else []
    for translated_attachment in translated_attachments:
        parts.append(_attachment_prompt_block(translated_attachment))
    return TranslatedInput(message="\n\n".join(parts), attachments=translated_attachments)


async def translate_binary_input(
    raw: bytes,
    *,
    filename: str = "websocket.bin",
    tenant_id: str | None = None,
    store_compiled_assets: bool = False,
    translator: RealWorldTranslator | None = None,
) -> TranslatedInput:
    """Translate a raw WebSocket binary frame into a normal chat message."""
    encoded = base64.b64encode(raw).decode("ascii")
    return await translate_chat_input(
        "",
        [Attachment(filename=filename, content_b64=encoded)],
        tenant_id=tenant_id,
        store_compiled_assets=store_compiled_assets,
        translator=translator,
    )


async def translate_attachment(
    attachment: Attachment,
    *,
    translator: RealWorldTranslator,
    tenant_id: str | None = None,
    store_compiled_asset: bool = False,
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
    compiler_asset_id: str | None = None
    compiler_status: str | None = None
    compiler_kind: str | None = None
    compiler_hermes_packet: str | None = None
    if store_compiled_asset and tenant_id and extracted_text.strip():
        (
            compiler_asset_id,
            compiler_status,
            compiler_kind,
            compiler_hermes_packet,
        ) = await _compile_attachment_to_asset(
            descriptor=descriptor,
            filename=attachment.filename,
            tenant_id=tenant_id,
            raw=raw,
            extracted_text=extracted_text,
        )
        descriptor.metadata.update(
            {
                "compiler_asset_id": compiler_asset_id,
                "compiler_status": compiler_status,
                "compiler_kind": compiler_kind,
            }
        )
    return AttachmentTranslation(
        filename=attachment.filename,
        descriptor=descriptor,
        extracted_text=extracted_text,
        compiler_asset_id=compiler_asset_id,
        compiler_status=compiler_status,
        compiler_kind=compiler_kind,
        compiler_hermes_packet=compiler_hermes_packet,
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
    compiler_lines = ""
    if attachment.compiler_asset_id:
        compiler_lines = (
            f"compiler_asset_id: {attachment.compiler_asset_id}\n"
            f"compiler_status: {attachment.compiler_status or 'unknown'}\n"
            f"compiler_kind: {attachment.compiler_kind or 'unknown'}\n"
        )
    compiler_packet = ""
    if attachment.compiler_hermes_packet:
        compiler_packet = f"\ncompiler_material_packet:\n{attachment.compiler_hermes_packet}\n"
    return (
        f"[Attachment: {attachment.filename}]\n"
        f"kind: {descriptor.kind}\n"
        f"mime_type: {descriptor.mime_type}\n"
        f"handler: {descriptor.suggested_handler}\n"
        f"confidence: {descriptor.confidence:.2f}\n"
        f"{compiler_lines}"
        f"{compiler_packet}"
        f"content:\n{attachment.extracted_text}"
    )


def _decode_text(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


async def _compile_attachment_to_asset(
    *,
    descriptor: InputDescriptor,
    filename: str,
    tenant_id: str,
    raw: bytes,
    extracted_text: str,
) -> tuple[str | None, str, str | None, str | None]:
    """Bridge translated attachments into the compiler/context system.

    This only stores material that the conservative lightweight compiler can
    honestly represent. Heavy backends (OCR, audio, MarkItDown) remain explicit
    future work and are not faked here.
    """

    from kun.compiler import CompilerIngestor
    from kun.interface.hermes import DefaultHermesAdapter

    declared_kind = _compiler_declared_kind(descriptor, filename)
    result = await CompilerIngestor().ingest_bytes(
        raw,
        tenant_id=tenant_id,
        source_uri=f"attachment:{filename}",
        declared_kind=declared_kind,
        mime_type=descriptor.mime_type,
        metadata={
            "source": "chat_attachment",
            "filename": filename,
            "input_kind": descriptor.kind,
            "mime_type": descriptor.mime_type,
            "handler": descriptor.suggested_handler,
            "preview_text": extracted_text[:1000],
            "detector_metadata": descriptor.metadata,
        },
    )
    status = "stored" if result.stored else result.reason
    envelope = await DefaultHermesAdapter().translate_material(
        material=result.material,
        target="llm",
        context={"source": "chat_attachment", "filename": filename},
        max_l2_chars=1400,
    )
    return result.asset_id, status, result.material.kind, envelope.rendered


def _compiler_declared_kind(descriptor: InputDescriptor, filename: str) -> str | None:
    lowered = filename.lower()
    if lowered.endswith((".txt", ".text", ".md", ".markdown", ".html", ".htm", ".json", ".csv")):
        return None
    if descriptor.kind in {"plain_text", "markdown", "html", "json", "csv", "pdf"}:
        return descriptor.kind
    if descriptor.kind in {"yaml", "xml", "sql", "code"}:
        return "plain_text"
    return descriptor.kind


__all__ = [
    "Attachment",
    "AttachmentTranslation",
    "TranslatedInput",
    "translate_attachment",
    "translate_binary_input",
    "translate_chat_input",
]
