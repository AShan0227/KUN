"""Output Translator — KUN 输出 → 真实世界格式 (V2.2 §23 后续 + Wire 12).

跟 InputTranslator (kun/interface/input_translator.py) 对偶:
- InputTranslator: 真实世界 → KUN (识别用户上传文件类型 + 推荐 handler)
- OutputTranslator (本模块): KUN → 真实世界 (把内部数据翻译成用户能用的格式)

为什么需要:
- 用户问"做个商业方案", LLM 输出大段 markdown — 用户要 PDF/PPT/邮件草稿
- 用户问"分析 csv", LLM 给文字总结 — 用户要图表 / Excel
- 用户问"发邮件", LLM 写文本 — 真要走 SMTP / Gmail API
- agent 间传数据 — 需要标准 schema (JSON / Protocol Buffer)

设计原则:
- "翻译 + 投递" 二步: translate(payload, target_format) + deliver(payload, target_handler)
- 不重建 — 用 starter_pack skill (markdown_to_pdf / json_validate / etc) 做底层
- 安全: 真投递 (邮件 / API call) 必走 PlanOnlyGate (用户确认)

7 类输出格式:
1. text_plain → 纯文本 (默认)
2. markdown → md (LLM 已经会, 直接透传)
3. pdf → markdown_to_pdf skill (BATCH5 C17 starter_pack)
4. docx → markdown_to_docx skill
5. csv / xlsx → 结构化表格 (从 dict list 生成)
6. json → 结构化 (含 schema 验证)
7. email_draft → 邮件草稿 (subject + body, 不真发)

不在本模块做的 (留给 M5):
- 真发邮件 / 真调外部 API (走 PlanOnlyGate)
- 图表 (matplotlib / plotly)
- 演示文稿 (PPT)
- 实时推送 (Slack / 飞书 webhook)

跟 V2.2 §22 hermes 集成: ExecutionStep 加 action_type "output_translate".
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


OutputFormat = Literal[
    "text_plain",
    "markdown",
    "pdf",
    "docx",
    "csv",
    "xlsx",
    "json",
    "email_draft",
]


class OutputDescriptor(BaseModel):
    """OutputTranslator 翻译结果."""

    format: OutputFormat
    mime_type: str
    payload_text: str = ""  # 文本类内容 (markdown / json string / etc)
    payload_bytes_ref: str = ""  # 文件类内容的引用 (s3:// or local path)
    metadata: dict[str, Any] = Field(default_factory=dict)
    requires_user_approval: bool = False  # 真投递前是否需用户确认 (邮件 / API)
    translated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class EmailDraft(BaseModel):
    """邮件草稿 (真发要走 PlanOnlyGate + 用户确认)."""

    to: list[str]
    cc: list[str] = Field(default_factory=list)
    subject: str
    body: str
    body_format: Literal["plain", "html", "markdown"] = "markdown"


class OutputTranslator:
    """KUN 内部数据 → 真实世界格式翻译器.

    用法:
        translator = OutputTranslator()
        descriptor = await translator.translate(
            payload={"title": "Q4 Plan", "sections": [...]},
            target_format="pdf",
        )
        # 用户拿到 descriptor.payload_bytes_ref (s3 / local path) 下载

    Args:
        workspace_root: 输出文件落盘的根目录 (默认 /tmp/kun_outputs)
    """

    _MIME_MAP: ClassVar[dict[OutputFormat, str]] = {
        "text_plain": "text/plain",
        "markdown": "text/markdown",
        "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "csv": "text/csv",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "json": "application/json",
        "email_draft": "message/rfc822",
    }

    def __init__(self, workspace_root: Path | str | None = None) -> None:
        if workspace_root is None:
            import tempfile

            workspace_root = Path(tempfile.gettempdir()) / "kun_outputs"
        self.workspace_root = Path(workspace_root)
        self.workspace_root.mkdir(parents=True, exist_ok=True)

    async def translate(
        self,
        payload: Any,
        target_format: OutputFormat,
        *,
        filename_hint: str | None = None,
    ) -> OutputDescriptor:
        """主入口. 按 target_format 分发到具体 translator."""
        mime = self._MIME_MAP.get(target_format, "application/octet-stream")

        if target_format == "text_plain":
            return self._text_plain(payload, mime)
        if target_format == "markdown":
            return self._markdown(payload, mime)
        if target_format == "pdf":
            return await self._pdf(payload, mime, filename_hint)
        if target_format == "docx":
            return await self._docx(payload, mime, filename_hint)
        if target_format == "csv":
            return self._csv(payload, mime, filename_hint)
        if target_format == "xlsx":
            return await self._xlsx(payload, mime, filename_hint)
        if target_format == "json":
            return self._json(payload, mime)
        if target_format == "email_draft":
            return self._email_draft(payload, mime)
        raise ValueError(f"unsupported format: {target_format}")

    def _text_plain(self, payload: Any, mime: str) -> OutputDescriptor:
        text = str(payload) if not isinstance(payload, str) else payload
        return OutputDescriptor(format="text_plain", mime_type=mime, payload_text=text)

    def _markdown(self, payload: Any, mime: str) -> OutputDescriptor:
        if isinstance(payload, dict):
            # dict → markdown table-style
            md = self._dict_to_markdown(payload)
        elif isinstance(payload, list):
            md = self._list_to_markdown(payload)
        else:
            md = str(payload)
        return OutputDescriptor(format="markdown", mime_type=mime, payload_text=md)

    async def _pdf(self, payload: Any, mime: str, filename_hint: str | None) -> OutputDescriptor:
        """Markdown → PDF. 简化实装: 把内容写为 .md 文件 + 占位标记 (M5 接 markdown_to_pdf skill)."""
        md_text = self._markdown(payload, "text/markdown").payload_text
        out_path = self._allocate_path(filename_hint or "output", "pdf")
        # 占位: 写 markdown 内容到 .pdf 路径 (真 PDF 转换走 BATCH5 C17 markdown_to_pdf)
        out_path.write_text(
            f"# Placeholder PDF (Markdown source below)\n\n{md_text}", encoding="utf-8"
        )
        return OutputDescriptor(
            format="pdf",
            mime_type=mime,
            payload_bytes_ref=str(out_path),
            metadata={
                "source_markdown_length": len(md_text),
                "real_pdf_pending_skill": "markdown_to_pdf",
            },
        )

    async def _docx(self, payload: Any, mime: str, filename_hint: str | None) -> OutputDescriptor:
        """Markdown → DOCX. 占位 (M5 接 markdown_to_docx skill)."""
        md_text = self._markdown(payload, "text/markdown").payload_text
        out_path = self._allocate_path(filename_hint or "output", "docx")
        out_path.write_text(f"DOCX Placeholder (Markdown source):\n\n{md_text}", encoding="utf-8")
        return OutputDescriptor(
            format="docx",
            mime_type=mime,
            payload_bytes_ref=str(out_path),
            metadata={"source_markdown_length": len(md_text)},
        )

    def _csv(self, payload: Any, mime: str, filename_hint: str | None) -> OutputDescriptor:
        """list[dict] → CSV. 不写文件, 返 text."""
        if not isinstance(payload, list) or not payload:
            return OutputDescriptor(
                format="csv", mime_type=mime, payload_text="", metadata={"rows": 0}
            )
        if not all(isinstance(row, dict) for row in payload):
            raise ValueError("csv payload must be list[dict]")

        # 以第一行 keys 为表头
        headers = list(payload[0].keys())
        lines = [",".join(headers)]
        for row in payload:
            lines.append(
                ",".join(str(row.get(h, "")).replace(",", ";").replace("\n", " ") for h in headers)
            )
        return OutputDescriptor(
            format="csv",
            mime_type=mime,
            payload_text="\n".join(lines),
            metadata={"rows": len(payload), "columns": len(headers)},
        )

    async def _xlsx(self, payload: Any, mime: str, filename_hint: str | None) -> OutputDescriptor:
        """list[dict] → XLSX. 占位 (M5 接 openpyxl)."""
        csv_descriptor = self._csv(payload, "text/csv", filename_hint)
        out_path = self._allocate_path(filename_hint or "output", "xlsx")
        out_path.write_text(
            f"XLSX Placeholder (CSV below):\n\n{csv_descriptor.payload_text}",
            encoding="utf-8",
        )
        return OutputDescriptor(
            format="xlsx",
            mime_type=mime,
            payload_bytes_ref=str(out_path),
            metadata=csv_descriptor.metadata,
        )

    def _json(self, payload: Any, mime: str) -> OutputDescriptor:
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        return OutputDescriptor(format="json", mime_type=mime, payload_text=text)

    def _email_draft(self, payload: Any, mime: str) -> OutputDescriptor:
        """payload 应该是 EmailDraft 或 dict 兼容. 真发要走 PlanOnlyGate."""
        if isinstance(payload, EmailDraft):
            draft = payload
        elif isinstance(payload, dict):
            draft = EmailDraft.model_validate(payload)
        else:
            raise ValueError("email_draft payload must be EmailDraft or dict")
        body_text = (
            f"To: {', '.join(draft.to)}\n"
            f"Cc: {', '.join(draft.cc)}\n"
            f"Subject: {draft.subject}\n\n"
            f"{draft.body}"
        )
        return OutputDescriptor(
            format="email_draft",
            mime_type=mime,
            payload_text=body_text,
            requires_user_approval=True,  # 邮件真发必走 PlanOnlyGate
            metadata={
                "to": draft.to,
                "cc": draft.cc,
                "subject": draft.subject,
                "body_format": draft.body_format,
            },
        )

    def _allocate_path(self, hint: str, ext: str) -> Path:
        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        safe_hint = "".join(c if c.isalnum() or c in "-_" else "_" for c in hint)[:40]
        return self.workspace_root / f"{ts}_{safe_hint}.{ext}"

    def _dict_to_markdown(self, payload: dict[str, Any]) -> str:
        lines = []
        for k, v in payload.items():
            if isinstance(v, list | tuple):
                lines.append(f"### {k}")
                for item in v:
                    lines.append(f"- {item}")
            elif isinstance(v, dict):
                lines.append(f"### {k}")
                for sub_k, sub_v in v.items():
                    lines.append(f"- **{sub_k}**: {sub_v}")
            else:
                lines.append(f"**{k}**: {v}")
            lines.append("")
        return "\n".join(lines).strip()

    def _list_to_markdown(self, payload: list[Any]) -> str:
        lines = []
        for item in payload:
            if isinstance(item, dict):
                lines.append(f"- {self._dict_to_markdown(item).replace(chr(10), ' ')}")
            else:
                lines.append(f"- {item}")
        return "\n".join(lines)


_default_translator: OutputTranslator | None = None


def get_default_translator() -> OutputTranslator:
    global _default_translator
    if _default_translator is None:
        _default_translator = OutputTranslator()
    return _default_translator


def reset_default_translator() -> None:
    global _default_translator
    _default_translator = None


__all__ = [
    "EmailDraft",
    "OutputDescriptor",
    "OutputFormat",
    "OutputTranslator",
    "get_default_translator",
    "reset_default_translator",
]
