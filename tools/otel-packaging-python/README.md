# Auto-instrumentation bootstrap for the operator image

The ADOT Python image the CloudWatch agent operator injects decides, in every Python process it reaches, whether
auto-instrumentation can start. When it cannot (for example on a Python older than the payload supports), it removes
itself from `sys.path` and `PYTHONPATH` and the application starts with its own packages, without telemetry, instead of
crashing on the payload's.

That decision is made by the [OpenTelemetry Packaging](https://github.com/open-telemetry/opentelemetry-packaging)
Python bootstrap (`packaging/common/python/sitecustomize.py`), vendored here unchanged and patched for the ADOT payload,
so that we track upstream instead of maintaining our own.

## Files

| File | Purpose |
|---|---|
| `UPSTREAM` | Upstream commit and SHA-256 of the vendored file, and the Python versions the payload supports |
| `vendor/sitecustomize.py`, `vendor/NOTICE` | The upstream file and its repository's NOTICE, unchanged |
| `patches/*.patch` | ADOT changes to the upstream file, applied in order |
| `adot_sitecustomize.py` | Entry point the operator's `PYTHONPATH` loads; runs the bootstrap |
| `adot_hooks.py` | What patch 0004 calls: the skip record, and the teardown after a failed initialization |
| `build.sh` | Installs all of this into a payload and moves its general libraries to `deps/`; called by the root `Dockerfile` |
| `tests/` | Tests for `build.sh`, the patched bootstrap, and the entry point |

## Layout

`build.sh` groups the payload `pip install --target` produces (package grouping, B2):

- The **OpenTelemetry group** (`opentelemetry-*` and `aws-opentelemetry-distro`) stays at the payload root, which the
  operator puts on `PYTHONPATH` ahead of the application's site-packages. The SDK needs the API version it was built
  with, and the API promises backward compatibility, so the application's own OpenTelemetry code runs on ours.
- **General libraries** (urllib3, requests, protobuf, wrapt, typing_extensions, ...) move to `<payload>/deps/`, which
  the bootstrap puts at the end of `sys.path`, behind the application's site-packages. They make no compatibility
  promise and the application depends on its own versions; the SDK's requirements on them are loose. An application
  without its own copy uses ours.

Distributions are grouped by their `RECORD`; a top-level entry both groups install fails the build.

`deps/` is placed at the end of `sys.path` once, at startup. A directory the application adds after that
(`site.addsitedir`, `sys.path.append`) comes after it, so for that directory our general libraries still come first,
as every payload package did before grouping.

## How it runs

The operator sets `PYTHONPATH=<payload>/opentelemetry/instrumentation/auto_instrumentation:<customer entries>:<payload>`.

1. Python imports `<payload>/opentelemetry/instrumentation/auto_instrumentation/sitecustomize.py`, which is
   `adot_sitecustomize.py`. It replaces upstream's, which only calls `initialize()`, and keeps ours ahead of any
   `sitecustomize` the customer has on `PYTHONPATH`.
2. It runs `<payload>/_otel_packaging_sitecustomize.py`, the patched upstream bootstrap. The file is not named
   `sitecustomize.py`: upstream `initialize()` stops child processes it starts from re-running auto-instrumentation by
   removing the `auto_instrumentation` directory from `PYTHONPATH`, which only works if no other `sitecustomize.py` is
   on it. The shim executes it under the module name `_otel_packaging_sitecustomize`, so upstream code must not rely
   on running as `__main__`: an `if __name__ == "__main__":` block would not run. The upstream file has none today, and
   the tests check that the bootstrap's top-level code runs (it initializes on a supported Python).
3. The bootstrap checks the Python version, the OTLP protocol, and the payload's OpenTelemetry distributions
   (`<payload>/cp3<minor>/all-dependencies.txt`), puts `<payload>/deps` at the end of `sys.path` (patch 0005), pins the
   `opentelemetry` namespace, then calls `initialize(swallow_exceptions=False)`, which loads the ADOT
   distro. On a failed check or an initialization error it removes the payload from `sys.path` and `PYTHONPATH` and
   clears `PYTHON_AUTO_INSTRUMENTATION_AGENT_PATH_PREFIX`. Through patch 0004 it then calls `<payload>/_adot_hooks.py`
   (`adot_hooks.py`), which:
   - after an initialization error, first undoes everything initialization started (see [Teardown](#teardown));
   - removes the variables the operator set for our SDK, and forgets every module loaded from the payload;
   - writes the [skip record](#skip-record) and one line to stderr.
4. When the bootstrap deactivates, `adot_sitecustomize.py` also removes every other payload directory from `sys.path` and
   `PYTHONPATH`: its own, which only the operator adds, and `cp3<minor>`, which the bootstrap inserts.

`all-dependencies.txt` lists only the payload's OpenTelemetry distributions. The SDK pins the exact API version it was
built with, so these must be the payload's copies; a different version earlier on `sys.path` (for example from the
customer's `PYTHONPATH`) deactivates auto-instrumentation instead of letting it start half-broken. General-purpose
libraries are not listed, so the check does not change which applications are instrumented.

## Patches

| Patch | Change | Why |
|---|---|---|
| `0001` | The double-instrumentation check never deactivates; it reports at debug level (`OTEL_INJECTOR_LOG_LEVEL=debug`) | Applications commonly depend on OpenTelemetry packages without instrumenting themselves; Apache Airflow depends on `opentelemetry-exporter-otlp` |
| `0002` | The payload goes back to its position on `sys.path` instead of the end | The OpenTelemetry group must stay ahead of the application's `site-packages`: an application that brings `opentelemetry-api` (fastapi, anthropic, google-cloud libraries do) would otherwise give our SDK an API version it does not support. General libraries go behind them through patch 0005 instead |
| `0003` | `OTEL_{TRACES,METRICS,LOGS}_EXPORTER` are not set | Upstream selects its own vendored exporters, which the ADOT payload does not contain; the ADOT distro sets its own defaults |
| `0004` | Every skip names a reason code and goes through `_adot_hooks.py`; initialization is bracketed by hooks; the warning no longer carries `sys.argv` | The skip record and teardown below. Arguments can hold passwords |
| `0005` | `<site>/deps` goes at the end of `sys.path`; the `opentelemetry` namespace's `__path__` is pinned before initialization | [Layout](#layout). A namespace package's path is recomputed when `sys.path` changes, so an application that moves its own site-packages forward after startup would get the OpenTelemetry modules not imported yet from its own copies |

Each patch is a candidate for an upstream option; drop it here once upstream has one.

## Skip record

Every Python process the operator reaches records its decision in `<payload>/.results/<container name>`, one line per
process, appended in a single `O_APPEND` write so that processes starting together lose no line. Once the file passes
about 100 lines it is trimmed, under a lock, to the newest line per program and layer (at most 50), so that a frequent
short-lived process such as an exec probe cannot push the main process's decision out. The OTel Injector writes the
same lines with `layer=injector` and follows the same rules; a reader takes the last line for a pid as the final
decision. `.results` is created writable by every user (mode 1777), because every injected container shares the
emptyDir and can run as a different user. Writing is best effort: a record that cannot be written changes nothing.

```
time=2026-11-24T21:59:44Z layer=sdk pid=1 program=/app/app.py language=python decision=skip reason=python_version_unsupported detail="unsupported Python version: 3.9.25 ..." env_removed=OTEL_PYTHON_DISTRO version=0.24.0
```

`program` is the program path, never the arguments. A skip also prints one line to stderr:
`[aws-instrumentation] SDK skipped: reason=<code> detail=<short>`.

| Reason | When |
|---|---|
| `python_version_unsupported` | Python is not one of `SUPPORTED_PYTHON_MINORS` |
| `config_file_invalid` | `otel-config-check` rejects `OTEL_CONFIG_FILE` (only where the validator ships, next to the payload) |
| `otlp_protocol_unsupported` | An OTLP protocol variable is neither `grpc` nor `http/protobuf` |
| `payload_incomplete` | `cp3<minor>/all-dependencies.txt` is missing or unreadable |
| `dependency_conflict` | One of the payload's OpenTelemetry distributions is shadowed by a different version |
| `init_failed` | Initialization raised; the teardown ran first |
| `bootstrap_error` | The bootstrap could not run, or failed in a way its checks do not handle |

The container name comes from `k8s.container.name` in `OTEL_RESOURCE_ATTRIBUTES` (the operator sets it), else from the
kubelet's `/dev/termination-log` mount in `/proc/self/mountinfo`, else `cmdline-<FNV-1a of /proc/1/cmdline>`.

On a skip, the variables the operator set for our SDK are removed, so that the application's own OpenTelemetry does
not pick them up (an unknown distro, configurator, sampler or propagator breaks it). When the operator lists them in
`AWS_OPERATOR_DEFAULT_ENV` (and the `OTEL_RESOURCE_ATTRIBUTES` keys it added in `AWS_OPERATOR_RESOURCE_ATTRIBUTE_KEYS`),
exactly those go. Without the list, only values nothing but the ADOT distro can use: `OTEL_PYTHON_DISTRO=aws_distro`,
`OTEL_PYTHON_CONFIGURATOR=aws_configurator`, `OTEL_TRACES_SAMPLER=xray` with its CloudWatch agent argument, and
`OTEL_PROPAGATORS` set to the operator's default list. Everything else may be the customer's and stays.

## Teardown

`initialize()` can raise after it patched libraries, started threads and installed providers, which keeps running
without telemetry the operator can see. Before deactivating, `adot_hooks.py` returns the process to the state recorded
right before initialization, each step on its own:

1. `uninstrument()` on every instrumented instrumentor.
2. Shuts down every exporter first, so that nothing the following shutdowns flush leaves the process; then
   ServiceEvents, Dynamic Instrumentation, and every provider, processor and reader (found with `gc`, because
   Application Signals and ServiceEvents keep private ones).
3. Cancels the timers started during initialization (the X-Ray sampler's pollers) and keeps the forked workers of a
   pre-fork server from restarting what was shut down: `os.register_at_fork` handlers cannot be removed.
4. Waits up to 2 seconds for the threads initialization started to exit.
5. Restores the root logger, `sys.path` (the distro adds the working directory), `sys.meta_path` (ServiceEvents adds an
   import hook), profiling and trace hooks, exception hooks, and the environment.
6. Removes the `opentelemetry.*` and ADOT modules first imported during initialization, including the application's
   own copies, and every other module loaded from the payload, so that the application imports its own versions.

## Updating upstream

1. Copy `packaging/common/python/sitecustomize.py` and `NOTICE` at the new commit to `vendor/`.
2. Update `UPSTREAM_COMMIT` and `UPSTREAM_SHA256` in `UPSTREAM`.
3. Re-apply `patches/` and refresh any that no longer apply. Drop those upstream has absorbed.
4. Run the tests.

## Tests

```sh
tox -e 3.11-otel-packaging-python
```

`RUN_DOCKER_TESTS=1` also runs the old-interpreter tests, which start `python:2.7-slim` to `python:3.9-slim` containers.

The teardown tests build the payload the image ships (`pip install --target` of `aws-opentelemetry-distro`, then
`build.sh`) once per session, so they need PyPI.

Run the tests on a Python the payload supports (`SUPPORTED_PYTHON_MINORS` in `UPSTREAM`). On any other version the
bootstrap skips auto-instrumentation by design, so the tests fail on purpose rather than pass for the wrong reason.
