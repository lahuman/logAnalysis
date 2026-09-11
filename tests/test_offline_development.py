from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tests.test_offline_compatibility import load_script


class OfflineDevelopmentTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory(prefix="offline development ")
        self.addCleanup(work.cleanup)
        self.root = Path(work.name)
        self.launch = load_script("launch")
        self.builder = load_script("build")
        shutil.copyfile(Path(__file__).resolve().parents[1] / "deploy/offline/launch.py", self.root / "launch.py")
        self.write("src/log_analyzer/__init__.py", '__version__ = "fixture-v1"\n')
        self.write("src/log_analyzer/config.py", "def load_config(path): pass\n")
        self.write("src/log_analyzer/report.py", "class ReportWriter: pass\n")
        self.write("src/log_analyzer/cli.py", "def main(): pass\n")
        self.write("src/log_analyzer/llm_smoke.py", "def main(): pass\n")
        (self.root / "tests").mkdir()

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def cli(self, *args, runtime_fixture=False):
        # Only replace the Linux runtime boundary; syntax, isolated imports,
        # argument parsing, discovery and test execution all run unchanged.
        script = '''
import runpy, sys
launcher = runpy.run_path(sys.argv[1])
if sys.argv[2] == "fixture":
    launcher["doctor"].__globals__["check_runtime"] = lambda: {"git": "fixture"}
sys.argv = [sys.argv[1], *sys.argv[3:]]
sys.exit(launcher["main"]())
'''
        return subprocess.run([sys.executable, "-I", "-B", "-c", script, str(self.root / "launch.py"),
                               "fixture" if runtime_fixture else "real", *args],
                              cwd=self.root, capture_output=True, text=True, encoding="utf-8", timeout=45)

    def test_manifest_allows_source_and_test_edits_but_protects_runtime_and_launcher(self):
        test = self.write("tests/test_probe.py", "value = 1\n")
        protected = [self.write(name, "original\n") for name in
                     ("app/dependency.py", "runtime/python/bin/python3.11", "runtime/git/bin/git", "log-analyzer")]
        protected.append(self.root / "launch.py")
        manifest = {"editable_paths": list(self.builder.EDITABLE_PATHS),
                    "files": self.builder.bundle_files(self.root)}
        self.write("manifest.json", json.dumps(manifest))
        self.assertIn("tests/", manifest["editable_paths"])
        test.write_text("value = 2\n", encoding="utf-8")
        self.write("tests/test_new.py", "value = 3\n")
        self.write("src/log_analyzer/report.py", "class ReportWriter: changed = True\n")
        with patch.object(self.launch, "ROOT", self.root):
            self.launch.verify_bundle()
            self.launch.check_editable_sources()
            for path in protected:
                with self.subTest(path=path.relative_to(self.root)):
                    original = path.read_bytes()
                    path.write_bytes(original + b"# changed\n")
                    with self.assertRaisesRegex(self.launch.PreflightError, "bundle file mismatch"):
                        self.launch.verify_bundle()
                    path.write_bytes(original)

    def test_source_scope_works_without_manifest_or_runtime_and_reports_scope(self):
        result = self.cli("doctor", "--scope", "source")
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(result.stdout)
        self.assertEqual(record["event"], "offline_doctor_succeeded")
        self.assertEqual(record["scope"], "source")
        self.assertEqual(record["application"], "fixture-v1")
        self.assertNotIn("git", record)
        self.assertFalse((self.root / "data").exists())

    def test_source_imports_use_a_fresh_process_and_see_edits(self):
        with patch.object(self.launch, "ROOT", self.root):
            before = sys.modules.get("log_analyzer")
            self.assertEqual(self.launch.check_source_imports(), "fixture-v1")
            self.write("src/log_analyzer/__init__.py", '__version__ = "fixture-v2"\n')
            self.assertEqual(self.launch.check_source_imports(), "fixture-v2")
            self.assertIs(sys.modules.get("log_analyzer"), before)
        self.assertEqual(list(self.root.rglob("*.pyc")), [])

    def test_source_import_failure_keeps_metadata_and_suppresses_import_output(self):
        self.write("src/log_analyzer/cli.py",
                   'print("secret-output")\nraise ImportError("secret-message", name="missing_symbol")\n')
        result = self.cli("doctor", "--scope", "source")
        self.assertEqual(result.returncode, 2)
        record = json.loads(result.stderr)
        self.assertEqual(record["scope"], "source")
        self.assertEqual(record["stage"], "imports")
        self.assertEqual(record["module"], "missing_symbol")
        self.assertEqual(record["location"]["line"], 2)
        self.assertNotIn("secret-", result.stdout + result.stderr)

    def test_source_scope_reports_all_syntax_errors_in_source_and_tests(self):
        self.write("src/log_analyzer/cli.py", 'secret = "hidden-source" +\n')
        self.write("tests/test_broken.py", "def invalid(:\n")
        result = self.cli("doctor", "--scope", "source")
        self.assertEqual(result.returncode, 2)
        record = json.loads(result.stderr)
        self.assertEqual(record["error_count"], 2)
        self.assertEqual({Path(item["path"]).name for item in record["errors"]}, {"cli.py", "test_broken.py"})
        self.assertTrue(all(item["line"] == 1 for item in record["errors"]))
        self.assertNotIn("hidden-source", result.stderr)

    def test_runtime_scope_skips_broken_source_and_still_checks_manifest(self):
        self.write("src/log_analyzer/cli.py", "def invalid(:\n")
        self.write("tests/test_broken.py", "def invalid(:\n")
        with (patch.object(self.launch, "ROOT", self.root),
              patch.object(self.launch.platform, "system", return_value="Linux"),
              patch.object(self.launch.platform, "machine", return_value="x86_64"),
              patch.object(self.launch.platform, "libc_ver", return_value=("glibc", "2.28")),
              patch.object(self.launch, "check_source_imports") as source_imports):
            with self.assertRaises(self.launch.PreflightError) as caught:
                self.launch.doctor("runtime")
        self.assertEqual(caught.exception.details["stage"], "manifest")
        self.assertEqual(caught.exception.details["error_type"], "FileNotFoundError")
        source_imports.assert_not_called()

    def test_doctor_default_and_runtime_scope_select_the_expected_checks(self):
        for scope in ("all", "runtime"):
            with self.subTest(scope=scope):
                output = io.StringIO()
                with (patch.object(self.launch, "check_runtime", return_value={"git": "fixture"}) as runtime,
                      patch.object(self.launch, "check_editable_sources") as syntax,
                      patch.object(self.launch, "check_source_imports", return_value="fixture") as imports,
                      redirect_stdout(output)):
                    if scope == "all":
                        self.launch.doctor()
                    else:
                        self.launch.doctor(scope)
                runtime.assert_called_once_with()
                self.assertEqual(syntax.call_count, int(scope == "all"))
                self.assertEqual(imports.call_count, int(scope == "all"))
                self.assertEqual(json.loads(output.getvalue())["scope"], scope)

    def test_selected_tests_exclude_unrelated_failures_and_preserve_default_discovery(self):
        self.write("tests/test_probe_pass.py",
                   "import unittest\nclass Passing(unittest.TestCase):\n    def test_ok(self): self.assertEqual(2 + 2, 4)\n")
        self.write("tests/test_probe_fail.py",
                   "import unittest\nclass Failing(unittest.TestCase):\n    def test_bad(self): self.fail('unselected failure')\n")
        selected = self.cli("test", "--pattern", "test_probe_pass.py", runtime_fixture=True)
        self.assertEqual(selected.returncode, 0, selected.stderr)
        self.assertEqual(json.loads(selected.stdout)["scope"], "all")
        self.assertIn("Ran 1 test", selected.stderr)
        self.assertNotIn("unselected failure", selected.stderr)
        for options in ((), ("--pattern", "test_probe_*.py")):
            result = self.cli("test", *options, runtime_fixture=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn("Ran 2 tests", result.stderr)
            self.assertIn("FAILED (failures=1)", result.stderr)

    def test_unmatched_pattern_is_a_failure(self):
        result = self.cli("test", "--pattern", "test_missing.py", runtime_fixture=True)
        self.assertEqual(result.returncode, 2)
        record = json.loads(result.stderr)
        self.assertEqual(record["stage"], "tests")
        self.assertEqual(record["pattern"], "test_missing.py")
        self.assertIn("no tests matched", record["reason"])

    def test_test_import_failure_returns_failure(self):
        self.write("tests/test_missing_import.py", "import nonexistent_offline_test_dependency\n")
        result = self.cli("test", "--pattern", "test_missing_import.py", runtime_fixture=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAILED (errors=1)", result.stderr)

    def test_selected_tests_do_not_bypass_full_preflight(self):
        def fail_doctor():
            raise self.launch.PreflightError("runtime is broken")

        with (patch.object(self.launch.sys, "argv", ["launch.py", "test", "--pattern", "test_probe.py"]),
              patch.object(self.launch, "doctor", side_effect=fail_doctor) as doctor,
              patch("unittest.TestLoader.discover") as discover,
              patch("sys.stderr", new_callable=io.StringIO)):
            self.assertEqual(self.launch.main(), 2)
        doctor.assert_called_once_with()
        discover.assert_not_called()

    def test_options_reject_wrong_commands_and_path_patterns(self):
        for args in (("test", "--scope", "source"), ("doctor", "--pattern", "test*.py"),
                     ("doctor", "--scope", "unknown"), ("test", "--pattern", ""),
                     ("test", "--pattern", "../test.py"), ("test", "--pattern", "tests\\test.py")):
            with self.subTest(args=args):
                result = self.cli(*args)
                self.assertEqual(result.returncode, 2)
                self.assertIn("error:", result.stderr)
                self.assertNotIn("offline_doctor_succeeded", result.stdout)


if __name__ == "__main__":
    unittest.main()
