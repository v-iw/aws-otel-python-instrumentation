# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tests for the OpenTelemetry Packaging bootstrap (vendor/sitecustomize.py) as build.sh installs it, with ADOT's
patches: the decisions it makes in a process started the way the operator starts it."""

import re
import subprocess
import sys

import pytest
from conftest import operator_pythonpath, read_records, run_python, write_distribution


def _bundle_entries(state, payload):
    return [entry for entry in state["sys_path"] if entry == str(payload) or entry.startswith(str(payload) + "/")]


def _first_site_packages_index(state):
    return next(index for index, entry in enumerate(state["sys_path"]) if entry.endswith("site-packages"))


def test_initializes_on_a_supported_python(payload):
    pythonpath = operator_pythonpath(payload)
    state, stderr = run_python(pythonpath)
    assert state["initialized"]
    assert state["pythonpath"] == pythonpath
    assert "cannot auto-instrument" not in stderr


def test_skips_an_unsupported_python_before_initializing(payload):
    # Narrow the allowlist to exclude the running interpreter, which is what an unsupported Python sees.
    bootstrap = payload / "_otel_packaging_sitecustomize.py"
    text = bootstrap.read_text()
    text = re.sub(r"_SUPPORTED_PYTHON_MINORS = \([0-9, ]*\)", "_SUPPORTED_PYTHON_MINORS = (99,)", text)
    bootstrap.write_text(text)
    state, stderr = run_python(operator_pythonpath(payload))
    assert not state["initialized"]
    assert f"unsupported Python version: {sys.version_info[0]}.{sys.version_info[1]}" in stderr
    assert _bundle_entries(state, payload) == []
    assert state["pythonpath"] == ""
    assert state["prefix"] == ""


def test_initialization_failure_deactivates(payload, tmp_path):
    customer_entry = tmp_path / "customer"
    customer_entry.mkdir()
    state, stderr = run_python(operator_pythonpath(payload, customer_entry), {"FAKE_INITIALIZE": "raise"})
    assert "RuntimeError: fake initialization failure" in stderr
    assert _bundle_entries(state, payload) == []
    assert state["pythonpath"] == str(customer_entry)
    assert state["prefix"] == ""


# ADOT patch 0001: an application that ships OpenTelemetry packages of its own (Apache Airflow depends on
# opentelemetry-exporter-otlp) used to be treated as already instrumented and skipped.
def test_application_opentelemetry_packages_do_not_deactivate(payload, tmp_path):
    site_packages = tmp_path / "app-site-packages"
    write_distribution(site_packages, "opentelemetry_exporter_otlp-1.27.0", "opentelemetry-exporter-otlp", "1.27.0")
    write_distribution(site_packages, "opentelemetry_sdk-1.27.0", "opentelemetry-sdk", "1.27.0")
    state, stderr = run_python(operator_pythonpath(payload) + ":" + str(site_packages))
    assert state["initialized"]
    assert "cannot auto-instrument" not in stderr


def test_application_opentelemetry_packages_are_reported_in_debug_runs(payload, tmp_path):
    site_packages = tmp_path / "app-site-packages"
    write_distribution(site_packages, "opentelemetry_sdk-1.27.0", "opentelemetry-sdk", "1.27.0")
    state, stderr = run_python(
        operator_pythonpath(payload) + ":" + str(site_packages), {"OTEL_INJECTOR_LOG_LEVEL": "debug"}
    )
    assert state["initialized"]
    assert "opentelemetry-sdk 1.27.0" in stderr
    assert "Continuing with auto-instrumentation" in stderr


# ADOT patch 0002: the bundle used to be moved to the end of sys.path, behind the application's site-packages.
def test_bundle_stays_ahead_of_site_packages(payload):
    state, _ = run_python(operator_pythonpath(payload))
    bundle_index = state["sys_path"].index(str(payload))
    assert bundle_index < _first_site_packages_index(state)
    assert state["sys_path"][bundle_index + 1] == str(payload / f"cp3{sys.version_info[1]}")


# ADOT patch 0003: the upstream bundle used to set OTEL_*_EXPORTER to its own exporters, which the ADOT payload does
# not contain, so initialization failed whenever the operator left them unset.
def test_does_not_select_exporters(payload):
    state, _ = run_python(operator_pythonpath(payload))
    assert state["initialized"]
    assert state["exporters"] == {"TRACES": None, "METRICS": None, "LOGS": None}


def test_keeps_exporters_the_operator_set(payload):
    state, _ = run_python(operator_pythonpath(payload), {"OTEL_TRACES_EXPORTER": "otlp", "OTEL_LOGS_EXPORTER": "none"})
    assert state["exporters"] == {"TRACES": "otlp", "METRICS": None, "LOGS": "none"}


def test_opentelemetry_package_shadowed_by_an_earlier_entry_deactivates(payload, tmp_path):
    # The SDK pins the exact API version it was built with, so an older API ahead of the bundle cannot work.
    vendor = tmp_path / "vendor"
    write_distribution(vendor, "opentelemetry_api-1.27.0", "opentelemetry-api", "1.27.0")
    state, stderr = run_python(operator_pythonpath(payload, vendor))
    assert not state["initialized"]
    assert "dependency conflicts: opentelemetry-api (requires ==1.45.2, found 1.27.0)" in stderr
    assert _bundle_entries(state, payload) == []
    assert state["pythonpath"] == str(vendor)


def test_general_library_shadowed_by_an_earlier_entry_does_not_deactivate(payload, tmp_path):
    vendor = tmp_path / "vendor"
    write_distribution(vendor, "urllib3-1.26.20", "urllib3", "1.26.20")
    state, stderr = run_python(operator_pythonpath(payload, vendor))
    assert state["initialized"]
    assert "cannot auto-instrument" not in stderr


# ADOT patch 0004: every path that leaves the process uninstrumented names its reason code, for the skip record the
# agent reads and for the one stderr line.
def _narrow_allowlist(payload, _tmp_path):
    bootstrap = payload / "_otel_packaging_sitecustomize.py"
    text = re.sub(r"_SUPPORTED_PYTHON_MINORS = \([0-9, ]*\)", "_SUPPORTED_PYTHON_MINORS = (99,)", bootstrap.read_text())
    bootstrap.write_text(text)
    return [], {}


def _unusable_config_file(payload, tmp_path):
    # The bootstrap validates the file with otel-config-check next to the bundle, which the Injector layout ships and
    # the ADOT image does not; a stand-in that rejects every file.
    validator = payload.parent / "otel-config-check"
    validator.write_text("#!/bin/sh\necho 'file_format is missing'\nexit 1\n")
    validator.chmod(0o755)
    return [], {"OTEL_CONFIG_FILE": str(tmp_path / "config.yaml")}


def _unsupported_protocol(_payload, _tmp_path):
    return [], {"OTEL_EXPORTER_OTLP_PROTOCOL": "http/json"}


def _missing_dependency_list(payload, _tmp_path):
    (payload / f"cp3{sys.version_info[1]}" / "all-dependencies.txt").unlink()
    return [], {}


def _shadowed_api(_payload, tmp_path):
    vendor = tmp_path / "vendor"
    write_distribution(vendor, "opentelemetry_api-1.27.0", "opentelemetry-api", "1.27.0")
    return [vendor], {}


def _failing_initialization(_payload, _tmp_path):
    return [], {"FAKE_INITIALIZE": "raise"}


def _broken_packaging_library(_payload, tmp_path):
    # Any error the checks do not handle themselves: here, the packaging library raising on import.
    broken = tmp_path / "broken" / "packaging"
    broken.mkdir(parents=True)
    (broken / "__init__.py").write_text("raise RuntimeError('broken packaging library')\n")
    return [broken.parent], {}


@pytest.mark.parametrize(
    "setup, code",
    [
        (_narrow_allowlist, "python_version_unsupported"),
        (_unusable_config_file, "config_file_invalid"),
        (_unsupported_protocol, "otlp_protocol_unsupported"),
        (_missing_dependency_list, "payload_incomplete"),
        (_shadowed_api, "dependency_conflict"),
        (_failing_initialization, "init_failed"),
        (_broken_packaging_library, "bootstrap_error"),
    ],
)
def test_every_skip_names_its_reason_code(payload, tmp_path, setup, code):
    customer_entries, extra_env = setup(payload, tmp_path)
    extra_env["OTEL_RESOURCE_ATTRIBUTES"] = "k8s.container.name=app"
    state, stderr = run_python(operator_pythonpath(payload, *customer_entries), extra_env)
    assert _bundle_entries(state, payload) == []
    assert stderr.splitlines()[-1].startswith(f"[aws-instrumentation] SDK skipped: reason={code} detail=")
    assert [record["reason"] for record in read_records(payload)["app"]] == [code]


def test_skip_without_the_hooks_still_warns_without_the_arguments(payload):
    # A payload without _adot_hooks.py changes no decision: upstream's warning, minus the command line.
    (payload / "_adot_hooks.py").unlink()
    state, stderr = run_python(operator_pythonpath(payload), {"FAKE_INITIALIZE": "raise"}, args=["--password=s3cr3t"])
    assert _bundle_entries(state, payload) == []
    assert "cannot auto-instrument Python process: error when importing/initializing" in stderr
    assert "s3cr3t" not in stderr


# ADOT patch 0005, package grouping (B2): an application that pins its own urllib3 (requests 2.31 with urllib3 1.26.20)
# got the payload's urllib3 2.x instead, which broke it on every Python, and crashed it on Python 3.9.
def test_general_libraries_come_after_the_applications_packages(payload):
    state, _ = run_python(operator_pythonpath(payload))
    assert state["sys_path"][-1] == str(payload / "deps")
    assert state["sys_path"].index(str(payload)) < _first_site_packages_index(state)
    assert _first_site_packages_index(state) < len(state["sys_path"]) - 1


def _import_urllib3(payload, *application_entries):
    script = "import json, urllib3; print('STATE ' + json.dumps(urllib3.SOURCE))"
    pythonpath = operator_pythonpath(payload) + "".join(":" + str(entry) for entry in application_entries)
    return run_python(pythonpath, script=script)[0]


def test_application_keeps_its_own_general_library(payload, tmp_path):
    # Appended after the payload, which is where the application's site-packages are.
    application = tmp_path / "application-site-packages"
    write_distribution(
        application, "urllib3-1.26.20", "urllib3", "1.26.20", {"urllib3/__init__.py": "SOURCE = 'application'\n"}
    )
    assert _import_urllib3(payload, application) == "application"


def test_general_library_the_application_lacks_comes_from_the_payload(payload):
    assert _import_urllib3(payload) == "payload"


def test_application_pinning_urllib3_1_26_keeps_it_and_is_instrumented(real_payload, tmp_path):
    application = tmp_path / "application-site-packages"
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "--target", str(application),
         "requests==2.31.0", "urllib3==1.26.20"],
        check=True, capture_output=True, text=True,
    )  # fmt: skip
    script = (
        "import json, requests, urllib3; print('STATE ' + json.dumps({"
        "'urllib3': [urllib3.__version__, urllib3.__file__], 'requests': requests.__version__, "
        "'instrumented': hasattr(requests.Session.send, '__wrapped__')}))"
    )
    state, stderr = run_python(
        operator_pythonpath(real_payload) + ":" + str(application),
        {"OTEL_TRACES_EXPORTER": "none", "OTEL_METRICS_EXPORTER": "none", "OTEL_LOGS_EXPORTER": "none"},
        script=script,
    )
    assert state["urllib3"] == ["1.26.20", str(application / "urllib3" / "__init__.py")]
    assert state["requests"] == "2.31.0"
    assert state["instrumented"]
    assert "[aws-instrumentation]" not in stderr


def test_application_moving_its_packages_forward_later_keeps_our_opentelemetry(real_payload, tmp_path):
    # ADOT patch 0005 pins the opentelemetry namespace: an application that puts its own site-packages first after
    # startup would otherwise get every opentelemetry module not imported yet from its own copies.
    application = tmp_path / "application-site-packages"
    marker = application / "opentelemetry" / "propagators" / "ot_trace" / "__init__.py"
    marker.parent.mkdir(parents=True)
    marker.write_text("SOURCE = 'application'\n")
    script = (
        f"import json, sys; sys.path.insert(0, {str(application)!r}); "
        "import opentelemetry.propagators.ot_trace as ot_trace; "
        "print('STATE ' + json.dumps(ot_trace.__file__))"
    )
    location, _ = run_python(
        operator_pythonpath(real_payload),
        {"OTEL_TRACES_EXPORTER": "none", "OTEL_METRICS_EXPORTER": "none", "OTEL_LOGS_EXPORTER": "none"},
        script=script,
    )
    assert location.startswith(str(real_payload) + "/")
