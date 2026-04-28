from __future__ import annotations

import json

from kun.cli import app
from kun.qi import reset_protocol_registry
from typer.testing import CliRunner


def test_protocol_cli_save_and_list(tmp_path) -> None:
    reset_protocol_registry()
    payload = {
        "protocol_id": "coding.pytest",
        "version": "0.1.0",
        "tenant_id": "ignored",
        "status": "stable",
        "trigger": {"task_type_pattern": "coding.*"},
    }
    protocol_file = tmp_path / "protocol.json"
    protocol_file.write_text(json.dumps(payload), encoding="utf-8")

    runner = CliRunner()
    saved = runner.invoke(app, ["protocol", "save", str(protocol_file), "--tenant", "t-cli"])
    assert saved.exit_code == 0
    assert "saved" in saved.output

    listed = runner.invoke(app, ["protocol", "list", "--tenant", "t-cli"])
    assert listed.exit_code == 0
    assert "coding.pytest" in listed.output

    reset_protocol_registry()
