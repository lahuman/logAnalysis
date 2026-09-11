"""Relocatable entrypoint; runtime checks and config validation never use the network."""

import argparse
from contextlib import contextmanager
import hashlib
import importlib
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

    def __init__(self, reason: str, **details):
        super().__init__(reason)
        self.details = details


def failure_details(exc: Exception, **context) -> dict:
    """Report exception metadata without messages, source text or local values."""
    details = {"error_type": type(exc).__name__, **context}
    reason = "check configuration, file permissions and runtime; see README.md"
    hint = "Check the reported stage and location; exception messages are omitted to protect credentials."
    if isinstance(exc, PreflightError):
        return {**details, "reason": str(exc), "hint": hint, **exc.details}
    if isinstance(exc, FileNotFoundError):
        reason = "required file or directory was not found"
        hint = "Check the path and symlink target; restore missing bundle files from the matching archive."
    elif isinstance(exc, PermissionError):
        reason = "permission denied"
        hint = "Check the execution account, parent directory access and file permissions."
        if context.get("operation") == "execute":
            hint += " Check execute permission, noexec mounts and SELinux policy."
        elif context.get("operation") in {"write", "lock"}:
            hint += " Check data directory write access and filesystem restrictions."
    elif isinstance(exc, ModuleNotFoundError):
        reason = "required Python module was not found"
        hint = "Run ./log-analyzer; check src/, app/ and runtime/python/ for missing files."
    elif isinstance(exc, ImportError):
        reason = "Python module could not be imported"
        hint = "Check the module path, source imports and native library compatibility; use the matching bundle runtime."
    elif isinstance(exc, OSError):
        reason = "operating system operation failed"
        hint = "Check the reported path, errno, filesystem and runtime compatibility."
    elif isinstance(exc, subprocess.CalledProcessError):
        reason = "runtime command failed"
        hint = "Check the executable at the reported path and its native library compatibility."
        details["returncode"] = exc.returncode
    elif isinstance(exc, subprocess.TimeoutExpired):
        reason = "runtime check timed out"
        hint = "Check whether source imports block or perform work at module load time."
    elif isinstance(exc, json.JSONDecodeError):
        reason = "invalid JSON file"
        hint = "Restore manifest.json from the matching bundle archive."
        details.update(line=exc.lineno, column=exc.colno)
    if isinstance(exc, OSError):
        if exc.filename is not None:
            details["path"] = os.fsdecode(exc.filename)
        if exc.errno is not None:
            details.update(errno=exc.errno, os_error=os.strerror(exc.errno))
    if isinstance(exc, ImportError):
        if exc.name:
            details["module"] = exc.name
        if exc.path:
            details["path"] = exc.path
    trace = exc.__traceback__
    if trace is not None:
        while trace.tb_next is not None:
            trace = trace.tb_next
        details["location"] = {"file": trace.tb_frame.f_code.co_filename,
                               "line": trace.tb_lineno, "function": trace.tb_frame.f_code.co_name}
    return {**details, "reason": reason, "hint": hint}


@contextmanager
def diagnostic_step(stage: str, *, operation: str, path: Path | None = None, **context):
    context.update(stage=stage, operation=operation)
    if path is not None:
        context["path"] = str(path)
    try:
        yield
    except Exception as exc:
        details = failure_details(exc, **context)
        reason = details.pop("reason")
        raise PreflightError(reason, **details) from exc


def verify_bundle() -> None:
    with diagnostic_step("manifest", operation="read", path=ROOT / "manifest.json"):
        manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
        files = manifest["files"].items()
    for name, expected in files:
        path = ROOT / name
        with diagnostic_step("integrity", operation="read", path=path):
            if path.is_symlink():
                actual = "symlink:" + os.readlink(path)
            else:
                actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != expected:
                raise PreflightError(f"bundle file mismatch: {name}",
                                     hint="Restore this file from the matching archive; check transfer and line-ending changes.")


def check_editable_sources() -> None:
    errors = []
    paths = sorted((ROOT / "src/log_analyzer").rglob("*.py")) + sorted((ROOT / "tests").rglob("*.py"))
    for path in paths:
        try:
            with diagnostic_step("sources", operation="read", path=path):
                try:
                    compile(path.read_bytes(), str(path), "exec")
                except SyntaxError as exc:
                    raise PreflightError(f"Python syntax error: {path.relative_to(ROOT)}:{exc.lineno}",
                                         line=exc.lineno, hint="Correct the Python syntax at the reported line.") from None
        except PreflightError as exc:
            errors.append(failure_details(exc))
    if errors:
        details = errors[0].copy()
        reason = details.pop("reason")
        raise PreflightError(reason, **details, errors=errors, error_count=len(errors))


def check_source_imports() -> str:
    # A fresh interpreter must see edits even when doctor is called before tests.
    script = '''
import contextlib, importlib, io, json, runpy, sys
launcher = runpy.run_path(sys.argv[1])
try:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        for name in ("log_analyzer", "log_analyzer.config", "log_analyzer.report", "log_analyzer.cli", "log_analyzer.llm_smoke"):
            with launcher["diagnostic_step"]("imports", operation="import", module=name):
                importlib.import_module(name)
        with launcher["diagnostic_step"]("imports", operation="import", module="log_analyzer"):
            from log_analyzer.config import load_config
            from log_analyzer.report import ReportWriter
            from log_analyzer import __version__
    print(json.dumps({"application": __version__}))
except Exception as exc:
    print(json.dumps(launcher["failure_details"](exc, stage="imports")))
    sys.exit(2)
'''
    with diagnostic_step("imports", operation="execute", path=ROOT / "launch.py"):
        result = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(ROOT / "launch.py")],
                                cwd=ROOT, text=True, encoding="utf-8", capture_output=True, timeout=30)
        if result.returncode not in (0, 2):
            raise subprocess.CalledProcessError(result.returncode, result.args)
        try:
            details = json.loads(result.stdout)
        except json.JSONDecodeError:
            raise PreflightError("source import check did not return a diagnostic",
                                 hint="Check launch.py and the selected Python runtime; child output is omitted.") from None
        if result.returncode:
            reason = details.pop("reason")
            raise PreflightError(reason, **details)
        return details["application"]


def check_runtime() -> dict:
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise PreflightError("Linux x86_64 is required")
    libc, version = platform.libc_ver()
    if libc != "glibc" or tuple(map(int, version.split(".")[:2])) < (2, 28):
        raise PreflightError(f"glibc >= 2.28 (RHEL 8) is required; detected {libc} {version}")
    verify_bundle()
    for module in ("aiohttp", "elasticsearch", "httpx", "pydantic_core", "oracledb",
                   "sqlite3", "ssl", "fcntl", "cryptography.hazmat.primitives.hashes"):
        with diagnostic_step("imports", operation="import", module=module):
            importlib.import_module(module)
    with diagnostic_step("imports", operation="initialize", module="cryptography.hazmat.primitives.hashes"):
        from cryptography.hazmat.primitives import hashes
        hashes.Hash(hashes.SHA256()).finalize()
    import sqlite3
    import ssl
    import fcntl

    with diagnostic_step("git", operation="execute", path=ROOT / "runtime/git/bin/git"):
        git = subprocess.check_output([str(ROOT / "runtime/git/bin/git"), "--version"],
                                      text=True, stderr=subprocess.PIPE).strip()
    data = ROOT / "data"
    import tempfile
    with diagnostic_step("data", operation="write", path=data):
        data.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=data) as work:
            with diagnostic_step("sqlite", operation="write", path=Path(work) / "check.db"):
                with sqlite3.connect(str(Path(work) / "check.db")) as connection:
                    connection.execute("CREATE TABLE runtime_check (id INTEGER)")
            with diagnostic_step("lock", operation="lock", path=Path(work) / "lock"):
                with (Path(work) / "lock").open("w") as stream:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return {"glibc": version, "git": git, "openssl": ssl.OPENSSL_VERSION}


def doctor(scope: str = "all") -> None:
    if sys.version_info[:3] != (3, 11, 8):
        raise PreflightError("Python 3.11.8 is required; use the bundled runtime")
    details = {}
    if scope in ("all", "source"):
        check_editable_sources()
    if scope in ("all", "runtime"):
        details.update(check_runtime())
    if scope in ("all", "source"):
        details["application"] = check_source_imports()
    print(json.dumps({"event": "offline_doctor_succeeded", "python": platform.python_version(),
                      "scope": scope, "network_used": False, **details}))


def main() -> int:
    parser = argparse.ArgumentParser(description="중요망 로그 분석기 (Python 3.11.8 포함)")
    parser.add_argument("command", choices=("doctor", "test", "check-config", "smoke", "healthcheck", "run"))
    parser.add_argument("--config", type=Path, default=Path("config/config.toml"))
    parser.add_argument("--scope", choices=("all", "source", "runtime"),
                        help="doctor only: all checks (default), source syntax/imports, or runtime checks")
    parser.add_argument("--pattern", help="test only: test filename pattern (default: test*.py)")
    args = parser.parse_args()
    if args.scope is not None and args.command != "doctor":
        parser.error("--scope is only supported by doctor")
    if args.pattern is not None:
        if args.command != "test":
            parser.error("--pattern is only supported by test")
        if not args.pattern or "/" in args.pattern or "\\" in args.pattern:
            parser.error("--pattern must be a filename pattern, such as 'test_file_source.py'")
    try:
        if sys.version_info[:3] != (3, 11, 8):
            raise PreflightError("Python 3.11.8 is required; use the bundled runtime")
        if args.command == "doctor":
            doctor(args.scope or "all")
            return 0
        if args.command == "test":
            doctor()
            import unittest
            sys.path.insert(0, str(ROOT))
            with diagnostic_step("tests", operation="discover", path=ROOT / "tests"):
                suite = unittest.TestLoader().discover(str(ROOT / "tests"), pattern=args.pattern or "test*.py")
                if suite.countTestCases() == 0:
                    raise PreflightError("no tests matched the filename pattern", pattern=args.pattern or "test*.py",
                                         hint="Check the test filename and quote wildcard patterns, for example 'test_file*.py'.")
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
        print(json.dumps({"event": "offline_preflight_failed", "command": args.command,
                          "bundle_root": str(ROOT), "python": sys.executable,
                          **({"scope": args.scope or "all"} if args.command == "doctor" else {}),
                          **failure_details(exc, stage=args.command)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
