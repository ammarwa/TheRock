#!/usr/bin/env python
# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""Provision local Rust (cargo) and Node.js toolchains for building mirage.

The mirage emulator UX is a Rust workspace whose dashboard build step
(``dashboard/build.rs``) shells out to ``node``/``npm``. On systems that
already provide a suitable cargo and Node.js (>= 20.19) on PATH this script is
effectively a no-op: it simply emits a small ``cargo`` wrapper that points at
the system toolchains. When either toolchain is missing (or Node.js is too
old), the missing piece is downloaded into a build-local directory so mirage
can be built without modifying the host system.

The script writes a wrapper executable (``--wrapper``) that prepends the
resolved Node.js and cargo ``bin`` directories to ``PATH`` and then execs the
real cargo. Point ``-DMIRAGE_CARGO=<wrapper>`` at it so every cargo build/test
invocation can find node/npm.

Usage:
    python provision_mirage_toolchains.py \
        --toolchain-dir build/emulation/mirage-toolchains \
        --wrapper build/emulation/mirage-toolchains/bin/cargo

This is Linux-only, matching mirage's supported platforms.
"""

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

# Node.js version downloaded when the host lacks a suitable runtime. This is a
# current LTS release that satisfies the dashboard's Vite 8 / React 19
# toolchain requirement (Node >= 20.19).
DEFAULT_NODE_VERSION = "22.12.0"

# Minimum (major, minor) Node.js version accepted from the host PATH. Mirrors
# the check in dashboard/build.rs.
MIN_NODE_VERSION = (20, 19)

# Official rustup bootstrap installer.
RUSTUP_INIT_URL = "https://sh.rustup.rs"


def log(message: str) -> None:
    print(f"[provision-mirage-toolchains] {message}", flush=True)


def parse_node_version(version: str) -> tuple[int, int] | None:
    """Parse a ``major.minor[.patch]`` string into ``(major, minor)``."""
    raw = version.strip().lstrip("v")
    parts = raw.split(".")
    if len(parts) < 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def node_arch() -> str:
    """Map the host machine to a Node.js distribution architecture."""
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "x64"
    if machine in ("aarch64", "arm64"):
        return "arm64"
    raise SystemExit(
        f"Unsupported architecture for local Node.js install: {platform.machine()!r}. "
        "Install Node.js >= 20.19 and npm on PATH manually."
    )


def download(url: str, dest: Path) -> None:
    log(f"Downloading {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url) as response, open(dest, "wb") as out:
        shutil.copyfileobj(response, out)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def detect_system_cargo() -> Path | None:
    """Return the path to a usable system cargo, or None."""
    cargo = shutil.which("cargo")
    rustc = shutil.which("rustc")
    if cargo and rustc:
        return Path(cargo)
    return None


def detect_system_node() -> Path | None:
    """Return the bin directory of a usable system Node.js, or None."""
    node = shutil.which("node")
    npm = shutil.which("npm")
    if not node or not npm:
        return None
    try:
        result = subprocess.run(
            [node, "--version"], capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    parsed = parse_node_version(result.stdout)
    if parsed is None or parsed < MIN_NODE_VERSION:
        return None
    # Use the directory as found on PATH without resolving symlinks: this keeps
    # the bundled npm next to node.
    return Path(node).parent


def ensure_cargo(toolchain_dir: Path) -> tuple[Path, dict[str, str]]:
    """Resolve cargo, installing a local Rust toolchain if necessary.

    Returns ``(cargo_path, env)`` where ``env`` is a mapping of environment
    variables the wrapper must export so the cargo invocation works. For a
    locally provisioned rustup toolchain this carries ``CARGO_HOME`` and
    ``RUSTUP_HOME``; the proxy ``cargo`` binary resolves its toolchain via
    ``RUSTUP_HOME`` at runtime, so without these it fails with "rustup could
    not choose a version of cargo to run". For a system cargo it is empty.
    """
    system_cargo = detect_system_cargo()
    if system_cargo is not None:
        log(f"Using system cargo at {system_cargo}")
        return system_cargo, {}

    cargo_home = toolchain_dir / "cargo"
    rustup_home = toolchain_dir / "rustup"
    cargo_bin = cargo_home / "bin" / "cargo"
    local_env = {
        "CARGO_HOME": str(cargo_home),
        "RUSTUP_HOME": str(rustup_home),
    }
    if cargo_bin.exists():
        log(f"Using previously provisioned cargo at {cargo_bin}")
        return cargo_bin, local_env

    log("cargo not found on PATH; installing a local Rust toolchain via rustup")
    env = os.environ.copy()
    env.update(local_env)
    with tempfile.TemporaryDirectory() as tmp:
        installer = Path(tmp) / "rustup-init.sh"
        download(RUSTUP_INIT_URL, installer)
        subprocess.run(
            [
                "sh",
                str(installer),
                "-y",
                "--no-modify-path",
                "--profile",
                "minimal",
                "--default-toolchain",
                "stable",
            ],
            env=env,
            check=True,
        )
    if not cargo_bin.exists():
        raise SystemExit(f"rustup did not produce a cargo binary at {cargo_bin}")
    log(f"Installed local cargo at {cargo_bin}")
    return cargo_bin, local_env


def ensure_node(toolchain_dir: Path, node_version: str) -> Path:
    """Resolve Node.js, downloading a local distribution if necessary.

    Returns the bin directory containing ``node`` and ``npm``.
    """
    system_node_bin = detect_system_node()
    if system_node_bin is not None:
        log(f"Using system Node.js from {system_node_bin}")
        return system_node_bin

    arch = node_arch()
    dist_name = f"node-v{node_version}-linux-{arch}"
    node_root = toolchain_dir / dist_name
    node_bin = node_root / "bin"
    if (node_bin / "node").exists() and (node_bin / "npm").exists():
        log(f"Using previously provisioned Node.js from {node_bin}")
        return node_bin

    log(
        f"Node.js >= {MIN_NODE_VERSION[0]}.{MIN_NODE_VERSION[1]} with npm not found "
        f"on PATH; downloading Node.js {node_version} ({arch})"
    )
    base_url = f"https://nodejs.org/dist/v{node_version}"
    archive_name = f"{dist_name}.tar.xz"
    with tempfile.TemporaryDirectory() as tmp:
        archive_path = Path(tmp) / archive_name
        download(f"{base_url}/{archive_name}", archive_path)

        shasums_path = Path(tmp) / "SHASUMS256.txt"
        download(f"{base_url}/SHASUMS256.txt", shasums_path)
        expected = _expected_sha256(shasums_path, archive_name)
        actual = sha256_of(archive_path)
        if actual != expected:
            raise SystemExit(
                f"SHA-256 mismatch for {archive_name}: expected {expected}, got {actual}"
            )

        toolchain_dir.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive_path, "r:xz") as tar:
            _safe_extract(tar, toolchain_dir)

    if not (node_bin / "node").exists():
        raise SystemExit(f"Node.js archive did not produce {node_bin / 'node'}")
    log(f"Installed local Node.js at {node_bin}")
    return node_bin


def _expected_sha256(shasums_path: Path, archive_name: str) -> str:
    for line in shasums_path.read_text().splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == archive_name:
            return parts[0]
    raise SystemExit(f"Could not find {archive_name} in SHASUMS256.txt")


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """Extract a tar archive, rejecting path traversal entries."""
    dest = dest.resolve()
    for member in tar.getmembers():
        member_path = (dest / member.name).resolve()
        if not str(member_path).startswith(str(dest) + os.sep) and member_path != dest:
            raise SystemExit(f"Refusing to extract unsafe path: {member.name}")
    # ``filter="data"`` applies the safe extraction rules (and silences the
    # Python 3.14 deprecation warning); fall back for older runtimes.
    try:
        tar.extractall(dest, filter="data")
    except TypeError:
        tar.extractall(dest)


def write_wrapper(
    wrapper: Path,
    node_bin: Path,
    cargo_bin: Path,
    real_cargo: Path,
    cargo_env: dict[str, str],
) -> None:
    """Write an executable cargo wrapper that puts node/cargo on PATH."""
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    # Export any toolchain env (e.g. CARGO_HOME/RUSTUP_HOME for a locally
    # provisioned rustup) so the rustup proxy can resolve its toolchain.
    env_lines = "".join(
        f'export {key}="{value}"\n' for key, value in sorted(cargo_env.items())
    )
    contents = (
        "#!/usr/bin/env bash\n"
        "# Auto-generated by provision_mirage_toolchains.py. Do not edit.\n"
        "# Prepends the provisioned Node.js and cargo bin directories to PATH so\n"
        "# that cargo (and the dashboard build.rs npm step) resolve correctly.\n"
        "set -euo pipefail\n"
        f'export PATH="{node_bin}:{cargo_bin}:${{PATH}}"\n'
        f"{env_lines}"
        f'exec "{real_cargo}" "$@"\n'
    )
    wrapper.write_text(contents)
    wrapper.chmod(0o755)
    log(f"Wrote cargo wrapper to {wrapper}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--toolchain-dir",
        type=Path,
        required=True,
        help="Directory to install locally provisioned toolchains into.",
    )
    parser.add_argument(
        "--wrapper",
        type=Path,
        required=True,
        help="Path of the cargo wrapper executable to emit.",
    )
    parser.add_argument(
        "--node-version",
        default=DEFAULT_NODE_VERSION,
        help=f"Node.js version to download when missing (default: {DEFAULT_NODE_VERSION}).",
    )
    args = parser.parse_args(argv)

    if sys.platform.startswith("win"):
        raise SystemExit("provision_mirage_toolchains.py is only supported on Linux.")

    toolchain_dir = args.toolchain_dir.resolve()
    toolchain_dir.mkdir(parents=True, exist_ok=True)

    # Do not resolve() the cargo path: when cargo is a rustup proxy, `cargo` is
    # a symlink to `rustup` that dispatches based on argv[0]. Resolving it would
    # turn `cargo` into `rustup` and break the build.
    cargo, cargo_env = ensure_cargo(toolchain_dir)
    node_bin = ensure_node(toolchain_dir, args.node_version)

    write_wrapper(args.wrapper.resolve(), node_bin, cargo.parent, cargo, cargo_env)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
