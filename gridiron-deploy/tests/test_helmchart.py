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
MCP_DOCKERFILE = REPO_ROOT / "Dockerfile.mcp"

EXPECTED_IMAGE_TAG = "26.8.0-cuda12.9-py3.12"
EXPECTED_IMAGE_REPO = "nvidia/cuopt"
EXPECTED_IMAGE = f"{EXPECTED_IMAGE_REPO}:{EXPECTED_IMAGE_TAG}"
GPU_RESOURCE = "nvidia.com/gpu"

# The estate's brain (langgraph-agents) wires the `gpu_solver` server to
# http://cuopt-mcp:8090. Both halves of that URL are a contract, not a preference.
MCP_SERVICE_NAME = "cuopt-mcp"
MCP_PORT = 8090
# The shared docker network the estate's compose stacks join.
SHARED_NETWORK = "erp_shared_network"

# The Kubernetes manifest templates that must ship with the chart.
REQUIRED_TEMPLATES = [
    "deployment.yaml",
    "service.yaml",
    "ingress.yaml",
    "serviceaccount.yaml",
    "mcp-deployment.yaml",
    "mcp-service.yaml",
]


def _load_yaml(path: Path):
    assert path.is_file(), f"expected file to exist: {path}"
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _env_map(service: dict) -> dict:
    """compose `environment:` as a dict, accepting either supported form."""
    env = service.get("environment") or {}
    if isinstance(env, dict):
        return {str(k): "" if v is None else str(v) for k, v in env.items()}
    out = {}
    for item in env:
        key, _, value = str(item).partition("=")
        out[key] = value
    return out


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


# ---------------------------------------------------------------------------
# The Contract-A MCP sidecar — compose, chart and image
# ---------------------------------------------------------------------------
class TestMcpSidecar:
    """The sidecar is only useful if it is reachable at the URL the brain holds.

    Every assertion here is about that one sentence: the DNS name, the port, the
    network the name resolves on, and the address of the solver behind it.
    """

    def _compose(self):
        return _load_yaml(COMPOSE_FILE)

    def _values(self):
        return _load_yaml(CHART_DIR / "values.yaml")

    # -- compose ----------------------------------------------------------- #
    def test_mcp_service_exists_with_contract_name(self):
        compose = self._compose()
        assert MCP_SERVICE_NAME in compose["services"], (
            f"compose must define a '{MCP_SERVICE_NAME}' service — langgraph-agents "
            f"resolves that hostname"
        )
        service = compose["services"][MCP_SERVICE_NAME]
        # container_name is what actually decides the DNS name on the shared network.
        assert service.get("container_name") == MCP_SERVICE_NAME

    def test_mcp_port_mapping(self):
        service = self._compose()["services"][MCP_SERVICE_NAME]
        mapping = f"{MCP_PORT}:{MCP_PORT}"
        assert any(str(p) == mapping for p in service["ports"]), (
            f"expected {mapping}, got {service['ports']}"
        )

    def test_mcp_image_is_pinned(self):
        service = self._compose()["services"][MCP_SERVICE_NAME]
        assert "latest" not in str(service["image"])
        assert ":" in str(service["image"]), "image must carry an explicit tag"

    def test_mcp_builds_from_repo_root_context(self):
        # The image needs the `gridiron/` package, so the context is the repo root.
        build = self._compose()["services"][MCP_SERVICE_NAME]["build"]
        assert build["context"] == ".."
        assert build["dockerfile"] == "gridiron-deploy/Dockerfile.mcp"

    def test_both_services_join_the_shared_network(self):
        """Declaring `networks:` on one service only would drop it off the default
        network — and then the sidecar could not resolve `cuopt` at all."""
        compose = self._compose()
        assert compose.get("networks", {}).get(SHARED_NETWORK) == {"external": True}
        for name in ("cuopt", MCP_SERVICE_NAME):
            assert SHARED_NETWORK in (compose["services"][name].get("networks") or []), (
                f"service {name!r} must join {SHARED_NETWORK}"
            )

    def test_mcp_points_at_the_solver_by_service_name(self):
        env = _env_map(self._compose()["services"][MCP_SERVICE_NAME])
        # The client default is http://localhost:5000, which is wrong in a container.
        assert env.get("CUOPT_BASE_URL") == "http://cuopt:5000"
        assert "localhost" not in env["CUOPT_BASE_URL"]

    def test_mcp_token_is_required_at_up_time(self):
        env = _env_map(self._compose()["services"][MCP_SERVICE_NAME])
        token = env.get("CUOPT_MCP_TOKEN", "")
        # `:?` makes `docker compose up` fail rather than booting a surface that
        # fail-closes 503 on every call while looking alive.
        assert ":?" in token, f"CUOPT_MCP_TOKEN must be required, got {token!r}"
        # A real token must never be committed here.
        assert token.startswith("${")

    def test_mcp_never_ships_the_insecure_escape_hatch(self):
        env = _env_map(self._compose()["services"][MCP_SERVICE_NAME])
        assert "CUOPT_MCP_ALLOW_INSECURE" not in env, (
            "that env var reopens the tool surface; it is local-dev only"
        )

    def test_mcp_self_heal_stream_matches_the_incident_module(self):
        env = _env_map(self._compose()["services"][MCP_SERVICE_NAME])
        assert env.get("OTEL_SERVICE_NAME") == "cuopt"

    def test_mcp_healthcheck_probes_the_unauthenticated_route(self):
        service = self._compose()["services"][MCP_SERVICE_NAME]
        test = " ".join(str(x) for x in service["healthcheck"]["test"])
        assert "HEAD" in test, "only HEAD / is unauthenticated"
        assert "/tools" not in test, "GET /tools answers 401/503 and would flap forever"
        assert str(MCP_PORT) in test

    def test_mcp_waits_for_a_healthy_solver(self):
        depends = self._compose()["services"][MCP_SERVICE_NAME]["depends_on"]
        assert depends["cuopt"]["condition"] == "service_healthy"

    def test_mcp_requests_no_gpu(self):
        """The separate container exists precisely so it does not hold a GPU."""
        service = self._compose()["services"][MCP_SERVICE_NAME]
        assert "runtime" not in service
        assert "deploy" not in service

    # -- Dockerfile -------------------------------------------------------- #
    def test_dockerfile_exists_and_is_pinned(self):
        assert MCP_DOCKERFILE.is_file(), f"missing {MCP_DOCKERFILE}"
        text = MCP_DOCKERFILE.read_text(encoding="utf-8")
        from_lines = [ln for ln in text.splitlines() if ln.strip().startswith("FROM ")]
        assert from_lines, "Dockerfile must declare a base image"
        for line in from_lines:
            assert ":" in line, f"base image must carry an explicit tag: {line}"
            assert ":latest" not in line and not line.strip().endswith("latest")

    def test_dockerfile_serves_the_contract_port_and_app(self):
        text = MCP_DOCKERFILE.read_text(encoding="utf-8")
        assert f"EXPOSE {MCP_PORT}" in text
        assert "gridiron.mcp.app:app" in text
        assert f'"{MCP_PORT}"' in text, "CMD must bind the contract port"

    def test_dockerfile_installs_the_observability_deps(self):
        """Without them the self-heal rail this module exists for is dead in the
        sidecar — and app.py degrades silently rather than failing."""
        text = MCP_DOCKERFILE.read_text(encoding="utf-8")
        assert "gridiron/mcp/requirements.txt" in text
        assert "gridiron/observability/requirements.txt" in text

    # -- chart ------------------------------------------------------------- #
    def test_chart_mcp_service_is_addressable_at_the_contract_name(self):
        mcp = self._values()["mcp"]
        # cuopt-server.fullname renders <release>-cuopt-server, which would NOT
        # match the URL the brain holds — hence the override.
        assert mcp["fullnameOverride"] == MCP_SERVICE_NAME
        assert mcp["service"]["port"] == MCP_PORT
        assert mcp["service"]["targetPort"] == MCP_PORT
        assert mcp["service"]["type"] == "ClusterIP"

    def test_chart_mcp_image_is_pinned(self):
        image = self._values()["mcp"]["image"]
        assert image["tag"] and "latest" not in str(image["tag"])

    def test_chart_mcp_requests_no_gpu(self):
        resources = self._values()["mcp"].get("resources") or {}
        rendered = yaml.safe_dump(resources)
        assert GPU_RESOURCE not in rendered, "the sidecar must not reserve a GPU"

    def test_chart_mcp_takes_the_token_by_secret_reference(self):
        """Chart.yaml states no secrets live in this chart."""
        token_secret = self._values()["mcp"]["tokenSecret"]
        assert set(token_secret) >= {"name", "key"}
        assert token_secret["name"] == "", "must reference an existing Secret, not ship one"

    def test_chart_mcp_templates_are_guarded_and_disjoint_from_the_solver(self):
        deployment = (TEMPLATES_DIR / "mcp-deployment.yaml").read_text(encoding="utf-8")
        service = (TEMPLATES_DIR / "mcp-service.yaml").read_text(encoding="utf-8")
        for text in (deployment, service):
            assert "if .Values.mcp.enabled" in text, "both manifests must be toggleable"
            # If the MCP pods carried the solver's selector labels (name+instance),
            # the solver Service would load-balance solve traffic onto them.
            assert "cuopt-server.mcp.selectorLabels" in text
            assert 'include "cuopt-server.selectorLabels"' not in text
        # Comments stripped: the manifest must not *reserve* a GPU, but it is free
        # to say in prose why it does not.
        manifest_only = "\n".join(
            ln for ln in deployment.splitlines() if not ln.strip().startswith("#")
        )
        assert GPU_RESOURCE not in manifest_only
