from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location('offline_' + name, ROOT / 'deploy/offline' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    # Loading the executable launcher must not change this test process's import ordering.
    import sys
    original_path = sys.path[:]
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = original_path
    return module


class OfflineCompatibilityTests(unittest.TestCase):
    def test_elf_gate_accepts_228_and_rejects_newer_or_unknown_abi(self):
        builder = load_script('build')
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            (root / 'extension.so').write_bytes(b'\x7fELFfixture')
            (root / 'readme.txt').write_text('ordinary file')
            with patch.object(builder.subprocess, 'check_output', return_value='Name: GLIBC_2.2.5\nName: GLIBC_2.28\n'):
                result = builder.verify_elf_compatibility(root)
                self.assertEqual(result, {'elf_files_checked':1, 'max_glibc_required':'2.28'})
            for symbol in ['GLIBC_2.34', 'GLIBC_2.29', 'GLIBC_ABI_DT_RELR', 'GLIBC_PRIVATE']:
                with self.subTest(symbol=symbol), patch.object(builder.subprocess, 'check_output', return_value='Name: '+symbol):
                    with self.assertRaises(ValueError):
                        builder.verify_elf_compatibility(root)

    def test_source_edit_is_allowed_but_syntax_error_has_file_and_line(self):
        launch = load_script('launch')
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            source = root / 'src/log_analyzer/example.py'
            source.parent.mkdir(parents=True)
            source.write_text('value = 1\n')
            with patch.object(launch, 'ROOT', root):
                launch.check_editable_sources()
                source.write_text('value = 2\n')
                launch.check_editable_sources()
                source.write_text('def broken(:\n')
                with self.assertRaisesRegex(launch.PreflightError, 'example.py:1'):
                    launch.check_editable_sources()

    def test_older_libc_is_rejected_before_dependency_checks(self):
        launch = load_script('launch')
        with (patch.object(launch.platform,'system',return_value='Linux'),
              patch.object(launch.platform,'machine',return_value='x86_64'),
              patch.object(launch.platform,'libc_ver',return_value=('glibc','2.17')),
              patch.object(launch,'verify_bundle') as integrity):
            with self.assertRaisesRegex(launch.PreflightError, 'detected glibc 2.17'):
                launch.doctor()
            integrity.assert_not_called()
