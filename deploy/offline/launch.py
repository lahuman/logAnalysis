"""Relocatable entrypoint; runtime checks and config validation never use the network."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "app")]


class PreflightError(ValueError):
    """A local diagnostic containing no configuration values or credentials."""


def verify_bundle() -> None:
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    for name, expected in manifest["files"].items():
        path = ROOT / name
        if path.is_symlink():
            actual = "symlink:" + os.readlink(path)
        else:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise PreflightError(f"bundle file mismatch: {name}")


def check_editable_sources() -> None:
    for path in sorted((ROOT / "src/log_analyzer").rglob("*.py")):
        try:
            compile(path.read_bytes(), str(path), "exec")
        except SyntaxError as exc:
            raise PreflightError(f"Python syntax error: {path.relative_to(ROOT)}:{exc.lineno}") from None


def doctor() -> None:
    if sys.version_info[:3] != (3, 11, 8):
        raise PreflightError("Python 3.11.8 is required; use the bundled runtime")
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise PreflightError("Linux x86_64 is required")
    libc, version = platform.libc_ver()
    if libc != "glibc" or tuple(map(int, version.split(".")[:2])) < (2, 28):
        raise PreflightError(f"glibc >= 2.28 (RHEL 8) is required; detected {libc} {version}")
    verify_bundle()
    check_editable_sources()
    import aiohttp
    import elasticsearch
    import httpx
    import pydantic_core
    import oracledb
    import sqlite3
    import ssl
    import fcntl
    import log_analyzer
    from cryptography.hazmat.primitives import hashes
    hashes.Hash(hashes.SHA256()).finalize()
    from log_analyzer.config import load_config
    from log_analyzer.report import ReportWriter

    git = subprocess.check_output([str(ROOT / "runtime/git/bin/git"), "--version"], text=True).strip()
    data = ROOT / "data"
    data.mkdir(exist_ok=True)
    import tempfile
    with tempfile.TemporaryDirectory(dir=data) as work:
        with sqlite3.connect(str(Path(work) / "check.db")) as connection:
            connection.execute("CREATE TABLE runtime_check (id INTEGER)")
        with (Path(work) / "lock").open("w") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    print(json.dumps({"event": "offline_doctor_succeeded", "python": platform.python_version(),
                      "application": log_analyzer.__version__, "glibc": version,
                      "git": git, "openssl": ssl.OPENSSL_VERSION, "network_used": False}))


def main() -> int:
    parser = argparse.ArgumentParser(description="중요망 로그 분석기 (Python 3.11.8 포함)")
    parser.add_argument("command", choices=("doctor", "test", "check-config", "smoke", "healthcheck", "run"))
    parser.add_argument("--config", type=Path, default=Path("config/config.toml"))
    args = parser.parse_args()
    try:
        if sys.version_info[:3] != (3, 11, 8):
            raise PreflightError("Python 3.11.8 is required; use the bundled runtime")
        if args.command == "doctor":
            doctor()
            return 0
        if args.command == "test":
            doctor()
            import unittest
            sys.path.insert(0, str(ROOT))
            suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
            return 0 if unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful() else 1
        from log_analyzer.config import load_config
        config = load_config(args.config)
        if config.openai.provider != "onprem":
            raise PreflightError("this bundle requires provider=onprem; cloud providers are disabled")
        if "replace-with-" in config.openai.model:
            raise PreflightError("set the internal model ID in config/config.toml")
        if args.command == "check-config":
            print(json.dumps({"event": "offline_configuration_valid", "network_used": False}))
            return 0
        if args.command == "smoke":
            from log_analyzer.llm_smoke import main as smoke_main
            return smoke_main(["--config", str(args.config), "--output-directory", "data/smoke-reports"])
        from log_analyzer.cli import main as batch_main
        return batch_main([args.command, "--config", str(args.config)])
    except Exception as exc:
        # Avoid echoing configuration validation details which may include secrets.
        reason = str(exc) if isinstance(exc, PreflightError) else "check configuration, file permissions and runtime; see README.md"
        print(json.dumps({"event": "offline_preflight_failed", "error_type": type(exc).__name__, "reason": reason}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
