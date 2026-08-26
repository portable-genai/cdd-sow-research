"""The promotion script's signing guard, executed rather than grepped.

``--cosign-key`` accepts two forms that fail in opposite directions if the guard is wrong. A
FILE must exist, because a path that does not resolve means the operator believes they are
signing with a key they do not have. A KMS URI must NOT be file-checked, because no such file
exists by design — the private key never leaves the KMS — and a ``test -f`` applied to it
refuses the strongest of the three signing options for failing a test that does not describe
it.

These run the real script against a stub PATH rather than reading it for strings. A grep would
have passed against the pre-change file for the URI case, since the ``--key`` line it looks for
was already there; what was wrong sat one line above it, in a check that never ran until push
time. So the assertion here is on what ``cosign`` was actually invoked with.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = Path("scripts/promote_production_images.sh").resolve()
PREFIX = "registry.example/proj/repo"
KMS_URI = "gcpkms://projects/p/locations/l/keyRings/r/cryptoKeys/k"

_STUBS = {
    # Answers `inspect --format` with something the digest greps can match, and records every
    # other call. `imagetools` arrives as `docker buildx imagetools`, which this also absorbs.
    "docker": f"""#!/usr/bin/env bash
echo "docker $*" >> "$STUB_LOG"
case "$1" in
  inspect)
    case "$*" in
      *RepoDigests*) echo "{PREFIX}/doc1-api@sha256:{"a" * 64}"
                     echo "{PREFIX}/doc1-ui@sha256:{"b" * 64}" ;;
      *)             echo "sha256:{"c" * 64}" ;;
    esac ;;
esac
exit 0
""",
    "npm": '#!/usr/bin/env bash\necho "npm $*" >> "$STUB_LOG"\nexit 0\n',
    "trivy": '#!/usr/bin/env bash\necho "trivy $*" >> "$STUB_LOG"\nexit 0\n',
    "cosign": '#!/usr/bin/env bash\necho "cosign $*" >> "$STUB_LOG"\nexit 0\n',
}


def _run(tmp_path: Path, cosign_key: str) -> tuple[subprocess.CompletedProcess[str], str]:
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    for name, body in _STUBS.items():
        stub = stub_dir / name
        stub.write_text(body)
        stub.chmod(0o755)

    log = tmp_path / "calls.log"
    log.touch()
    env = dict(os.environ)
    env["PATH"] = f"{stub_dir}:{env['PATH']}"
    env["STUB_LOG"] = str(log)

    completed = subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "--image-prefix",
            PREFIX,
            "--tag",
            "0123456abcdef",
            "--sign",
            "--cosign-key",
            cosign_key,
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=SCRIPT.parent.parent,
    )
    return completed, log.read_text()


def test_a_kms_uri_signs_and_is_never_treated_as_a_file(tmp_path: Path) -> None:
    completed, calls = _run(tmp_path, KMS_URI)

    assert completed.returncode == 0, completed.stderr
    signed = [line for line in calls.splitlines() if line.startswith("cosign sign")]
    assert len(signed) == 2, calls
    assert all(f"--key {KMS_URI}" in line for line in signed), signed
    # The URI must reach cosign whole. A guard that stripped the scheme to make `test -f`
    # pass would sign with a relative path that resolves to nothing.
    assert all("gcpkms://projects/p/" in line for line in signed), signed


def test_a_file_key_that_does_not_exist_still_refuses(tmp_path: Path) -> None:
    missing = tmp_path / "absent-cosign.key"
    completed, calls = _run(tmp_path, str(missing))

    assert completed.returncode != 0
    assert "cosign sign" not in calls, "an unresolvable key file must never reach a signature"


def test_keyless_stays_the_default_when_no_key_is_named(tmp_path: Path) -> None:
    completed, calls = _run(tmp_path, "")

    assert completed.returncode == 0, completed.stderr
    signed = [line for line in calls.splitlines() if line.startswith("cosign sign")]
    assert len(signed) == 2, calls
    assert all("--key" not in line for line in signed), signed


def test_an_unsigned_push_is_refused_before_anything_is_built(tmp_path: Path) -> None:
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    for name, body in _STUBS.items():
        stub = stub_dir / name
        stub.write_text(body)
        stub.chmod(0o755)
    log = tmp_path / "calls.log"
    log.touch()
    env = dict(os.environ)
    env["PATH"] = f"{stub_dir}:{env['PATH']}"
    env["STUB_LOG"] = str(log)

    completed = subprocess.run(
        ["bash", str(SCRIPT), "--image-prefix", PREFIX, "--tag", "0123456abcdef", "--push"],
        capture_output=True,
        text=True,
        env=env,
        cwd=SCRIPT.parent.parent,
    )

    assert completed.returncode == 2
    assert "unsigned release promotion is refused" in completed.stderr
    assert log.read_text() == "", "the refusal must precede the build, not follow it"


def test_a_promotion_build_never_reuses_a_cached_security_layer(tmp_path: Path) -> None:
    """The 2026-08-26 promotion shipped openssl 3.5.6 with 3.5.7 available and fixed.

    The runtime stage runs `apt-get update && apt-get upgrade -y` and says in place that a
    digest pin freezes unpatched packages, so security updates are applied on top. That is
    true of the instruction and false of the image: the base digest had not changed, so the
    layer was served from the build cache and the image carried the patch level of whenever
    that cache was last warmed. `#13 [runtime 3/8] ... CACHED` in the build log, and
    `dpkg-query` inside the built image, are what proved it — the blocking trivy pass then
    refused the promotion, which is the one part of this that worked as designed.

    Reproducibility is what the digest pin buys; a promotion build is exactly where it must
    not also buy staleness.
    """
    completed, calls = _run(tmp_path, KMS_URI)

    assert completed.returncode == 0, completed.stderr
    # `docker buildx imagetools` also starts with "docker build"; the image builds are the
    # two that carry --platform.
    builds = [line for line in calls.splitlines() if line.startswith("docker build --platform")]
    assert len(builds) == 2, calls
    assert all("--no-cache" in line for line in builds), builds
