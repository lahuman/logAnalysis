from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from log_analyzer.errors import AlreadyRunningError, UnsupportedPlatformError
from log_analyzer.run_lock import RunLock


class RunLockTests(unittest.TestCase):
    def test_windows_fails_closed_instead_of_silently_running_unlocked(self) -> None:
        if RunLock.supported():
            self.skipTest("non-POSIX behavior only")
        with tempfile.TemporaryDirectory() as directory:
            lock = RunLock(Path(directory) / "run.lock", "/etc/app.toml")
            with self.assertRaises(UnsupportedPlatformError):
                lock.acquire()
            self.assertFalse(lock.acquired)

    @unittest.skipUnless(RunLock.supported(), "requires POSIX flock")
    def test_second_owner_is_rejected_and_metadata_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.lock"
            first = RunLock(path, "/etc/first.toml").acquire()
            try:
                with self.assertRaises(AlreadyRunningError) as raised:
                    RunLock(path, "/etc/second.toml").acquire()
                self.assertEqual(raised.exception.metadata["pid"], os.getpid())
                self.assertEqual(
                    raised.exception.metadata["config_path"],
                    "/etc/first.toml",
                )
            finally:
                first.release()

            with RunLock(path, "/etc/second.toml") as second:
                self.assertTrue(second.acquired)
            self.assertTrue(path.exists())

    @unittest.skipUnless(RunLock.supported(), "requires POSIX flock")
    def test_kernel_releases_lock_after_abnormal_process_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.lock"
            script = (
                "import sys,time;"
                "from log_analyzer.run_lock import RunLock;"
                "lock=RunLock(sys.argv[1], 'child.toml').acquire();"
                "print('ready', flush=True);"
                "time.sleep(30)"
            )
            environment = dict(os.environ)
            src = str(Path(__file__).resolve().parents[1] / "src")
            environment["PYTHONPATH"] = (
                src
                if not environment.get("PYTHONPATH")
                else src + os.pathsep + environment["PYTHONPATH"]
            )
            process = subprocess.Popen(
                [sys.executable, "-c", script, str(path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
            )
            try:
                self.assertEqual(process.stdout.readline().strip(), "ready")
                with self.assertRaises(AlreadyRunningError):
                    RunLock(path, "parent.toml").acquire()
                process.kill()
                process.wait(timeout=5)
                with RunLock(path, "parent.toml"):
                    pass
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
