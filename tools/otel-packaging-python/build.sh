#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Installs the auto-instrumentation bootstrap into an installed ADOT Python payload (the output of
# `pip install --target <payload> aws-opentelemetry-distro`):
#
#   <payload>/_otel_packaging_sitecustomize.py
#       OpenTelemetry Packaging's sitecustomize.py (vendor/, pinned in UPSTREAM) with patches/*.patch applied and its
#       Python version allowlist set to SUPPORTED_PYTHON_MINORS.
#   <payload>/opentelemetry/instrumentation/auto_instrumentation/sitecustomize.py
#       adot_sitecustomize.py, replacing upstream's.
#   <payload>/_adot_hooks.py
#       adot_hooks.py, which patch 0004 calls: the skip record, and the teardown after a failed initialization.
#   <payload>/deps/
#       Every distribution that is not OpenTelemetry's or the ADOT distro's, moved out of the payload root so that the
#       bootstrap can put it behind the application's site-packages (package grouping, patch 0005).
#   <payload>/cp3<minor>/all-dependencies.txt, for every supported minor
#       The payload's OpenTelemetry distributions, pinned, for the bootstrap's dependency-conflict check.
#
# Usage: tools/otel-packaging-python/build.sh <payload dir>
# Needs bash, patch and python3.
set -euo pipefail

if [ $# -ne 1 ]; then
  echo "usage: $0 <payload dir>" >&2
  exit 2
fi

here=$(cd "$(dirname "$0")" && pwd)
payload=$(cd "$1" && pwd)
# shellcheck source=SCRIPTDIR/UPSTREAM
source "$here/UPSTREAM"

auto_instrumentation=$payload/opentelemetry/instrumentation/auto_instrumentation
if [ ! -d "$auto_instrumentation" ]; then
  echo "not an installed ADOT Python payload: $auto_instrumentation does not exist" >&2
  exit 1
fi

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT


python3 - "$here/vendor/sitecustomize.py" "$UPSTREAM_SHA256" <<'EOF'
import hashlib, sys
path, expected = sys.argv[1], sys.argv[2]
with open(path, "rb") as f:
    actual = hashlib.sha256(f.read()).hexdigest()
if actual != expected:
    sys.exit("%s: SHA-256 is %s, UPSTREAM pins %s" % (path, actual, expected))
EOF

cp "$here/vendor/sitecustomize.py" "$work/sitecustomize.py"
for patch_file in "$here"/patches/*.patch; do
  # No --forward: a patch that is already applied (for example, duplicated during a rebase) must fail the build.
  patch --quiet --batch --directory="$work" -p1 < "$patch_file"
done

# Written the way the upstream build writes it (packaging/builder/download.go), into the marker line the file carries.
python3 - "$work/sitecustomize.py" "$SUPPORTED_PYTHON_MINORS" <<'EOF'
import re, sys
path, minors = sys.argv[1], sys.argv[2].split()
with open(path, encoding="utf-8") as f:
    text = f.read()
pattern = re.compile(r"_SUPPORTED_PYTHON_MINORS = \([0-9, ]*\)  # supported-python-minors")
if len(pattern.findall(text)) != 1:
    sys.exit("%s: expected exactly one supported-python-minors marker" % path)
text = pattern.sub("_SUPPORTED_PYTHON_MINORS = (%s,)  # supported-python-minors" % ", ".join(minors), text)
with open(path, "w", encoding="utf-8") as f:
    f.write(text)
EOF

# Package grouping (B2): every distribution that is not OpenTelemetry's or the ADOT distro's moves to <payload>/deps,
# which the bootstrap (patch 0005) puts at the end of sys.path, behind the application's site-packages. The
# OpenTelemetry group stays at the payload root, where the operator's PYTHONPATH puts it, ahead of them. Top-level
# entries move whole, so that files a RECORD does not list (compiled caches) go with their package; an entry that
# belongs to both groups cannot be split and fails the build.
python3 - "$payload" <<'EOF'
import os, re, shutil, sys
payload = sys.argv[1]
deps = os.path.join(payload, "deps")
owners = {}
for entry in sorted(os.listdir(payload)):
    if not entry.endswith(".dist-info"):
        continue
    name = None
    with open(os.path.join(payload, entry, "METADATA"), encoding="utf-8") as f:
        for line in f:
            if line.startswith("Name: "):
                name = line[len("Name: "):].strip()
                break
            if not line.strip():
                break
    if not name:
        sys.exit("%s: no Name in METADATA" % entry)
    normalized = re.sub(r"[-_.]+", "-", name).lower()
    group = "otel" if normalized.startswith("opentelemetry-") or normalized == "aws-opentelemetry-distro" else "deps"
    record = os.path.join(payload, entry, "RECORD")
    if not os.path.isfile(record):
        sys.exit("%s: no RECORD, so its files cannot be grouped" % entry)
    tops = {entry}
    with open(record, encoding="utf-8") as f:
        for line in f:
            path = line.rsplit(",", 2)[0].strip()
            # Scripts are recorded outside the payload (../../bin/...); they stay where pip put them.
            if path and not path.startswith("..") and not os.path.isabs(path):
                tops.add(path.split("/")[0])
    for top in tops:
        owners.setdefault(top, set()).add(group)
shared = sorted(top for top, groups in owners.items() if len(groups) > 1)
if shared:
    sys.exit("cannot group the payload: %s belong to both OpenTelemetry and general distributions" % ", ".join(shared))
moving = sorted(top for top, groups in owners.items() if groups == {"deps"} and os.path.lexists(os.path.join(payload, top)))
if moving:
    os.makedirs(deps, exist_ok=True)
for top in moving:
    target = os.path.join(deps, top)
    if os.path.lexists(target):
        sys.exit("%s: already exists" % target)
    shutil.move(os.path.join(payload, top), target)
EOF

install -m 0644 "$work/sitecustomize.py" "$payload/_otel_packaging_sitecustomize.py"
install -m 0644 "$here/adot_sitecustomize.py" "$auto_instrumentation/sitecustomize.py"

# The hooks patch 0004 calls, with the payload's ADOT distro version written into the skip record's version field.
python3 - "$here/adot_hooks.py" "$payload" "$work/adot_hooks.py" <<'EOF'
import os, re, sys
source, payload, target = sys.argv[1:]
versions = [entry[len("aws_opentelemetry_distro-"):-len(".dist-info")] for entry in os.listdir(payload)
            if entry.startswith("aws_opentelemetry_distro-") and entry.endswith(".dist-info")]
if len(versions) != 1:
    sys.exit("%s: expected exactly one aws_opentelemetry_distro .dist-info, found %d" % (payload, len(versions)))
with open(source, encoding="utf-8") as f:
    text = f.read()
pattern = re.compile(r'^SDK_VERSION = "[^"]*"  # sdk-version$', re.MULTILINE)
if len(pattern.findall(text)) != 1:
    sys.exit("%s: expected exactly one sdk-version marker" % source)
text = pattern.sub('SDK_VERSION = "%s"  # sdk-version' % versions[0], text)
with open(target, "w", encoding="utf-8") as f:
    f.write(text)
EOF
install -m 0644 "$work/adot_hooks.py" "$payload/_adot_hooks.py"

# Every distribution left at the payload root is listed: after the grouping above, those are the OpenTelemetry
# distributions, whose copies must be the ones in effect because the SDK pins the exact API version it was built
# with. General-purpose libraries are in deps/ and not checked, so this check does not change which applications are
# instrumented.
python3 - "$payload" "$SUPPORTED_PYTHON_MINORS" <<'EOF'
import os, sys
payload, minors = sys.argv[1], sys.argv[2].split()
pins = {}
for entry in os.listdir(payload):
    if not entry.endswith(".dist-info"):
        continue
    name = version = None
    try:
        with open(os.path.join(payload, entry, "METADATA"), encoding="utf-8") as f:
            for line in f:
                if line.startswith("Name: "):
                    name = line[len("Name: "):].strip()
                elif line.startswith("Version: "):
                    version = line[len("Version: "):].strip()
                elif not line.strip():
                    break
    except OSError:
        continue
    if name and version:
        pins[name.lower()] = "%s==%s" % (name, version)
if not pins:
    sys.exit("%s: no OpenTelemetry distributions found" % payload)
content = "# Generated by tools/otel-packaging-python/build.sh: the bundle's OpenTelemetry distributions.\n"
content += "".join(pins[name] + "\n" for name in sorted(pins))
for minor in minors:
    directory = os.path.join(payload, "cp3%s" % minor)
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "all-dependencies.txt"), "w", encoding="utf-8") as f:
        f.write(content)
EOF
