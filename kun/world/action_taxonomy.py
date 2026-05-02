"""WorldGateway action taxonomy alignment.

This module only chooses handler-friendly action types. It does not execute or
dispatch anything externally.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_REAL_DISPATCH_CONFIRMATION_FIELDS = (
    "external_dispatch_confirmed",
    "real_dispatch_confirmed",
    "real_world_dispatch_confirmed",
    "dispatch_confirmed",
)

_EMAIL_SOURCES = {
    "email.send",
    "mail.send",
    "message.send",
    "messages.send",
    "sms.send",
    "slack.send",
    "notification.send",
}
_BROWSER_SOURCES = {
    "browser.execute",
    "browser.click",
    "browser.fill",
    "browser.submit",
    "browser.form_submit",
    "web.browser.execute",
}
_WEBHOOK_DRY_RUN_SOURCES = {
    "content.publish",
    "content.post",
    "webhook",
    "webhook.post",
    "http.post",
    "api.post",
    "external_api.post",
}
_ENTERPRISE_API_SOURCES = {
    "enterprise_api.post",
    "enterprise.api.post",
}
_LOCAL_FILE_SOURCES = {
    "file.write",
    "local_file.write",
    "filesystem.write",
    "document.write",
}
_LOW_RISK_REGISTERED_ACTIONS = {
    "email.draft",
    "browser.plan",
    "webhook.post_dry_run",
    "local_file.write",
}


@dataclass(frozen=True)
class TaxonomyResult:
    """A normalized WorldGateway action type plus auditable rationale."""

    action_type: str
    source_action_type: str
    taxonomy_reason: str
    requires_real_dispatch_confirmation: bool = False


def normalize_world_action_type(
    action_type: str,
    payload: dict[str, Any] | None = None,
) -> TaxonomyResult:
    """Map external action names onto registered WorldGateway handlers.

    Conservative defaults prefer non-dispatching handlers. Real external
    handlers are selected only when the payload carries an explicit confirmation.
    """

    source = _normalize_action_type(action_type)
    confirmed = _has_real_dispatch_confirmation(payload)

    if source in _LOW_RISK_REGISTERED_ACTIONS:
        return TaxonomyResult(
            action_type=source,
            source_action_type=source,
            taxonomy_reason="already_registered_low_risk_handler",
            requires_real_dispatch_confirmation=False,
        )

    if source in _EMAIL_SOURCES or source.startswith(("message.", "messages.", "email.")):
        if confirmed:
            return TaxonomyResult(
                action_type="email.send",
                source_action_type=source,
                taxonomy_reason="explicit_dispatch_confirmation_allows_email_send",
                requires_real_dispatch_confirmation=True,
            )
        return TaxonomyResult(
            action_type="email.draft",
            source_action_type=source,
            taxonomy_reason="message_or_email_defaults_to_draft",
            requires_real_dispatch_confirmation=True,
        )

    if source in _BROWSER_SOURCES or source.startswith("browser."):
        if confirmed:
            return TaxonomyResult(
                action_type="browser.execute",
                source_action_type=source,
                taxonomy_reason="explicit_dispatch_confirmation_allows_browser_execute",
                requires_real_dispatch_confirmation=True,
            )
        return TaxonomyResult(
            action_type="browser.plan",
            source_action_type=source,
            taxonomy_reason="browser_action_defaults_to_plan",
            requires_real_dispatch_confirmation=True,
        )

    if source in _ENTERPRISE_API_SOURCES:
        if confirmed:
            return TaxonomyResult(
                action_type="enterprise_api.post",
                source_action_type=source,
                taxonomy_reason="explicit_dispatch_confirmation_allows_enterprise_api_post",
                requires_real_dispatch_confirmation=True,
            )
        return TaxonomyResult(
            action_type="webhook.post_dry_run",
            source_action_type=source,
            taxonomy_reason="enterprise_api_post_defaults_to_webhook_dry_run",
            requires_real_dispatch_confirmation=True,
        )

    if source in _WEBHOOK_DRY_RUN_SOURCES or source.startswith(
        ("webhook.", "content.", "http.", "api.")
    ):
        return TaxonomyResult(
            action_type="webhook.post_dry_run",
            source_action_type=source,
            taxonomy_reason="webhook_or_publish_defaults_to_post_dry_run",
            requires_real_dispatch_confirmation=True,
        )

    if source in _LOCAL_FILE_SOURCES or source.startswith(("file.", "local_file.")):
        return TaxonomyResult(
            action_type="local_file.write",
            source_action_type=source,
            taxonomy_reason="file_write_maps_to_local_file_write",
            requires_real_dispatch_confirmation=False,
        )

    return TaxonomyResult(
        action_type=source,
        source_action_type=source,
        taxonomy_reason="no_taxonomy_mapping_found",
        requires_real_dispatch_confirmation=True,
    )


def apply_taxonomy_audit_fields(
    payload: dict[str, Any],
    result: TaxonomyResult,
) -> dict[str, Any]:
    """Return payload with source and normalization metadata attached."""

    return {
        **payload,
        "source_action_type": result.source_action_type,
        "taxonomy_reason": result.taxonomy_reason,
        "requires_real_dispatch_confirmation": result.requires_real_dispatch_confirmation,
    }


def _normalize_action_type(action_type: str) -> str:
    return action_type.strip().lower().replace("-", "_") or "unknown"


def _has_real_dispatch_confirmation(payload: dict[str, Any] | None) -> bool:
    if not payload:
        return False
    return any(_truthy(payload.get(field)) for field in _REAL_DISPATCH_CONFIRMATION_FIELDS)


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "confirmed"}
    if isinstance(value, int | float):
        return value != 0
    return False


__all__ = [
    "TaxonomyResult",
    "apply_taxonomy_audit_fields",
    "normalize_world_action_type",
]
