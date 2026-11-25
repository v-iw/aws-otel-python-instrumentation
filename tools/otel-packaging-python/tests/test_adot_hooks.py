# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tests for adot_hooks.py, which the bootstrap calls through patch 0004: the skip record and stderr line, the removal
of the operator's variables on a skip, and the teardown after a failed initialization.

The record and variable tests use the small fake payload from conftest. The teardown tests use the payload the image
ships (the real ADOT distro, SDK and instrumentations), because what they check is that everything those start is
undone."""

import logging
import os
import re
import shutil
import subprocess
import sys
import time

import pytest
from conftest import AUTO_INSTRUMENTATION, operator_pythonpath, read_records, run_python

CONTAINER = "OTEL_RESOURCE_ATTRIBUTES"

# Interpreters older than the payload supports, which the operator injects into just the same.
OLD_INTERPRETERS = ["2.7", "3.5", "3.6", "3.7", "3.8", "3.9"]

# What the operator sets for Python with Application Signals (pkg/instrumentation/defaultinstrumentation.go in the
# CloudWatch agent operator), pointed at the test receiver.
OPERATOR_ENV = {
    "OTEL_AWS_APPLICATION_SIGNALS_ENABLED": "true",
    "OTEL_AWS_APPLICATION_SIGNALS_EXPORTER_ENDPOINT": "http://127.0.0.1:{port}/v1/metrics",
    "OTEL_TRACES_SAMPLER": "xray",
    "OTEL_TRACES_SAMPLER_ARG": "endpoint=http://127.0.0.1:{port}",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://127.0.0.1:{port}/v1/traces",
    "OTEL_METRICS_EXPORTER": "none",
    "OTEL_LOGS_EXPORTER": "none",
    "OTEL_PYTHON_DISTRO": "aws_distro",
    "OTEL_PYTHON_CONFIGURATOR": "aws_configurator",
    "OTEL_PROPAGATORS": "tracecontext,baggage,xray",
}

# An instrumentor that fails after the payload's own instrumentors have patched their libraries, the way B4 measured
# it: initialization had already started everything when it raised.
FAILING_INSTRUMENTOR = """
import logging
import threading
import time

from opentelemetry.instrumentation.instrumentor import BaseInstrumentor


class AddedDuringInitialization(logging.Handler):
    def emit(self, record):
        pass


class FailingInstrumentor(BaseInstrumentor):
    def instrumentation_dependencies(self):
        return []

    def _instrument(self, **kwargs):
        # A thread that imports more of the SDK while it finishes, the way a final flush can: the teardown waits for
        # it, and must remove what it imported meanwhile.
        def import_late():
            time.sleep(0.5)
            import opentelemetry.instrumentation.zz_failing.late  # noqa: F401  (the application's, not ours)

        threading.Thread(target=import_late).start()
        # Changes to logging that no uninstrument() undoes, the way a configurator makes them.
        logging.getLogger().addHandler(AddedDuringInitialization())
        logging.getLogger().setLevel(logging.DEBUG)
        logging.setLogRecordFactory(lambda *args, **kwargs: logging.LogRecord(*args, **kwargs))
        raise RuntimeError("instrumentor failed on purpose")

    def _uninstrument(self, **kwargs):
        pass
"""

# Printed by the application after sitecustomize ran: what the process looks like to it.
APPLICATION_STATE_SCRIPT = """
import json, logging, os, sys, threading, time
from urllib.request import OpenerDirector, urlopen

payload = os.environ["TEST_PAYLOAD"]
urlopen("http://127.0.0.1:%s/application" % os.environ["TEST_PORT"], timeout=5).read()
if os.environ.get("TEST_WAIT_FOR_THREADS"):
    deadline = time.time() + 5
    while len(threading.enumerate()) > 1 and time.time() < deadline:
        time.sleep(0.05)
opentelemetry_modules = [
    name for name in sys.modules if name.split(".")[0] == "opentelemetry" or name.startswith("amazon.opentelemetry")
]
try:
    import opentelemetry
    application_import = list(opentelemetry.__path__)
except ImportError as error:
    application_import = type(error).__name__


def from_payload(module):
    location = getattr(module, "__file__", None) or ""
    return location.startswith(payload)


print("STATE " + json.dumps({
    "threads": [thread.name for thread in threading.enumerate()],
    "urllib_patched": hasattr(OpenerDirector.open, "__wrapped__"),
    "threading_patched": hasattr(threading.Thread.start, "__wrapped__"),
    "opentelemetry_modules": opentelemetry_modules,
    "payload_modules": sorted(name for name, module in list(sys.modules.items())
                              if name != "sitecustomize" and from_payload(module)),
    "payload_on_sys_path": [entry for entry in sys.path if entry.startswith(payload)],
    "cwd_on_sys_path": os.getcwd() in sys.path,
    "meta_path": [type(finder).__name__ for finder in sys.meta_path],
    "root_handlers": [type(handler).__name__ for handler in logging.getLogger().handlers],
    "root_level": logging.getLogger().level,
    "default_record_factory": logging.getLogRecordFactory() is logging.LogRecord,
    "logging_disabled": logging.root.manager.disable,
    "application_import": application_import,
    "environ": dict(os.environ),
}))
"""

# Forks the way a pre-fork server (gunicorn) starts its workers, and reports the threads of both processes.
FORK_STATE_SCRIPT = """
import json, os, threading, time

read_end, write_end = os.pipe()
child = os.fork()
if child == 0:
    time.sleep(1.5)
    os.write(write_end, json.dumps([thread.name for thread in threading.enumerate()]).encode())
    os._exit(0)
os.close(write_end)
os.waitpid(child, 0)
print("STATE " + json.dumps({
    "parent_threads": [thread.name for thread in threading.enumerate()],
    "child_threads": json.loads(os.read(read_end, 65536).decode()),
}))
"""


def _narrow_allowlist(payload):
    # Excludes the running interpreter, which is what an unsupported Python sees.
    bootstrap = payload / "_otel_packaging_sitecustomize.py"
    bootstrap.write_text(
        re.sub(r"_SUPPORTED_PYTHON_MINORS = \([0-9, ]*\)", "_SUPPORTED_PYTHON_MINORS = (99,)", bootstrap.read_text())
    )


def _environment_script():
    return "import json, os; print('STATE ' + json.dumps(dict(os.environ)))"


def _only_record(payload, container="app"):
    records = read_records(payload)
    assert list(records) == [container]
    assert len(records[container]) == 1
    return records[container][0]


# ---------------------------------------------------------------------------------------------------------------------
# Skip record and stderr line


def test_skip_writes_one_record_line(payload):
    _narrow_allowlist(payload)
    run_python(operator_pythonpath(payload), {CONTAINER: "k8s.container.name=app,k8s.pod.name=pod-1"})
    record = _only_record(payload)
    assert record["layer"] == "sdk"
    assert record["language"] == "python"
    assert record["decision"] == "skip"
    assert record["reason"] == "python_version_unsupported"
    assert record["detail"].startswith(f"unsupported Python version: {sys.version_info[0]}.{sys.version_info[1]}")
    assert record["pid"].isdigit()
    assert record["program"] == f"{sys.executable} -c"
    assert record["version"] == "0.23.0"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", record["time"])


def test_inject_writes_one_record_line(payload):
    run_python(operator_pythonpath(payload), {CONTAINER: "k8s.container.name=app"})
    record = _only_record(payload)
    assert record["decision"] == "inject"
    assert "reason" not in record


def test_skip_writes_one_stderr_line(payload):
    _narrow_allowlist(payload)
    _, stderr = run_python(operator_pythonpath(payload))
    lines = stderr.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith(
        "[aws-instrumentation] SDK skipped: reason=python_version_unsupported "
        f'detail="unsupported Python version: {sys.version_info[0]}.{sys.version_info[1]}.'
    )


def test_record_and_stderr_never_carry_the_arguments(payload):
    # Arguments can hold passwords; only the program is recorded.
    _narrow_allowlist(payload)
    _, stderr = run_python(
        operator_pythonpath(payload), {CONTAINER: "k8s.container.name=app"}, args=["--password=s3cr3t"]
    )
    assert "s3cr3t" not in stderr
    assert "s3cr3t" not in (payload / ".results" / "app").read_text()


def _record(program, decision="inject", reason=None):
    line = f"time=2026-11-25T00:00:00Z layer=sdk pid=1 program={program} language=python decision={decision}"
    return line + (f" reason={reason}" if reason else "") + " version=0.23.0"


# A frequent short-lived Python process (an exec probe, a helper script) appends a line on every start; trimming to the
# last lines would push the main process's decision out of the file within minutes.
def test_frequent_short_lived_processes_keep_the_main_process_decision(payload):
    (payload / ".results").mkdir()
    main = _record("/app/main.py", "skip", "init_failed")
    probes = [_record("/app/probe.py").replace("pid=1", f"pid={number}") for number in range(300)]
    (payload / ".results" / "app").write_text("\n".join([main, *probes]) + "\n")
    run_python(operator_pythonpath(payload), {CONTAINER: "k8s.container.name=app"})
    lines = (payload / ".results" / "app").read_text().splitlines()
    # The newest line of each program, oldest first: the main process, the probe, and this process.
    assert lines[:2] == [main, probes[-1]]
    assert len(lines) == 3
    assert "decision=inject" in lines[-1]


def test_trimming_keeps_the_newest_fifty_programs(payload):
    (payload / ".results").mkdir()
    lines = [_record(f"/app/helper-{number}.py") for number in range(300)]
    (payload / ".results" / "app").write_text("\n".join(lines) + "\n")
    run_python(operator_pythonpath(payload), {CONTAINER: "k8s.container.name=app"})
    kept = (payload / ".results" / "app").read_text().splitlines()
    assert len(kept) == 50
    assert kept[:49] == lines[-49:]


def test_short_file_is_only_appended_to(payload):
    (payload / ".results").mkdir()
    existing = [_record("/app/main.py")] * 3
    (payload / ".results" / "app").write_text("\n".join(existing) + "\n")
    run_python(operator_pythonpath(payload), {CONTAINER: "k8s.container.name=app"})
    assert (payload / ".results" / "app").read_text().splitlines()[:3] == existing


# Workers of a pre-fork server, or several helpers, start at the same moment; rewriting the file lost all but one line.
def test_processes_starting_together_lose_no_line(payload):
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": operator_pythonpath(payload),
        "PYTHONDONTWRITEBYTECODE": "1",
        CONTAINER: "k8s.container.name=app",
    }
    processes = [
        subprocess.Popen([sys.executable, "-c", "pass"], env=environment, stderr=subprocess.DEVNULL) for _ in range(20)
    ]
    for process in processes:
        assert process.wait() == 0
    assert len((payload / ".results" / "app").read_text().splitlines()) == 20


def test_results_directory_is_writable_by_every_container_user(payload):
    # The emptyDir is shared by every container the operator injects, which can run as different users.
    run_python(operator_pythonpath(payload), {CONTAINER: "k8s.container.name=app"})
    assert (payload / ".results").stat().st_mode & 0o7777 == 0o1777


def test_container_name_falls_back_to_the_command_line_hash(payload):
    run_python(operator_pythonpath(payload))
    names = list(read_records(payload))
    if os.path.exists("/proc/1/cmdline"):
        assert len(names) == 1 and re.fullmatch(r"cmdline-[0-9a-f]{8}", names[0])
    else:
        assert names == ["unknown"]


def test_container_name_that_could_leave_the_results_directory_is_not_used(payload):
    run_python(operator_pythonpath(payload), {CONTAINER: "k8s.container.name=../escaped"})
    assert not (payload / "escaped").exists()
    assert "../escaped" not in list(read_records(payload))


def test_record_that_cannot_be_written_changes_no_decision(payload):
    (payload / ".results").write_text("a file where the directory should be\n")
    _narrow_allowlist(payload)
    state, stderr = run_python(operator_pythonpath(payload), {CONTAINER: "k8s.container.name=app"})
    assert not state["initialized"]
    assert "reason=python_version_unsupported" in stderr
    assert state["pythonpath"] == ""


@pytest.mark.skipif(
    os.environ.get("RUN_DOCKER_TESTS") != "1" or shutil.which("docker") is None,
    reason="starts python:<version>-slim containers; set RUN_DOCKER_TESTS=1",
)
@pytest.mark.parametrize("version", OLD_INTERPRETERS)
def test_old_interpreters_write_the_skip_record(payload, version):
    mount = "/otel-auto-instrumentation-python"
    pythonpath = f"{mount}/{AUTO_INSTRUMENTATION.as_posix()}:{mount}"
    result = subprocess.run(
        [
            "docker", "run", "--rm", "--network", "none", "-v", f"{payload}:{mount}",
            "-e", "PYTHONPATH=" + pythonpath, "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-e", f"{CONTAINER}=k8s.container.name=app", "-e", "OTEL_PYTHON_DISTRO=aws_distro",
            f"python:{version}-slim", "python", "-c", "import os; print(os.environ.get('OTEL_PYTHON_DISTRO'))",
        ],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["None"]
    assert result.stderr.splitlines()[0].startswith(
        "[aws-instrumentation] SDK skipped: reason=python_version_unsupported"
    )
    record = _only_record(payload)
    assert record["reason"] == "python_version_unsupported"
    assert record["env_removed"] == "OTEL_PYTHON_DISTRO"


# ---------------------------------------------------------------------------------------------------------------------
# The operator's variables


def test_skip_removes_the_variables_the_operator_lists(payload):
    _narrow_allowlist(payload)
    environ, _ = run_python(
        operator_pythonpath(payload),
        {
            "AWS_OPERATOR_DEFAULT_ENV": "OTEL_PROPAGATORS,OTEL_TRACES_EXPORTER",
            "AWS_OPERATOR_RESOURCE_ATTRIBUTE_KEYS": "k8s.pod.name,k8s.namespace.name",
            "OTEL_PROPAGATORS": "tracecontext,baggage,xray",
            "OTEL_TRACES_EXPORTER": "otlp",
            "OTEL_SERVICE_NAME": "customers-own",
            CONTAINER: "team=checkout,k8s.pod.name=pod-1,k8s.namespace.name=default",
        },
        script=_environment_script(),
    )
    assert "OTEL_PROPAGATORS" not in environ
    assert "OTEL_TRACES_EXPORTER" not in environ
    assert environ["OTEL_SERVICE_NAME"] == "customers-own"
    assert environ[CONTAINER] == "team=checkout"


def test_skip_without_the_list_removes_only_values_nothing_but_our_sdk_uses(payload):
    _narrow_allowlist(payload)
    environ, _ = run_python(
        operator_pythonpath(payload),
        {
            "OTEL_PYTHON_DISTRO": "aws_distro",
            "OTEL_PYTHON_CONFIGURATOR": "aws_configurator",
            "OTEL_PROPAGATORS": "tracecontext,baggage,xray",
            "OTEL_TRACES_SAMPLER": "xray",
            "OTEL_TRACES_SAMPLER_ARG": "endpoint=http://cloudwatch-agent.amazon-cloudwatch:2000",
            "OTEL_TRACES_EXPORTER": "otlp",
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://cloudwatch-agent.amazon-cloudwatch:4316/v1/traces",
        },
        script=_environment_script(),
    )
    for removed in (
        "OTEL_PYTHON_DISTRO",
        "OTEL_PYTHON_CONFIGURATOR",
        "OTEL_PROPAGATORS",
        "OTEL_TRACES_SAMPLER",
        "OTEL_TRACES_SAMPLER_ARG",
    ):
        assert removed not in environ, removed
    # Values an application's own OpenTelemetry can use as well: possibly the customer's, so they stay.
    assert environ["OTEL_TRACES_EXPORTER"] == "otlp"
    assert environ["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] == "http://cloudwatch-agent.amazon-cloudwatch:4316/v1/traces"


def test_skip_without_the_list_keeps_the_customers_own_values(payload):
    _narrow_allowlist(payload)
    customer = {
        "OTEL_PROPAGATORS": "tracecontext",
        "OTEL_TRACES_SAMPLER": "parentbased_always_on",
        "OTEL_TRACES_SAMPLER_ARG": "0.5",
    }
    environ, _ = run_python(operator_pythonpath(payload), customer, script=_environment_script())
    for name, value in customer.items():
        assert environ[name] == value


def test_inject_keeps_the_operator_variables(payload):
    environ, _ = run_python(
        operator_pythonpath(payload),
        {"AWS_OPERATOR_DEFAULT_ENV": "OTEL_PROPAGATORS", "OTEL_PROPAGATORS": "tracecontext,baggage,xray"},
        script=_environment_script(),
    )
    assert environ["OTEL_PROPAGATORS"] == "tracecontext,baggage,xray"


# ---------------------------------------------------------------------------------------------------------------------
# Teardown after a failed initialization, on the payload the image ships


def _operator_environment(receiver):
    port = receiver.server_address[1]
    environment = {name: value.format(port=port) for name, value in OPERATOR_ENV.items()}
    environment["AWS_OPERATOR_DEFAULT_ENV"] = ",".join(OPERATOR_ENV)
    environment["AWS_OPERATOR_RESOURCE_ATTRIBUTE_KEYS"] = "k8s.pod.name"
    environment[CONTAINER] = "k8s.container.name=app,k8s.pod.name=pod-1"
    environment["OTEL_SERVICE_NAME"] = "checkout"
    environment["TEST_PORT"] = str(port)
    return environment


def _failing_instrumentor_site(tmp_path):
    # An instrumentation package the application installed itself: opentelemetry.* modules that are not the
    # payload's, which the teardown must remove too.
    site = tmp_path / "application-site-packages"
    dist_info = site / "opentelemetry_instrumentation_zz_failing-1.0.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: opentelemetry-instrumentation-zz-failing\nVersion: 1.0\n\n"
    )
    (dist_info / "entry_points.txt").write_text(
        "[opentelemetry_instrumentor]\nzz_failing = opentelemetry.instrumentation.zz_failing:FailingInstrumentor\n"
    )
    package = site / "opentelemetry" / "instrumentation" / "zz_failing"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(FAILING_INSTRUMENTOR)
    (package / "late.py").write_text("")
    return site


def _run_application(payload, receiver, tmp_path, fail, script=APPLICATION_STATE_SCRIPT, extra_env=None):
    # The failing instrumentor sits after the payload, where an application's site-packages are, so that its entry
    # point loads last.
    pythonpath = operator_pythonpath(payload)
    if fail:
        pythonpath += os.pathsep + str(_failing_instrumentor_site(tmp_path))
    environment = _operator_environment(receiver)
    environment["TEST_PAYLOAD"] = str(payload)
    if fail:
        environment["TEST_WAIT_FOR_THREADS"] = "1"
    environment.update(extra_env or {})
    working_directory = tmp_path / "application"
    working_directory.mkdir(exist_ok=True)
    shutil.rmtree(payload / ".results", ignore_errors=True)
    state, stderr = run_python(pythonpath, environment, script=script, cwd=working_directory)
    # Whatever the process exported while exiting has arrived by now.
    time.sleep(1)
    return state, stderr, environment


def _exports(receiver):
    return [path for path in receiver.requests if path.startswith("/v1/")]


def _our_lines(stderr):
    # ServiceEvents logs its own startup at INFO level; everything else on stderr is ours or an error.
    return [line for line in stderr.splitlines() if " - INFO - " not in line]


def test_failed_initialization_is_undone(real_payload, receiver, tmp_path):
    state, stderr, environment = _run_application(real_payload, receiver, tmp_path, fail=True)
    # Instrumentation patches, threads, providers and modules: gone.
    assert state["threads"] == ["MainThread"]
    assert not state["urllib_patched"]
    assert not state["threading_patched"]
    assert state["opentelemetry_modules"] == []
    assert state["payload_modules"] == []
    # The application's own opentelemetry packages (here, its instrumentation package) are importable again, and only
    # from its own site-packages.
    assert state["application_import"] == [str(tmp_path / "application-site-packages" / "opentelemetry")]
    # sys.path, import hooks and logging: as before initialization.
    assert state["payload_on_sys_path"] == []
    assert not state["cwd_on_sys_path"]
    assert "ServiceEventsMetaPathFinder" not in state["meta_path"]
    assert state["root_handlers"] == []
    assert state["root_level"] == logging.WARNING
    assert state["default_record_factory"]
    assert state["logging_disabled"] == 0
    # The variables: the operator's are gone, the customer's and everything else are as they were.
    for name in OPERATOR_ENV:
        assert name not in state["environ"], name
    assert state["environ"][CONTAINER] == "k8s.container.name=app"
    assert state["environ"]["OTEL_SERVICE_NAME"] == "checkout"
    # Compared with the same interpreter started without the payload, which sets a few variables itself (PEP 538's
    # LC_CTYPE, for one).
    baseline, _ = run_python("", environment, script=_environment_script())
    assert state["environ"]["PYTHON_AUTO_INSTRUMENTATION_AGENT_PATH_PREFIX"] == ""
    added = set(state["environ"]) - set(baseline) - {"PYTHON_AUTO_INSTRUMENTATION_AGENT_PATH_PREFIX"}
    assert added == set(), added
    # Nothing exported, not even while exiting; one line on stderr; one record.
    assert _exports(receiver) == []
    assert "/application" in receiver.requests
    assert _our_lines(stderr) == [
        '[aws-instrumentation] SDK skipped: reason=init_failed detail="error when importing/initializing Python '
        'OpenTelemetry auto-instrumentation: RuntimeError: instrumentor failed on purpose"'
    ]
    record = _only_record(real_payload)
    assert record["reason"] == "init_failed"
    assert set(record["env_removed"].split(",")) == set(OPERATOR_ENV) | {CONTAINER}


# A control for the test above: the same process without the failing instrumentor is instrumented and exports, so
# that what the test above sees missing would have been there.
def test_successful_initialization_is_kept(real_payload, receiver, tmp_path):
    state, stderr, _ = _run_application(real_payload, receiver, tmp_path, fail=False)
    assert state["urllib_patched"]
    assert state["threading_patched"]
    assert len(state["threads"]) > 1
    assert state["opentelemetry_modules"] != []
    assert state["environ"]["OTEL_PYTHON_DISTRO"] == "aws_distro"
    # Application Signals metrics, at least; the X-Ray sampler decides about the spans.
    assert _exports(receiver) != []
    assert "[aws-instrumentation]" not in stderr


# Pre-fork servers (gunicorn) fork their workers after sitecustomize ran. ServiceEvents, Dynamic Instrumentation and
# the SDK restart their threads in every child through os.register_at_fork handlers, which cannot be removed.
@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
def test_failed_initialization_stays_undone_in_forked_workers(real_payload, receiver, tmp_path):
    state, stderr, _ = _run_application(
        real_payload,
        receiver,
        tmp_path,
        fail=True,
        script=FORK_STATE_SCRIPT,
        extra_env={
            "OTEL_AWS_DYNAMIC_INSTRUMENTATION_ENABLED": "true",
            # On macOS any urlopen starts native system threads, so Python 3.12+ warns about every fork there. Linux
            # has none; the Python-level threads are checked below on every platform.
            **({"PYTHONWARNINGS": "ignore::DeprecationWarning"} if sys.platform == "darwin" else {}),
        },
    )
    assert state == {"parent_threads": ["MainThread"], "child_threads": ["MainThread"]}
    assert _exports(receiver) == []
    assert len(_our_lines(stderr)) == 1


def test_skip_forgets_the_modules_the_bootstrap_loaded_from_the_payload(real_payload, receiver, tmp_path):
    # The dependency-conflict check imports the payload's packaging library; after a skip the application must get its
    # own (here: the test environment's) or none.
    vendor = tmp_path / "vendor"
    dist_info = vendor / "opentelemetry_api-1.27.0.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text("Metadata-Version: 2.1\nName: opentelemetry-api\nVersion: 1.27.0\n\n")
    script = (
        "import json, os, sys; payload = os.environ['TEST_PAYLOAD']; "
        "print('STATE ' + json.dumps(sorted(name for name, module in list(sys.modules.items()) "
        "if name != 'sitecustomize' and (getattr(module, '__file__', None) or '').startswith(payload))))"
    )
    environment = _operator_environment(receiver)
    environment["TEST_PAYLOAD"] = str(real_payload)
    shutil.rmtree(real_payload / ".results", ignore_errors=True)
    modules, stderr = run_python(operator_pythonpath(real_payload, vendor), environment, script=script)
    assert modules == []
    assert "reason=dependency_conflict" in stderr
    assert _only_record(real_payload)["reason"] == "dependency_conflict"
