from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DeploymentFileTests(unittest.TestCase):
    def test_service_is_hardened_oneshot_with_runtime_lock_directory(self) -> None:
        service = (ROOT / "deploy" / "log-analyzer.service").read_text(
            encoding="utf-8"
        )

        self.assertIn("Type=oneshot", service)
        self.assertIn("User=log-analyzer", service)
        self.assertIn("RuntimeDirectory=log-analyzer", service)
        self.assertIn("NoNewPrivileges=true", service)
        self.assertIn("ProtectSystem=strict", service)
        self.assertIn("TimeoutStartSec=infinity", service)
        self.assertNotIn("LoadCredential=", service)

    def test_timer_and_hourly_drop_in_reset_schedule(self) -> None:
        timer = (ROOT / "deploy" / "log-analyzer.timer").read_text(encoding="utf-8")
        hourly = (ROOT / "deploy" / "hourly-schedule.conf.example").read_text(
            encoding="utf-8"
        )

        self.assertIn("OnCalendar=*:0/10", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("OnCalendar=\nOnCalendar=hourly", hourly)

    def test_credentials_are_selected_in_a_drop_in(self) -> None:
        credentials = (ROOT / "deploy" / "credentials.conf.example").read_text(
            encoding="utf-8"
        )

        self.assertIn("LoadCredential=OPENAI_API_KEY:", credentials)
        self.assertIn("LoadCredential=ES_API_KEY:", credentials)
        self.assertIn("LoadCredential=ES_USERNAME:", credentials)
        self.assertIn("LoadCredential=ES_PASSWORD:", credentials)

    def test_docker_image_pins_python_and_runs_as_non_root(self) -> None:
        dockerfile = (ROOT / "Dockerfile.test").read_text(encoding="utf-8")

        self.assertIn("FROM python:3.11.8-slim-bookworm", dockerfile)
        self.assertIn("USER tester", dockerfile)
        self.assertIn("COPY deploy ./deploy", dockerfile)
        self.assertIn("COPY Dockerfile.test compose.yaml ./", dockerfile)
        self.assertIn('CMD ["python", "-m", "unittest"', dockerfile)

    def test_compose_pins_es_and_exposes_it_only_on_loopback(self) -> None:
        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")

        self.assertIn(
            "docker.elastic.co/elasticsearch/elasticsearch:8.19.21",
            compose,
        )
        self.assertIn('"127.0.0.1:19200:9200"', compose)
        self.assertIn('xpack.security.enabled: "false"', compose)
        self.assertIn("LOG_ANALYZER_TEST_ES_URL: http://elasticsearch:9200", compose)
        self.assertIn("condition: service_healthy", compose)


if __name__ == "__main__":
    unittest.main()
