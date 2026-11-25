# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Fixtures that build a small ADOT payload with build.sh and run Python processes against it the way the operator does.

The payload is fake where it does not matter (a stand-in ``initialize()``, controlled by FAKE_INITIALIZE) and real where
it does: the bootstrap and the shim are the files build.sh installs, and the dependency-conflict check reads real
``.dist-info`` metadata.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

TOOL_DIRECTORY = Path(__file__).resolve().parent.parent
REPOSITORY_ROOT = TOOL_DIRECTORY.parent.parent
BUILD_SCRIPT = TOOL_DIRECTORY / "build.sh"
AUTO_INSTRUMENTATION = Path("opentelemetry", "instrumentation", "auto_instrumentation")

# Stands in for upstream's initialize(): records the call, and fails when FAKE_INITIALIZE=raise, the way upstream fails
# (logged and swallowed by default, raised with swallow_exceptions=False).
FAKE_INITIALIZE = """
import os


def initialize(*, swallow_exceptions=True):
    os.environ["FAKE_INITIALIZE_CALLED"] = "1"
    if os.environ.get("FAKE_INITIALIZE") == "raise":
        if swallow_exceptions:
            return
        raise RuntimeError("fake initialization failure")
"""

# Printed by every process the tests start, after sitecustomize has run.
STATE_SCRIPT = """
import json, os, sys
print("STATE " + json.dumps({
    "sys_path": sys.path,
    "pythonpath": os.environ.get("PYTHONPATH"),
    "prefix": os.environ.get("PYTHON_AUTO_INSTRUMENTATION_AGENT_PATH_PREFIX"),
    "initialized": os.environ.get("FAKE_INITIALIZE_CALLED") == "1",
    "exporters": {s: os.environ.get("OTEL_%s_EXPORTER" % s) for s in ("TRACES", "METRICS", "LOGS")},
}))
"""

PAYLOAD_DISTRIBUTIONS = {
    "opentelemetry_api-1.45.2": ("opentelemetry-api", "1.45.2"),
    "opentelemetry_sdk-1.45.2": ("opentelemetry-sdk", "1.45.2"),
    "aws_opentelemetry_distro-0.23.0": ("aws-opentelemetry-distro", "0.23.0"),
    "urllib3-2.8.0": ("urllib3", "2.8.0"),
}


def write_distribution(directory, dist_info_stem, name, version):
    dist_info = Path(directory, dist_info_stem + ".dist-info")
    dist_info.mkdir(parents=True, exist_ok=True)
    (dist_info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n\n")


def build_payload(root, tool_directory=TOOL_DIRECTORY):
    """Create a fake installed payload in ``root``, run build.sh on it, and return the completed process."""
    auto_instrumentation = root / AUTO_INSTRUMENTATION
    auto_instrumentation.mkdir(parents=True)
    # Regular packages, so that the payload's opentelemetry package is used rather than merged with the test
    # environment's.
    (root / "opentelemetry" / "__init__.py").write_text("")
    (root / "opentelemetry" / "instrumentation" / "__init__.py").write_text("")
    (auto_instrumentation / "__init__.py").write_text(FAKE_INITIALIZE)
    (auto_instrumentation / "sitecustomize.py").write_text(
        "raise AssertionError('upstream sitecustomize must be replaced')\n"
    )
    for stem, (name, version) in PAYLOAD_DISTRIBUTIONS.items():
        write_distribution(root, stem, name, version)
    return subprocess.run(
        ["bash", str(tool_directory / "build.sh"), str(root)], capture_output=True, text=True, check=False
    )


def supported_minors():
    text = (TOOL_DIRECTORY / "UPSTREAM").read_text()
    return re.search(r'^SUPPORTED_PYTHON_MINORS="([0-9 ]+)"$', text, re.MULTILINE).group(1).split()


@pytest.fixture(name="payload")
def fixture_payload(tmp_path):
    if sys.version_info[0] != 3 or str(sys.version_info[1]) not in supported_minors():
        supported = ", 3.".join(supported_minors())
        pytest.fail(
            f"run these tests on a Python the payload supports (3.{supported}); "
            f"the bootstrap skips {sys.version_info[0]}.{sys.version_info[1]} by design"
        )
    root = tmp_path / "otel-auto-instrumentation-python"
    result = build_payload(root)
    assert result.returncode == 0, result.stderr
    return root


def operator_pythonpath(payload, *customer_entries):
    """PYTHONPATH as the operator sets it: the auto_instrumentation directory, the customer's entries, the payload."""
    return os.pathsep.join([str(payload / AUTO_INSTRUMENTATION), *map(str, customer_entries), str(payload)])


def run_python(pythonpath, extra_env=None, script=STATE_SCRIPT, python=sys.executable, args=(), cwd=None):
    """Start a Python process the way the operator would and return (state, stderr)."""
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": pythonpath, "PYTHONDONTWRITEBYTECODE": "1"}
    env.update(extra_env or {})
    result = subprocess.run(
        [python, "-c", script, *args], capture_output=True, text=True, env=env, cwd=cwd, check=False
    )
    assert result.returncode == 0, result.stderr
    states = [line[len("STATE ") :] for line in result.stdout.splitlines() if line.startswith("STATE ")]
    assert len(states) == 1, result.stdout
    return json.loads(states[0]), result.stderr


def read_records(payload):
    """The skip record lines, per container name: {name: [{field: value}]}."""
    results = payload / ".results"
    if not results.is_dir():
        return {}
    return {path.name: [parse_record(line) for line in path.read_text().splitlines()] for path in results.iterdir()}


def parse_record(line):
    fields = {}
    for match in re.finditer(r'([a-z_]+)=("(?:[^"\\]|\\.)*"|\S*)', line):
        value = match.group(2)
        if value.startswith('"'):
            value = re.sub(r"\\(.)", r"\1", value[1:-1])
        fields[match.group(1)] = value
    return fields


@pytest.fixture(name="real_payload", scope="session")
def fixture_real_payload(tmp_path_factory):
    """The payload the image ships, built the way the Dockerfile builds it: the ADOT distro from this repository and
    its dependencies, installed with pip --target, then build.sh. Built once per session; it needs PyPI."""
    if sys.version_info[0] != 3 or str(sys.version_info[1]) not in supported_minors():
        pytest.fail("run these tests on a Python the payload supports")
    work = tmp_path_factory.mktemp("real-payload")
    distro = work / "aws-opentelemetry-distro"
    shutil.copytree(REPOSITORY_ROOT / "aws-opentelemetry-distro", distro, ignore=shutil.ignore_patterns("tests"))
    pyproject = distro / "pyproject.toml"
    lines = pyproject.read_text().splitlines(keepends=True)
    pyproject.write_text("".join(line for line in lines if "opentelemetry-exporter-otlp-proto-grpc" not in line))
    root = work / "otel-auto-instrumentation-python"
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "--target", str(root), str(distro)],
        check=True, capture_output=True, text=True,
    )  # fmt: skip
    result = subprocess.run(["bash", str(BUILD_SCRIPT), str(root)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return root


_RECEIVER_ANSWERS = {
    "/GetSamplingRules": b'{"SamplingRuleRecords": []}',
    "/SamplingTargets": b'{"SamplingTargetDocuments": [], "UnprocessedStatistics": [], "LastRuleModification": 0}',
}


class _Receiver(BaseHTTPRequestHandler):
    def log_message(self, *args):  # pylint: disable=arguments-differ
        pass

    def _answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self.server.requests.append(self.path)
        self.send_response(200)
        self.end_headers()
        # Empty but valid answers for the X-Ray sampler's polls.
        self.wfile.write(_RECEIVER_ANSWERS.get(self.path, b"{}"))

    do_GET = do_POST = _answer


@pytest.fixture(name="receiver")
def fixture_receiver():
    """A local HTTP endpoint for the agent's OTLP and sampling URLs; records the path of every request.

    A real listener rather than a mock: the exporters run in the process under test, which nothing in this process can
    intercept. Bound to 127.0.0.1 on a free port."""
    server = HTTPServer(("127.0.0.1", 0), _Receiver)
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
