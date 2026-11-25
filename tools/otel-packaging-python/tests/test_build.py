# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tests for build.sh: what it installs into a payload, and that it refuses inputs it cannot vouch for."""

import re
import shutil
import subprocess

from conftest import (
    AUTO_INSTRUMENTATION,
    BUILD_SCRIPT,
    TOOL_DIRECTORY,
    build_payload,
    supported_minors,
    write_distribution,
)


def test_installs_the_shim_in_place_of_upstreams_sitecustomize(payload):
    installed = (payload / AUTO_INSTRUMENTATION / "sitecustomize.py").read_text()
    assert installed == (TOOL_DIRECTORY / "adot_sitecustomize.py").read_text()


def test_installs_the_patched_upstream_bootstrap_at_the_payload_root(payload):
    bootstrap = (payload / "_otel_packaging_sitecustomize.py").read_text()
    for patch_file in sorted((TOOL_DIRECTORY / "patches").glob("*.patch")):
        number = patch_file.name.split("-", 1)[0]
        assert f"ADOT patch {number}:" in bootstrap, patch_file.name
    # Nothing named sitecustomize.py at the root: upstream initialize() keeps child processes it starts from
    # re-running auto-instrumentation by removing the auto_instrumentation directory from PYTHONPATH, which only works
    # if no other sitecustomize.py is on it.
    assert not (payload / "sitecustomize.py").exists()


def test_sets_the_python_allowlist_to_the_supported_minors(payload):
    bootstrap = (payload / "_otel_packaging_sitecustomize.py").read_text()
    expected = f"_SUPPORTED_PYTHON_MINORS = ({', '.join(supported_minors())},)  # supported-python-minors"
    assert bootstrap.count("# supported-python-minors") == 1
    assert expected in bootstrap


def test_supported_minors_match_the_distro_classifiers():
    pyproject = (TOOL_DIRECTORY.parent.parent / "aws-opentelemetry-distro" / "pyproject.toml").read_text()
    classifiers = re.findall(r'"Programming Language :: Python :: 3\.([0-9]+)"', pyproject)
    assert sorted(classifiers, key=int) == supported_minors()


def test_lists_only_opentelemetry_distributions_for_every_supported_minor(payload):
    for minor in supported_minors():
        manifest = (payload / ("cp3" + minor) / "all-dependencies.txt").read_text().splitlines()
        requirements = [line for line in manifest if line and not line.startswith("#")]
        # urllib3 is in the payload too, but general-purpose libraries are not checked.
        assert requirements == [
            "aws-opentelemetry-distro==0.23.0",
            "opentelemetry-api==1.45.2",
            "opentelemetry-sdk==1.45.2",
        ]


def test_refuses_a_vendored_file_that_does_not_match_the_pinned_sha256(tmp_path):
    tool_copy = tmp_path / "otel-packaging-python"
    shutil.copytree(TOOL_DIRECTORY, tool_copy, ignore=shutil.ignore_patterns("tests", "__pycache__"))
    with open(tool_copy / "vendor" / "sitecustomize.py", "a", encoding="utf-8") as vendored:
        vendored.write("# modified\n")
    result = build_payload(tmp_path / "payload", tool_directory=tool_copy)
    assert result.returncode != 0
    assert "UPSTREAM pins" in result.stderr
    assert not (tmp_path / "payload" / "_otel_packaging_sitecustomize.py").exists()


def test_refuses_a_directory_that_is_not_an_installed_payload(tmp_path):
    result = subprocess.run(
        ["bash", str(TOOL_DIRECTORY / "build.sh"), str(tmp_path)], capture_output=True, text=True, check=False
    )
    assert result.returncode != 0
    assert "not an installed ADOT Python payload" in result.stderr


def test_installs_the_hooks_with_the_payloads_distro_version(payload):
    hooks = (payload / "_adot_hooks.py").read_text()
    source = (TOOL_DIRECTORY / "adot_hooks.py").read_text()
    assert hooks == source.replace('SDK_VERSION = "unknown"  # sdk-version', 'SDK_VERSION = "0.23.0"  # sdk-version')


def test_refuses_a_payload_without_exactly_one_distro(payload):
    shutil.rmtree(payload / "aws_opentelemetry_distro-0.23.0.dist-info")
    result = subprocess.run(
        ["bash", str(TOOL_DIRECTORY / "build.sh"), str(payload)], capture_output=True, text=True, check=False
    )
    assert result.returncode != 0
    assert "expected exactly one aws_opentelemetry_distro .dist-info, found 0" in result.stderr


# Package grouping (B2): general libraries move to deps/, which the bootstrap puts behind the application's packages.
def test_moves_general_libraries_out_of_the_payload_root(payload):
    assert (payload / "deps" / "urllib3" / "__init__.py").is_file()
    assert (payload / "deps" / "urllib3-2.8.0.dist-info" / "RECORD").is_file()
    assert not (payload / "urllib3").exists()
    assert not (payload / "urllib3-2.8.0.dist-info").exists()
    for stem in ("opentelemetry_api-1.45.2", "opentelemetry_sdk-1.45.2", "aws_opentelemetry_distro-0.23.0"):
        assert (payload / f"{stem}.dist-info").is_dir(), stem


def _payload_with(tmp_path, stem, name, version, files):
    root = tmp_path / "payload"
    (root / AUTO_INSTRUMENTATION).mkdir(parents=True)
    write_distribution(root, "opentelemetry_api-1.45.2", "opentelemetry-api", "1.45.2", {"opentelemetry/api.py": ""})
    write_distribution(root, "aws_opentelemetry_distro-0.23.0", "aws-opentelemetry-distro", "0.23.0")
    write_distribution(root, stem, name, version, files)
    return root


def _build(root):
    return subprocess.run(["bash", str(BUILD_SCRIPT), str(root)], capture_output=True, text=True, check=False)


def test_refuses_a_distribution_without_a_record(tmp_path):
    root = _payload_with(tmp_path, "six-1.17.0", "six", "1.17.0", {"six.py": ""})
    (root / "six-1.17.0.dist-info" / "RECORD").unlink()
    result = _build(root)
    assert result.returncode != 0
    assert "six-1.17.0.dist-info: no RECORD" in result.stderr
    assert not (root / "deps").exists()


def test_refuses_an_entry_both_groups_install(tmp_path):
    # A general library writing into the opentelemetry package could not be put behind the application's packages.
    root = _payload_with(tmp_path, "intruder-1.0", "intruder", "1.0", {"opentelemetry/intruder.py": ""})
    result = _build(root)
    assert result.returncode != 0
    assert "opentelemetry belong to both" in result.stderr
