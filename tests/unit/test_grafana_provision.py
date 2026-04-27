from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_lab_grafana_dashboard_provider_points_to_mounted_dir() -> None:
    provider_path = ROOT / "kun/infra/grafana-dashboards-provision.yaml"
    data = yaml.safe_load(provider_path.read_text())

    assert data["apiVersion"] == 1
    providers = data["providers"]
    assert len(providers) == 1
    provider = providers[0]
    assert provider["name"] == "kun-lab"
    assert provider["folder"] == "KUN"
    assert provider["options"]["path"] == "/var/lib/grafana/dashboards"


def test_dev_compose_mounts_lab_dashboard_and_provider() -> None:
    compose_path = ROOT / "docker-compose.dev.yml"
    data = yaml.safe_load(compose_path.read_text())
    volumes = data["services"]["grafana"]["volumes"]

    assert (
        "./kun/infra/grafana-dashboards-provision.yaml:"
        "/etc/grafana/provisioning/dashboards/kun-lab.yml:ro"
    ) in volumes
    assert (
        "./kun/infra/grafana-dashboard-kun-lab.json:/var/lib/grafana/dashboards/kun-lab.json:ro"
    ) in volumes
