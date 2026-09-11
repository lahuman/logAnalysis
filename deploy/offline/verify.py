"""Verify an archive in an isolated network namespace with loopback enabled.

Run on a RHEL 8 compatible test host:
  unshare --net sh -c 'ip link set lo up; python3.11 deploy/offline/verify.py ARCHIVE'
This uses synthetic HTTP responses, not an actual LLM model.
"""

import argparse
from datetime import UTC, datetime, timedelta
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import ipaddress
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tarfile
import tempfile
import threading


class SyntheticLLM(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.calls.append((self.path, self.headers.get("Authorization"), body))
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        path = "src/main/java/com/example/smoke/OrderService.java"
        result = {"summary": "Synthetic verification: null customer.",
                  "root_causes": [{"cause": "Null customer dereferenced.", "confidence": 0.9,
                                   "evidence": [{"file": path, "line": 4, "description": "customer.trim()"}]}],
                  "recommended_fixes": [{"description": "Validate customer before trim.", "files": [path], "risk": "low"}],
                  "validation_steps": ["Test null and non-null inputs."], "unknowns": ["Synthetic server; no real model inference."]}
        payload = json.dumps({"choices": [{"index": 0, "finish_reason": "stop",
                               "message": {"role": "assistant", "content": json.dumps(result)}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def verify(archive: Path) -> None:
    # Refuse a test run that still has a non-loopback network interface.
    interfaces = {name for _, name in socket.if_nameindex()}
    if interfaces != {"lo"}:
        raise RuntimeError("run in a network namespace with only loopback")
    with socket.socket() as probe:
        probe.settimeout(1)
        if probe.connect_ex(("1.1.1.1", 443)) == 0:
            raise RuntimeError("external network unexpectedly reachable")
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    expected = archive.with_suffix(archive.suffix + ".sha256").read_text().split()[0]
    assert digest == expected, "archive checksum mismatch"
    with tempfile.TemporaryDirectory(prefix="relocated log analyzer ") as directory:
        parent = Path(directory)
        with tarfile.open(archive) as stream:
            stream.extractall(parent, filter="data")
        root = next(parent.glob("log-analyzer-*"))
        environment = dict(os.environ)
        for key in ("LOG_ANALYZER_PYTHON", "LLM_API_KEY", "OPENAI_API_KEY", "NVIDIA_API_KEY"):
            environment.pop(key, None)
        environment["HTTPS_PROXY"] = "http://external-proxy.invalid:8888"
        environment["HTTP_PROXY"] = "http://external-proxy.invalid:8888"
        environment["PYTHONPATH"] = "/nonexistent"

        def launch(command, expected_code=0, *options):
            result = subprocess.run([str(root / "log-analyzer"), command, *options], cwd=parent, env=environment,
                                    text=True, capture_output=True, timeout=45)
            if result.returncode != expected_code:
                raise AssertionError(result.stdout + result.stderr)
            return result.stdout + result.stderr

        assert "offline_doctor_succeeded" in launch("doctor")
        assert "set the internal model ID" in launch("check-config", 2)
        source = root / "config/config.toml"
        original = source.read_text()
        configuration = original.replace('model = "replace-with-internal-model-id"', 'model = "synthetic-local"')
        source.write_text(configuration.replace('provider = "onprem"', 'provider = "nvidia_nim"'))
        assert "cloud providers are disabled" in launch("smoke", 2)

        server = ThreadingHTTPServer(("127.0.0.1", 0), SyntheticLLM)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            configuration = configuration.replace("https://llm.internal:8000/v1", f"http://127.0.0.1:{server.server_port}/v1")
            configuration = configuration.replace("allow_http = false", "allow_http = true").replace("auth_required = true", "auth_required = false")
            configuration = configuration.replace('tls_ca = "config/certs/internal-ca.pem"', "")
            source.write_text(configuration)
            assert "offline_configuration_valid" in launch("check-config")
            assert "nim_smoke_succeeded" in launch("smoke")
            assert len(SyntheticLLM.calls) == 1
            assert SyntheticLLM.calls[0][1] is None
            credential = root / "config/credentials/LLM_API_KEY"
            credential.write_text("synthetic-test-token")
            credential.chmod(0o600)
            assert "nim_smoke_succeeded" in launch("smoke")
            assert SyntheticLLM.calls[-1][1] == "Bearer synthetic-test-token"
            reports = list((root / "data/smoke-reports").glob("*.md"))
            assert len(reports) == 2
            assert all("COMPLETED" in p.read_text() and "OrderService.java:4" in p.read_text() for p in reports)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)

        # A self-signed internal CA must fail by default and succeed when configured.
        certificate = parent / "test-ca.pem"
        private_key = parent / "test-ca-key.pem"
        sys.path.insert(0, str(root / "app"))
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
        cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
                .not_valid_after(datetime.now(UTC) + timedelta(days=1))
                .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
                .sign(key, hashes.SHA256()))
        private_key.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        private_key.chmod(0o600)
        certificate.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        tls_server = ThreadingHTTPServer(("127.0.0.1", 0), SyntheticLLM)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate, private_key)
        tls_server.socket = context.wrap_socket(tls_server.socket, server_side=True)
        tls_worker = threading.Thread(target=tls_server.serve_forever, daemon=True)
        tls_worker.start()
        try:
            configuration = configuration.replace(f"http://127.0.0.1:{server.server_port}/v1",
                                                   f"https://127.0.0.1:{tls_server.server_port}/v1")
            configuration = configuration.replace("allow_http = true", "allow_http = false")
            source.write_text(configuration)
            assert "nim_smoke_failed" in launch("smoke", 1)
            assert len(SyntheticLLM.calls) == 2
            source.write_text(configuration.replace("\n[openai]\n", "\n[openai]\ntls_ca = " + json.dumps(str(certificate)) + "\n"))
            assert "nim_smoke_succeeded" in launch("smoke")
            assert len(SyntheticLLM.calls) == 3
        finally:
            tls_server.shutdown()
            tls_server.server_close()
            tls_worker.join(timeout=5)

        # Ensure built-in Git operations work using only the bundled binary.
        git = root / "runtime/git/bin/git"
        repo = root / "repos/verification"
        git_environment = dict(environment, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null",
                               GIT_ALLOW_PROTOCOL="file", GIT_NO_LAZY_FETCH="1")
        def git_run(*args):
            result = subprocess.run([str(git), "-C", str(repo), *args], text=True,
                                    capture_output=True, env=git_environment)
            if result.returncode:
                raise RuntimeError(result.stdout + result.stderr)
            return result.stdout
        repo.mkdir()
        git_run("init", "-b", "main")
        git_run("config", "user.name", "Offline Test")
        git_run("config", "user.email", "offline@example.test")
        (repo / "Source.java").write_text("class Source {}\n")
        git_run("add", "Source.java")
        git_run("-c", "commit.gpgsign=false", "commit", "-m", "Synthetic source")
        assert "class Source" in git_run("show", "HEAD:Source.java")
        assert "class Source" in git_run("blame", "-L", "1,1", "HEAD", "--", "Source.java")
        assert "Synthetic source" in git_run("log", "-p", "-1", "--", "Source.java")
        git_run("cat-file", "-s", "HEAD:Source.java")
        # Exercise local text ingestion through the real CLI, Git resolver, state and reports.
        java = repo / "src/main/java/com/example/smoke/OrderService.java"
        java.parent.mkdir(parents=True)
        java.write_text("package com.example.smoke;\npublic class OrderService {\n"
                        "public String customerName(String customer) {\nreturn customer.trim();\n}\n}\n")
        git_run("add", ".")
        git_run("-c", "commit.gpgsign=false", "commit", "-m", "Synthetic local-file source")
        log = root / "data/input/application.log"
        record = ("2000-01-01 09:00:00 ERROR Logger - customer missing\n"
                  "java.lang.NullPointerException: customer missing\n"
                  "\tat com.example.smoke.OrderService.customerName(OrderService.java:4)\n")
        log.write_text(record)
        file_server = ThreadingHTTPServer(("127.0.0.1", 0), SyntheticLLM)
        file_worker = threading.Thread(target=file_server.serve_forever, daemon=True)
        file_worker.start()
        calls_before = len(SyntheticLLM.calls)
        try:
            file_config = configuration.replace(
                f"https://127.0.0.1:{tls_server.server_port}/v1", f"http://127.0.0.1:{file_server.server_port}/v1"
            ).replace("allow_http = false", "allow_http = true")
            file_config = file_config.replace('"repos/order-api.git"', json.dumps(str(repo)))
            file_config = file_config.replace('"com.example.order"', '"com.example.smoke"')
            source.write_text(file_config)
            first = json.loads(launch("run"))["summary"]
            replay = json.loads(launch("run"))["summary"]
            assert first["completed"] == 1 and replay["duplicate_events"] == 1
            log.write_text(record + record)
            appended = json.loads(launch("run"))["summary"]
            assert appended["completed"] == 1 and appended["cache_hits"] == 1
            assert len(SyntheticLLM.calls) == calls_before + 1
            assert len(list((root / "data/reports").rglob("*.md"))) == 2
        finally:
            file_server.shutdown()
            file_server.server_close()
            file_worker.join(timeout=5)
        # Operator config, credentials and reports do not invalidate runtime checksums.
        assert "offline_doctor_succeeded" in launch("doctor")
        module = root / "src/log_analyzer/__init__.py"
        original_module = module.read_text()
        module.write_text(original_module + '\n__version__ = "editable-check"\n')
        assert '"application": "editable-check"' in launch("doctor")
        module.write_text(original_module + "\ndef invalid(:\n")
        assert "Python syntax error" in launch("doctor", 2)
        assert '"scope": "runtime"' in launch("doctor", 0, "--scope", "runtime")
        assert "Python syntax error" in launch("doctor", 2, "--scope", "source")
        module.write_text(original_module)
        test_module = root / "tests/test_offline_editable_probe.py"
        test_module.write_text("import unittest\nclass EditableProbe(unittest.TestCase):\n    def test_edit(self):\n        self.assertEqual(1, 1)\n")
        assert '"scope": "source"' in launch("doctor", 0, "--scope", "source")
        assert "Ran 1 test" in launch("test", 0, "--pattern", test_module.name)
        test_module.write_text(test_module.read_text().replace("assertEqual(1, 1)", "assertEqual(1, 2)"))
        assert "FAILED (failures=1)" in launch("test", 1, "--pattern", test_module.name)
        test_module.unlink()
        assert "no tests matched" in launch("test", 2, "--pattern", test_module.name)
        assert "Ran " in launch("test")
        dependency = root / "app/certifi/__init__.py"
        dependency.write_text(dependency.read_text() + "\n# integrity test\n")
        assert "bundle file mismatch" in launch("doctor", 2)
        assert "bundle file mismatch" in launch("doctor", 2, "--scope", "runtime")
        print(json.dumps({"event": "offline_archive_verified", "external_network": False,
                          "relocated_path_with_spaces": True, "synthetic_llm_calls": len(SyntheticLLM.calls),
                          "bundled_git": True, "custom_ca_tls": True, "local_file_cli_replay_append": True,
                          "editable_source": True, "source_syntax_check": True, "bundled_tests": True,
                          "editable_tests": True, "scoped_doctor": True, "selected_tests": True,
                          "tamper_detection": True, "sha256": digest}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    verify(parser.parse_args().archive.resolve())
