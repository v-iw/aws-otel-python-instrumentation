# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# ADOT additions to the OpenTelemetry Packaging bootstrap. build.sh installs this file as <bundle>/_adot_hooks.py, next
# to the bootstrap (<bundle>/_otel_packaging_sitecustomize.py), which loads it through patch 0004 and calls:
#
#   skip(code, detail)       on every path that leaves the process uninstrumented, after it removed the bundle from
#                            sys.path and PYTHONPATH. Writes the skip record and the stderr line, and removes the
#                            variables the operator set for our SDK.
#   before_initialize()      right before it imports and initializes auto-instrumentation.
#   initialize_failed()      when initialization raised. Undoes everything initialization started (B4).
#   initialize_succeeded()   when the process is instrumented. Writes the inject record.
#
# The shim (adot_sitecustomize.py) loads it the same way and calls skip("bootstrap_error", ...) when it cannot run the
# bootstrap at all.
#
# The record is one line per decision, appended to <bundle>/.results/<container name>; the last RESULTS_KEEP_LINES
# lines are kept. The OTel Injector writes the same lines with layer=injector; a reader takes the last line for a pid
# as the final decision.
#
# IMPORTANT: this file must stay valid Python 2.7, use only the standard library, and never raise. skip() runs on every
# interpreter the operator reaches, including the ones the version gate rejects. The teardown only runs behind the
# gate, on the Python versions the payload supports.

# pylint: disable=consider-using-f-string  # f-strings are a SyntaxError on Python 2.7
# pylint: disable=broad-exception-caught  # nothing here may raise into the application's startup
# pylint: disable=global-statement,import-outside-toplevel

import io
import os
import sys
import time

# build.sh writes the payload's aws-opentelemetry-distro version into this line.
SDK_VERSION = "unknown"  # sdk-version
LAYER = "sdk"
LANGUAGE = "python"
RESULTS_DIRECTORY_NAME = ".results"
RESULTS_KEEP_LINES = 50
DETAIL_MAX_LENGTH = 200
STDERR_PREFIX = "[aws-instrumentation]"

# The operator lists the variables it set (A4). Without the list (operators before it), only values that the
# CloudWatch agent operator sets and that nothing but the ADOT distro can use are removed: left behind, they break an
# application's own OpenTelemetry (an unknown distro, configurator, sampler or propagator).
OPERATOR_DEFAULT_ENV = "AWS_OPERATOR_DEFAULT_ENV"
OPERATOR_RESOURCE_ATTRIBUTE_KEYS = "AWS_OPERATOR_RESOURCE_ATTRIBUTE_KEYS"
KNOWN_OPERATOR_VALUES = {
    "OTEL_PYTHON_DISTRO": ("aws_distro",),
    "OTEL_PYTHON_CONFIGURATOR": ("aws_configurator",),
    "OTEL_TRACES_SAMPLER": ("xray",),
    "OTEL_PROPAGATORS": ("tracecontext,baggage,xray", "tracecontext,baggage,b3,xray"),
}
# Removed only together with OTEL_TRACES_SAMPLER=xray, and only when it points at the CloudWatch agent.
SAMPLER_ARGUMENT = "OTEL_TRACES_SAMPLER_ARG"

BUNDLE_ROOT = os.path.dirname(os.path.abspath(__file__))

_snapshot = None


# ---------------------------------------------------------------------------------------------------------------------
# Skip record


def _single_line(value, limit=None):
    value = " ".join(("%s" % (value,)).split())
    if limit is not None and len(value) > limit:
        value = value[: limit - 3] + "..."
    return value


def _field(value):
    value = _single_line(value)
    if not value:
        return '""'
    if any(character in value for character in ' "=\\'):
        return '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"')
    return value


def _program():
    # The program path only, never the arguments: they can carry passwords.
    argv = getattr(sys, "argv", None) or []
    first = argv[0] if argv else ""
    if first in ("", "-c", "-m"):
        return ("%s %s" % (sys.executable or "python", first)).strip()
    try:
        return os.path.abspath(first)
    except Exception:
        return first


def _valid_container_name(name):
    # Kubernetes container names are DNS labels; anything else could escape the results directory.
    return 0 < len(name) <= 253 and name[0] != "." and all(c.isalnum() or c in "-._" for c in name)


def _container_name_from_resource_attributes():
    for item in os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "").split(","):
        key, separator, value = item.partition("=")
        if separator and key.strip() == "k8s.container.name" and _valid_container_name(value.strip()):
            return value.strip()
    return None


def _container_name_from_mountinfo():
    # The kubelet mounts /dev/termination-log from .../pods/<uid>/containers/<container name>/<id>.
    try:
        with io.open("/proc/self/mountinfo", "r", encoding="utf-8", errors="replace") as mountinfo:
            for line in mountinfo:
                fields = line.split(" ")
                if len(fields) < 5 or fields[4] != "/dev/termination-log":
                    continue
                parts = fields[3].split("/")
                for index in range(2, len(parts) - 1):
                    if parts[index] == "containers" and parts[index - 2] == "pods":
                        if _valid_container_name(parts[index + 1]):
                            return parts[index + 1]
    except Exception:
        pass
    return None


def _container_name_from_command_line():
    # FNV-1a (32-bit) of the container's first process's command line.
    try:
        with io.open("/proc/1/cmdline", "rb") as cmdline:
            data = bytearray(cmdline.read())
    except Exception:
        return None
    value = 0x811C9DC5
    for byte in data:
        value = ((value ^ byte) * 0x01000193) & 0xFFFFFFFF
    return "cmdline-%08x" % value


def container_name():
    return (
        _container_name_from_resource_attributes()
        or _container_name_from_mountinfo()
        or _container_name_from_command_line()
        or "unknown"
    )


def _record_line(decision, code=None, detail=None, removed_variables=None):
    fields = [
        ("time", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
        ("layer", LAYER),
        ("pid", os.getpid()),
        ("program", _program()),
        ("language", LANGUAGE),
        ("decision", decision),
    ]
    if code is not None:
        fields.append(("reason", code))
    if detail is not None:
        fields.append(("detail", _single_line(detail, DETAIL_MAX_LENGTH)))
    if removed_variables:
        fields.append(("env_removed", ",".join(removed_variables)))
    fields.append(("version", SDK_VERSION))
    return " ".join("%s=%s" % (key, _field(value)) for key, value in fields)


def _text(value):
    # io.open writes text only. Python 2 str and Python 3 bytes are byte strings (bytes is str on Python 2); joined
    # with text below, the result is text on both.
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def _append_record(line, name=None):
    temporary = None
    try:
        directory = os.path.join(BUNDLE_ROOT, RESULTS_DIRECTORY_NAME)
        try:
            os.mkdir(directory)
        except OSError:
            pass
        path = os.path.join(directory, name or container_name())
        lines = []
        try:
            with io.open(path, "r", encoding="utf-8", errors="replace") as results:
                lines = results.read().splitlines()
        except Exception:
            pass
        lines.append(_text(line))
        # Written whole and renamed into place, so a reader never sees a partial line. Two processes writing at the
        # same moment can drop one of their lines; the record is best effort.
        temporary = "%s.%d.tmp" % (path, os.getpid())
        with io.open(temporary, "w", encoding="utf-8") as results:
            results.write("\n".join(lines[-RESULTS_KEEP_LINES:]) + "\n")
        os.rename(temporary, path)
        temporary = None
    except Exception:
        pass
    finally:
        if temporary is not None:
            try:
                os.remove(temporary)
            except Exception:
                pass


def _write_stderr(line):
    try:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
    except Exception:
        pass


# ---------------------------------------------------------------------------------------------------------------------
# Operator variables


def _list_variable(name):
    return [item.strip() for item in os.environ.get(name, "").split(",") if item.strip()]


def _remove_resource_attribute_keys(keys):
    value = os.environ.get("OTEL_RESOURCE_ATTRIBUTES")
    if value is None or not keys:
        return False
    kept = [item for item in value.split(",") if item.partition("=")[0].strip() not in keys]
    if len(kept) == len(value.split(",")):
        return False
    if any(item.strip() for item in kept):
        os.environ["OTEL_RESOURCE_ATTRIBUTES"] = ",".join(kept)
    else:
        del os.environ["OTEL_RESOURCE_ATTRIBUTES"]
    return True


def remove_operator_variables():
    """Remove the variables the operator set for our SDK. Returns the names removed or changed."""
    removed = []
    listed = _list_variable(OPERATOR_DEFAULT_ENV)
    if listed:
        for name in listed:
            if name in os.environ and name not in (OPERATOR_DEFAULT_ENV, OPERATOR_RESOURCE_ATTRIBUTE_KEYS):
                del os.environ[name]
                removed.append(name)
        if _remove_resource_attribute_keys(_list_variable(OPERATOR_RESOURCE_ATTRIBUTE_KEYS)):
            removed.append("OTEL_RESOURCE_ATTRIBUTES")
        return removed
    for name in sorted(KNOWN_OPERATOR_VALUES):
        value = os.environ.get(name)
        if value is not None and value.strip().replace(" ", "") in KNOWN_OPERATOR_VALUES[name]:
            del os.environ[name]
            removed.append(name)
    argument = os.environ.get(SAMPLER_ARGUMENT, "")
    if "OTEL_TRACES_SAMPLER" in removed and argument.startswith("endpoint=") and "cloudwatch-agent" in argument:
        del os.environ[SAMPLER_ARGUMENT]
        removed.append(SAMPLER_ARGUMENT)
    return removed


# ---------------------------------------------------------------------------------------------------------------------
# Hooks


def skip(code, detail):
    """Report that this process is not instrumented. Returns True once the stderr line is written."""
    # Named before the operator's variables go: the container name can come from OTEL_RESOURCE_ATTRIBUTES.
    name = container_name()
    removed = []
    try:
        removed = remove_operator_variables()
    except Exception:
        pass
    _attempt(_forget_bundle_modules)
    _append_record(_record_line("skip", code, detail, removed), name)
    _write_stderr(
        "%s SDK skipped: reason=%s detail=%s"
        % (STDERR_PREFIX, _field(code), _field(_single_line(detail, DETAIL_MAX_LENGTH)))
    )
    return True


def initialize_succeeded():
    global _snapshot
    _snapshot = None
    _append_record(_record_line("inject"))


def before_initialize():
    """Record the process state initialization starts from, so that a failure can return to it."""
    global _snapshot
    import threading

    logging_state = None
    if "logging" in sys.modules:
        import logging

        root = logging.getLogger()
        logging_state = (list(root.handlers), root.level, logging.getLogRecordFactory(), logging.getLoggerClass())
    _snapshot = {
        "modules": set(sys.modules),
        "path": list(sys.path),
        "meta_path": list(sys.meta_path),
        "path_hooks": list(sys.path_hooks),
        "environ": dict(os.environ),
        "threads": set(threading.enumerate()),
        "logging": logging_state,
        "profile": (sys.getprofile(), sys.gettrace(), threading.getprofile(), threading.gettrace()),
        "excepthooks": (sys.excepthook, threading.excepthook),
    }


def initialize_failed():
    """Undo everything initialization started: patches, providers, threads, hooks, modules, path and environment."""
    global _snapshot
    snapshot, _snapshot = _snapshot, None
    if snapshot is None:
        return
    try:
        import logging
    except Exception:
        return
    previous_disable = logging.root.manager.disable
    # The SDK logs every shutdown step; none of that is the application's.
    logging.disable(logging.CRITICAL)
    try:
        _teardown(snapshot)
    except Exception:
        pass
    finally:
        logging.disable(previous_disable)


# ---------------------------------------------------------------------------------------------------------------------
# Teardown (B4). Runs only behind the bootstrap's version gate, after initialization raised and before the bootstrap
# removes the bundle from sys.path and PYTHONPATH. Every step is attempted on its own; nothing new is imported from
# the bundle.


def _inside(path, directory):
    path = os.path.normpath(os.path.abspath(path))
    directory = os.path.normpath(directory)
    return path == directory or path.startswith(directory + os.sep)


def _loaded(module_name, attribute):
    module = sys.modules.get(module_name)
    return getattr(module, attribute, None) if module is not None else None


def _classes(*names):
    classes = []
    for module_name, attribute in names:
        cls = _loaded(module_name, attribute)
        if isinstance(cls, type):
            classes.append(cls)
    return tuple(classes)


def _attempt(function, *args):
    try:
        function(*args)
    except Exception:
        pass


def _call(instance, method):
    # Looked up inside the try: a missing method must not end the loop it is called from.
    try:
        getattr(instance, method)()
    except Exception:
        pass


def _loaded_from_bundle(module):
    try:
        namespace = vars(module)
    except Exception:
        return False
    locations = []
    if namespace.get("__file__"):
        locations.append(namespace["__file__"])
    else:
        locations.extend(list(namespace.get("__path__") or []))
    return bool(locations) and all(_inside(location, BUNDLE_ROOT) for location in locations)


def _being_imported(name, module):
    # The module running this (sitecustomize, from the bundle) is still being imported: removing it from sys.modules
    # would make its own import fail, on every interpreter.
    spec = getattr(module, "__spec__", None)
    return name in ("sitecustomize", "usercustomize") or bool(getattr(spec, "_initializing", False))


def _forget_bundle_modules():
    # The bootstrap's own checks import from the bundle (its dependency-conflict check uses the bundle's packaging
    # library). Once the bundle is off sys.path, the application must import its own copies, or none.
    names = [
        name
        for name, module in list(sys.modules.items())
        if not _being_imported(name, module) and _loaded_from_bundle(module)
    ]
    for name in names:
        sys.modules.pop(name, None)


def _new_modules(snapshot):
    """Modules first imported during initialization that belong to our SDK: OpenTelemetry and ADOT by name
    (namespace packages have no file to judge by), and anything else loaded from the bundle (such as its urllib3)."""
    names = []
    for name, module in list(sys.modules.items()):
        if name in snapshot["modules"]:
            continue
        if (
            name.split(".")[0] == "opentelemetry"
            or name == "amazon.opentelemetry"
            or name.startswith("amazon.opentelemetry.")
        ):
            names.append(name)
        elif _loaded_from_bundle(module):
            names.append(name)
    return names


def _uninstrument(objects):
    base = _classes(("opentelemetry.instrumentation.instrumentor", "BaseInstrumentor"))
    for instrumentor in objects:
        if (
            base
            and isinstance(instrumentor, base)
            and getattr(instrumentor, "_is_instrumented_by_opentelemetry", False)
        ):
            _call(instrumentor, "uninstrument")


def _shutdown_instances(objects, classes):
    for instance in objects:
        if classes and isinstance(instance, classes):
            _call(instance, "shutdown")


def _inert_after_fork(objects):
    # The SDK restarts the worker of every batch processor and metric reader in a forked child, shut down or not, and
    # a restarted reader exports once more (into an exporter that is shut down, which logs a warning in every
    # worker). Their fork handlers cannot be removed; the thread they start looks its target up on the instance.
    readers = _classes(("opentelemetry.sdk.metrics.export", "MetricReader"))
    processors = _classes(("opentelemetry.sdk._shared_internal", "BatchProcessor"))
    for instance in objects:
        if readers and isinstance(instance, readers) and hasattr(instance, "_ticker"):
            _attempt(setattr, instance, "_ticker", lambda: None)
        elif processors and isinstance(instance, processors) and hasattr(instance, "worker"):
            _attempt(setattr, instance, "worker", lambda: None)


def _shutdown_service_events():
    module_name = "amazon.opentelemetry.distro.serviceevents.serviceevents_instrumentation"
    instance = _loaded(module_name, "_serviceevents_instance")
    if instance is not None:
        _call(instance, "shutdown")
        # Its fork handler cannot be unregistered and restarts every collector in a forked worker.
        _attempt(setattr, instance, "collectors", [])


def _shutdown_dynamic_instrumentation(objects):
    # Dynamic Instrumentation polls the agent from its own threads, which no OpenTelemetry shutdown reaches.
    debugger = sys.modules.get("amazon.opentelemetry.distro.debugger.debugger")
    if debugger is not None and hasattr(debugger, "cleanup_debugger"):
        _attempt(debugger.cleanup_debugger)
    # cleanup_debugger stops the configuration pollers but not the status reporters.
    classes = _classes(
        ("amazon.opentelemetry.distro.debugger._status_reporter", "StatusReporter"),
        ("amazon.opentelemetry.distro.debugger._status_reporter", "BackgroundStatusReporter"),
    )
    for instance in objects:
        if classes and isinstance(instance, classes):
            _call(instance, "stop")


def _disable_fork_reinitialization():
    # The Dynamic Instrumentation fork handler re-initializes the debugger in every forked worker; os.register_at_fork
    # has no way to remove it, so the function it calls is replaced instead.
    debugger = sys.modules.get("amazon.opentelemetry.distro.debugger.debugger")
    if debugger is not None:
        _attempt(setattr, debugger, "initialize_debugger", lambda *args, **kwargs: False)
    import logging

    for name in list(logging.Logger.manager.loggerDict):
        if name == "amazon.opentelemetry" or name.startswith("amazon.opentelemetry."):
            logger = logging.Logger.manager.loggerDict[name]
            if isinstance(logger, logging.Logger):
                logger.disabled = True


def _stop_timers(snapshot, module_names):
    import threading

    class _InertTimer(threading.Timer):
        def start(self):
            pass

    # A poller that is running right now schedules its next Timer when it returns; our modules get one that never
    # starts. Then every Timer started during initialization is cancelled.
    for name in module_names:
        namespace = getattr(sys.modules.get(name), "__dict__", None)
        if namespace is not None and namespace.get("Timer") is threading.Timer:
            namespace["Timer"] = _InertTimer
    for thread in threading.enumerate():
        if thread not in snapshot["threads"] and isinstance(thread, threading.Timer):
            _call(thread, "cancel")


def _join_new_threads(snapshot, budget_seconds=2.0):
    # Everything above stops its threads, but some only notice on their next wake-up. Waiting for them (within a
    # budget, for one stuck in a network call) means the application starts, and forks its workers, single-threaded.
    import threading

    deadline = time.time() + budget_seconds
    for thread in threading.enumerate():
        if thread in snapshot["threads"] or thread is threading.current_thread():
            continue
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        _attempt(thread.join, remaining)


def _restore_logging(snapshot):
    import logging

    root = logging.getLogger()
    handlers, level, factory, logger_class = snapshot["logging"] or (
        [],
        logging.WARNING,
        logging.LogRecord,
        logging.Logger,
    )
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
            _call(handler, "close")
    root.setLevel(level)
    logging.setLogRecordFactory(factory)
    logging.setLoggerClass(logger_class)


def _restore_interpreter(snapshot):
    import threading

    sys.path[:] = snapshot["path"]
    sys.meta_path[:] = snapshot["meta_path"]
    sys.path_hooks[:] = snapshot["path_hooks"]
    for entry in list(sys.path_importer_cache):
        if entry not in sys.path or _inside(entry, BUNDLE_ROOT):
            sys.path_importer_cache.pop(entry, None)
    profile, trace, thread_profile, thread_trace = snapshot["profile"]
    sys.setprofile(profile)
    sys.settrace(trace)
    threading.setprofile(thread_profile)
    threading.settrace(thread_trace)
    sys.excepthook, threading.excepthook = snapshot["excepthooks"]


def _restore_environ(snapshot):
    environ = snapshot["environ"]
    for name in list(os.environ):
        if name not in environ:
            del os.environ[name]
    for name, value in environ.items():
        if os.environ.get(name) != value:
            os.environ[name] = value


def _remove_modules(names):
    for name in names:
        sys.modules.pop(name, None)


def _teardown(snapshot):
    import gc

    objects = gc.get_objects()
    try:
        # 1. Instrumentation patches, so that the application's libraries are its own again.
        _attempt(_uninstrument, objects)
        # 2. Exporters first, so that nothing the shutdowns below flush leaves the process.
        _attempt(
            _shutdown_instances,
            objects,
            _classes(
                ("opentelemetry.sdk.trace.export", "SpanExporter"),
                ("opentelemetry.sdk.metrics.export", "MetricExporter"),
                ("opentelemetry.sdk._logs.export", "LogRecordExporter"),
                ("opentelemetry.sdk._logs.export", "LogExporter"),
            ),
        )
        _attempt(_shutdown_service_events)
        _attempt(_shutdown_dynamic_instrumentation, objects)
        # 3. Providers, then any processor or reader no provider owns (Application Signals and ServiceEvents keep
        #    private ones the global getters cannot reach).
        _attempt(
            _shutdown_instances,
            objects,
            _classes(
                ("opentelemetry.sdk.trace", "TracerProvider"),
                ("opentelemetry.sdk.metrics", "MeterProvider"),
                ("opentelemetry.sdk._logs", "LoggerProvider"),
            ),
        )
        _attempt(
            _shutdown_instances,
            objects,
            _classes(
                ("opentelemetry.sdk.trace", "SpanProcessor"),
                ("opentelemetry.sdk.metrics.export", "MetricReader"),
                ("opentelemetry.sdk._logs", "LogRecordProcessor"),
            ),
        )
        _attempt(_inert_after_fork, objects)
    finally:
        del objects
    module_names = _new_modules(snapshot)
    # 4. Threads and hooks that outlive a shutdown.
    _attempt(_stop_timers, snapshot, module_names)
    _attempt(_disable_fork_reinitialization)
    _attempt(_join_new_threads, snapshot)
    # 5. Process-wide state, back to what it was before initialization.
    _attempt(_restore_logging, snapshot)
    _attempt(_restore_interpreter, snapshot)
    _attempt(_restore_environ, snapshot)
    # 6. Our modules, so that the application imports its own copies (or none). Decided first, then removed.
    _attempt(_remove_modules, module_names)
    try:
        import importlib

        importlib.invalidate_caches()
    except Exception:
        pass
