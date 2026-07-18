"""
Structural validation for the cuopt-server Helm chart and the local
docker-compose deploy path.

Pure pytest — no helm, no kubernetes, no GPU. Runs fully offline. These tests
assert the *load-bearing* values the estate depends on (image tag, port 5000,
the GPU resource, the solver command) and that every required chart template
file is present. Go-template files are NOT rendered here; only the non-template
YAML (Chart.yaml, values.yaml, docker-compose.cuopt.yml) is parsed.
"""
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

# Repo root is the parent of this tests/ directory.
REPO_ROOT = Path(__file__).resolve().parent.parent
CHART_DIR = REPO_ROOT / "helmchart" / "cuopt-server"
TEMPLATES_DIR = CHART_DIR / "templates"
COMPOSE_FILE = REPO_ROOT / "docker-compose.cuopt.yml"

EXPECTED_IMAGE_TAG = "26.8.0-cuda12.9-py3.12"
EXPECTED_IMAGE_REPO = "nvidia/cuopt"
EXPECTED_IMAGE = f"{EXPECTED_IMAGE_REPO}:{EXPECTED_IMAGE_TAG}"
GPU_RESOURCE = "nvidia.com/gpu"
# The four Kubernetes manifest templates that must ship with the chart.
REQUIRED_TEMPLATES = ["deployment.yaml", "service.yaml", "ingress.yaml", "serviceaccount.yaml"]


def _load_yaml(path: Path):
    assert path.is_file(), f"expected file to exist: {path}"
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# ---------------------------------------------------------------------------
# Chart.yaml
# ---------------------------------------------------------------------------
class TestChartYaml:
    def test_chart_metadata(self):
        chart = _load_yaml(CHART_DIR / "Chart.yaml")
        assert chart["name"] == "cuopt-server"
        assert chart["apiVersion"] == "v2"
        assert chart["type"] == "application"
        # appVersion tracks the cuOpt release; keep it as the string "26.8.0".
        assert str(chart["appVersion"]) == "26.8.0"
        # Chart is independently versioned (SemVer).
        assert "version" in chart and chart["version"]


# ---------------------------------------------------------------------------
# values.yaml
# ---------------------------------------------------------------------------
class TestValuesYaml:
    def _values(self):
        return _load_yaml(CHART_DIR / "values.yaml")

    def test_image(self):
        values = self._values()
        image = values["image"]
        assert image["repository"] == EXPECTED_IMAGE_REPO
        # Exact, immutable tag — never "latest" or a floating tag.
        assert image["tag"] == EXPECTED_IMAGE_TAG
        assert "latest" not in str(image["tag"])

    def test_service_ports(self):
        values = self._values()
        service = values["service"]
        assert service["port"] == 5000
        assert service["targetPort"] == 5000
        assert service["type"] == "ClusterIP"

    def test_gpu_resource_in_requests_and_limits(self):
        values = self._values()
        resources = values["resources"]
        assert GPU_RESOURCE in resources["requests"], "GPU missing from resources.requests"
        assert GPU_RESOURCE in resources["limits"], "GPU missing from resources.limits"
        assert resources["requests"][GPU_RESOURCE] == 1
        assert resources["limits"][GPU_RESOURCE] == 1

    def test_command(self):
        values = self._values()
        command = values["command"]
        assert isinstance(command, list) and command, "command must be a non-empty list"
        command_str = " ".join(str(part) for part in command)
        assert "cuopt_server.cuopt_service" in command_str
        assert "5000" in command_str


# ---------------------------------------------------------------------------
# Chart templates (existence + non-empty; NOT rendered)
# ---------------------------------------------------------------------------
class TestChartTemplates:
    @pytest.mark.parametrize("template_name", REQUIRED_TEMPLATES)
    def test_required_template_present_and_nonempty(self, template_name):
        path = TEMPLATES_DIR / template_name
        assert path.is_file(), f"missing chart template: {path}"
        assert path.stat().st_size > 0, f"chart template is empty: {path}"

    def test_helper_files_present(self):
        # Standard helm boilerplate the manifests depend on.
        for name in ("_helpers.tpl", "NOTES.txt"):
            path = TEMPLATES_DIR / name
            assert path.is_file(), f"missing chart file: {path}"
            assert path.stat().st_size > 0, f"chart file is empty: {path}"


# ---------------------------------------------------------------------------
# docker-compose.cuopt.yml
# ---------------------------------------------------------------------------
class TestDockerComposeCuopt:
    def _compose(self):
        return _load_yaml(COMPOSE_FILE)

    def test_cuopt_service_image(self):
        compose = self._compose()
        assert "cuopt" in compose["services"], "compose must define a 'cuopt' service"
        service = compose["services"]["cuopt"]
        assert service["image"] == EXPECTED_IMAGE
        assert service["image"].endswith(EXPECTED_IMAGE_TAG)
        assert "latest" not in service["image"]

    def test_cuopt_service_port(self):
        compose = self._compose()
        ports = compose["services"]["cuopt"]["ports"]
        # docker-compose short syntax entries are strings like "5000:5000".
        assert any(str(p) == "5000:5000" for p in ports), f"expected 5000:5000 mapping, got {ports}"

    def test_cuopt_service_has_healthcheck(self):
        compose = self._compose()
        service = compose["services"]["cuopt"]
        assert "healthcheck" in service, "cuopt service must define a healthcheck"
        healthcheck = service["healthcheck"]
        assert healthcheck.get("test"), "healthcheck must define a test command"
        # Sanity: the health probe should target the cuOpt health endpoint.
        assert "cuopt/health" in " ".join(str(x) for x in healthcheck["test"])
