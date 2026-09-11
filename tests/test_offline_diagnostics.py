from __future__ import annotations

from contextlib import redirect_stderr
import errno
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tests.test_offline_compatibility import load_script


class OfflineDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.launch = load_script("launch")
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.root = Path(work.name)
        (self.root / "src/log_analyzer").mkdir(parents=True)
        self.write_manifest({})

    def write_manifest(self, files):
        (self.root / "manifest.json").write_text(json.dumps({"files": files}), encoding="utf-8")

    def run_doctor(self):
        output = io.StringIO()
        with (patch.object(self.launch, "ROOT", self.root),
              patch.object(self.launch.sys, "argv", ["launch.py", "doctor"]),
              patch.object(self.launch.sys, "version_info", (3, 11, 8)),
              patch.object(self.launch.platform, "system", return_value="Linux"),
              patch.object(self.launch.platform, "machine", return_value="x86_64"),
              patch.object(self.launch.platform, "libc_ver", return_value=("glibc", "2.28")),
              redirect_stderr(output)):
            self.assertEqual(self.launch.main(), 2)
        record = json.loads(output.getvalue())
        self.assertEqual(record["event"], "offline_preflight_failed")
        self.assertEqual(record["command"], "doctor")
        self.assertEqual(record["bundle_root"], str(self.root))
        self.assertTrue(record["hint"])
        return record

    def test_missing_manifest_reports_path_and_read_stage(self):
        (self.root / "manifest.json").unlink()
        record = self.run_doctor()
        self.assertEqual(record["error_type"], "FileNotFoundError")
        self.assertEqual(record["stage"], "manifest")
        self.assertEqual(record["operation"], "read")
        self.assertEqual(record["path"], str(self.root / "manifest.json"))
        self.assertEqual(record["errno"], errno.ENOENT)
        self.assertGreater(record["location"]["line"], 0)

    def test_invalid_manifest_does_not_echo_document_contents(self):
        (self.root / "manifest.json").write_text('{"password": secret-value}', encoding="utf-8")
        record = self.run_doctor()
        self.assertEqual(record["error_type"], "JSONDecodeError")
        self.assertEqual(record["stage"], "manifest")
        self.assertEqual(record["line"], 1)
        self.assertNotIn("secret-value", json.dumps(record))

    def test_missing_bundle_dependency_reports_exact_path(self):
        self.write_manifest({"app/dependency.py": "unused"})
        record = self.run_doctor()
        self.assertEqual(record["error_type"], "FileNotFoundError")
        self.assertEqual(record["stage"], "integrity")
        self.assertEqual(record["path"], str(self.root / "app/dependency.py"))

    def test_bundle_permission_error_preserves_errno_without_exception_message(self):
        target = self.root / "runtime/python/lib/library.py"
        self.write_manifest({"runtime/python/lib/library.py": "unused"})
        with patch.object(Path, "read_bytes", side_effect=PermissionError(errno.EACCES, "secret-value", str(target))):
            record = self.run_doctor()
        self.assertEqual(record["error_type"], "PermissionError")
        self.assertEqual(record["stage"], "integrity")
        self.assertEqual(record["path"], str(target))
        self.assertEqual(record["errno"], errno.EACCES)
        self.assertNotIn("secret-value", json.dumps(record))

    def test_checksum_mismatch_keeps_existing_reason(self):
        (self.root / "library.py").write_text("changed", encoding="utf-8")
        self.write_manifest({"library.py": "original-checksum"})
        record = self.run_doctor()
        self.assertEqual(record["reason"], "bundle file mismatch: library.py")
        self.assertEqual(record["stage"], "integrity")
        self.assertEqual(record["path"], str(self.root / "library.py"))

    def test_syntax_error_has_line_without_source_text(self):
        (self.root / "src/log_analyzer/example.py").write_text('password = "secret-value" +\n', encoding="utf-8")
        record = self.run_doctor()
        self.assertEqual(record["stage"], "sources")
        self.assertEqual(record["line"], 1)
        self.assertIn("Python syntax error:", record["reason"])
        self.assertNotIn("secret-value", json.dumps(record))

    def test_missing_transitive_import_identifies_missing_module(self):
        with patch.object(self.launch.importlib, "import_module",
                          side_effect=ModuleNotFoundError("secret-value", name="missing_dependency")):
            record = self.run_doctor()
        self.assertEqual(record["error_type"], "ModuleNotFoundError")
        self.assertEqual(record["stage"], "imports")
        self.assertEqual(record["module"], "missing_dependency")
        self.assertNotIn("secret-value", json.dumps(record))

    def test_native_import_error_reports_module_and_path(self):
        target = str(self.root / "app/native.so")
        with patch.object(self.launch.importlib, "import_module",
                          side_effect=ImportError("secret-value", name="native", path=target)):
            record = self.run_doctor()
        self.assertEqual(record["error_type"], "ImportError")
        self.assertEqual(record["module"], "native")
        self.assertEqual(record["path"], target)
        self.assertNotIn("secret-value", json.dumps(record))

    def test_import_error_without_metadata_keeps_attempted_module(self):
        with patch.object(self.launch.importlib, "import_module", side_effect=ImportError("secret-value")):
            record = self.run_doctor()
        self.assertEqual(record["module"], "aiohttp")

    def test_nested_data_checks_keep_inner_stage_and_original_error_type(self):
        def failing_doctor(scope="all"):
            with self.launch.diagnostic_step("data", operation="write", path=self.root / "data"):
                with self.launch.diagnostic_step("lock", operation="lock", path=self.root / "data/check/lock"):
                    raise PermissionError(errno.EACCES, "secret-value")

        with patch.object(self.launch, "doctor", side_effect=failing_doctor):
            record = self.run_doctor()
        self.assertEqual(record["stage"], "lock")
        self.assertEqual(record["operation"], "lock")
        self.assertEqual(record["error_type"], "PermissionError")
        self.assertEqual(record["path"], str(self.root / "data/check/lock"))

    def test_git_failure_does_not_echo_subprocess_command_or_output(self):
        def failing_doctor(scope="all"):
            with self.launch.diagnostic_step("git", operation="execute", path=self.root / "runtime/git/bin/git"):
                raise subprocess.CalledProcessError(126, ["secret-command"], output="secret-output", stderr="secret-stderr")

        with patch.object(self.launch, "doctor", side_effect=failing_doctor):
            record = self.run_doctor()
        self.assertEqual(record["stage"], "git")
        self.assertEqual(record["returncode"], 126)
        self.assertEqual(record["error_type"], "CalledProcessError")
        self.assertNotIn("secret-", json.dumps(record))

    def test_unexpected_error_reports_location_without_message_or_locals(self):
        def failing_doctor(scope="all"):
            password = "secret-local"
            raise ValueError("secret-message")

        with patch.object(self.launch, "doctor", side_effect=failing_doctor):
            record = self.run_doctor()
        self.assertEqual(record["error_type"], "ValueError")
        self.assertEqual(record["location"]["function"], "failing_doctor")
        self.assertNotIn("secret-", json.dumps(record))


@unittest.skipUnless(shutil.which("sh"), "POSIX shell is required")
class OfflineBootstrapTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory(prefix="offline bootstrap ")
        self.addCleanup(work.cleanup)
        self.root = Path(work.name)
        launcher = Path(__file__).resolve().parents[1] / "deploy/offline/log-analyzer"
        shutil.copyfile(launcher, self.root / "log-analyzer")

    def run_launcher(self, python=None):
        environment = os.environ.copy()
        environment.pop("LOG_ANALYZER_PYTHON", None)
        if python is not None:
            environment["LOG_ANALYZER_PYTHON"] = python
        result = subprocess.run([shutil.which("sh"), str(self.root / "log-analyzer"), "doctor"],
                                env=environment, text=True, capture_output=True)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("stage=bootstrap", result.stderr)
        self.assertIn("hint:", result.stderr)
        return result.stderr

    def test_missing_bundled_python(self):
        output = self.run_launcher()
        self.assertIn("runtime/python/bin/python3.11", output)
        self.assertIn("missing, inaccessible or a broken symlink", output)

    def test_missing_python_override_keeps_command_name(self):
        output = self.run_launcher("nonexistent-offline-python")
        self.assertIn("path: nonexistent-offline-python", output)
        self.assertIn("Python command was not found", output)

    @unittest.skipIf(os.name == "nt", "POSIX executable bits are required")
    def test_python_without_execute_permission(self):
        python = self.root / "python"
        python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        python.chmod(0o600)
        self.assertIn("cannot be executed", self.run_launcher(str(python)))

    def test_missing_launcher_with_working_shell_as_python(self):
        self.assertIn("Launcher is missing or unreadable", self.run_launcher("sh"))


if __name__ == "__main__":
    unittest.main()
