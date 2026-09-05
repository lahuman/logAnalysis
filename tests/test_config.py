from __future__ import annotations

import tempfile
import unittest
from pathlib import Path, PurePosixPath

from log_analyzer.config import (
    ConfigError,
    load_config,
    read_secret,
    require_secret,
    OpenAIConfig,
)


def _config_text(extra: str = "") -> str:
    return (
        """
[run]
batch_size = 100

[error_source]
url = "https://elasticsearch.example.test:9200"
index = "logs-*"

[analysis]
language = "java"

[services.orders]
repository = "/srv/repos/orders"
source_roots = ["service/src/main/java"]
application_packages = ["com.example.orders"]

[openai]
model = "test-model"

[state]
path = "/var/lib/log-analyzer/state.db"

[report]
directory = "/var/lib/log-analyzer/reports"
"""
        + extra
    )


class ConfigTests(unittest.TestCase):
    def test_nim_provider_defaults_and_legacy_openai_are_distinct(self) -> None:
        nim = OpenAIConfig(provider="nvidia_nim", model="test-model")
        legacy = OpenAIConfig(model="test-model")
        self.assertEqual("https://integrate.api.nvidia.com/v1", nim.base_url)
        self.assertEqual("NVIDIA_API_KEY", nim.api_key_secret)
        self.assertEqual("https://api.openai.com/v1", legacy.base_url)
        self.assertEqual("OPENAI_API_KEY", legacy.api_key_secret)
        self.assertEqual("1", legacy.cache_analyzer_version("1"))
        self.assertNotEqual(legacy.cache_analyzer_version("1"), nim.cache_analyzer_version("1"))

    def test_nim_cache_identity_includes_endpoint_output_mode_and_thinking(self) -> None:
        original = OpenAIConfig(provider="nvidia_nim", model="test-model")
        variants = (
            {"base_url": "https://nim.example.test/v1"},
            {"structured_output": "json_object"},
            {"enable_thinking": False},
        )
        for variant in variants:
            changed = OpenAIConfig.model_validate(original.model_dump() | variant)
            self.assertNotEqual(original.cache_analyzer_version("1"), changed.cache_analyzer_version("1"))

    def test_nim_options_are_validated_through_toml(self) -> None:
        content = _config_text().replace(
            '[openai]', '[openai]\nprovider = "nvidia_nim"\nstructured_output = "guided_json"\nenable_thinking = false'
        )
        self.assertEqual("guided_json", self.load(content).openai.structured_output)
        with self.assertRaises(ConfigError):
            self.load(content.replace('"guided_json"', '"automatic_fallback"'))
        with self.assertRaises(ConfigError):
            self.load(content.replace('provider = "nvidia_nim"', 'provider = "openai"'))

    def test_rejects_insecure_openai_url(self) -> None:
        data = _config_text().replace(
            'model = "test-model"',
            'base_url = "http://api.openai.com/v1"\nmodel = "test-model"',
        )
        with self.assertRaises(ConfigError):
            self.load(data)

        secret_url = _config_text().replace(
            'model = "test-model"',
            'base_url = "https://secret@api.openai.com/v1"\nmodel = "test-model"',
        )
        with self.assertRaises(ConfigError):
            self.load(secret_url)

    def test_rejects_insecure_or_secret_bearing_elasticsearch_url(self) -> None:
        for url in (
            "http://elasticsearch.example.test:9200",
            "https://user:password@elasticsearch.example.test:9200",
            "https://elasticsearch.example.test:9200?api_key=secret",
        ):
            with self.subTest(url=url), self.assertRaises(ConfigError):
                self.load(
                    _config_text().replace(
                        "https://elasticsearch.example.test:9200",
                        url,
                    )
                )

    def test_rejects_analysis_identity_that_does_not_match_code(self) -> None:
        data = _config_text().replace(
            'language = "java"',
            'language = "java"\nprompt_version = "old-prompt"',
        )
        with self.assertRaises(ConfigError):
            self.load(data)

    def load(self, content: str):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(content, encoding="utf-8")
            return load_config(path)

    def test_loads_named_service_table_and_defaults(self) -> None:
        config = self.load(_config_text())

        self.assertEqual(config.run.batch_size, 100)
        self.assertEqual(config.run.max_concurrency, 3)
        self.assertEqual(config.analysis.language, "java")
        self.assertEqual(config.services[0].name, "orders")
        self.assertEqual(config.services[0].branch, "HEAD")
        self.assertEqual(config.services[0].diff_history, 5)
        self.assertEqual(
            config.services[0].source_roots,
            (PurePosixPath("service/src/main/java"),),
        )
        self.assertEqual(config.report.retention_days, 30)
        self.assertEqual(config.error_source.url, "https://elasticsearch.example.test:9200")

    def test_loads_git_fallback_settings(self) -> None:
        content = _config_text().replace(
            'repository = "/srv/repos/orders"',
            'repository = "/srv/repos/orders"\nbranch = "origin/main"\ndiff_history = 10',
        )
        config = self.load(content)
        self.assertEqual(config.services[0].branch, "origin/main")
        self.assertEqual(config.services[0].diff_history, 10)

    def test_loads_array_of_service_tables(self) -> None:
        content = """
[error_source]
url = "https://elasticsearch.example.test"
index = "logs"

[[services]]
name = "orders"
repository = "/srv/repos/orders"
application_packages = ["com.example.orders"]

[openai]
model = "test-model"
"""
        config = self.load(content)
        self.assertEqual(config.services[0].name, "orders")

    def test_rejects_unsupported_language(self) -> None:
        with self.assertRaises(ConfigError):
            self.load(_config_text().replace('language = "java"', 'language = "python"'))

    def test_rejects_disabled_tls_verification(self) -> None:
        content = _config_text().replace(
            'index = "logs-*"',
            'index = "logs-*"\nverify_tls = false',
        )
        with self.assertRaisesRegex(ConfigError, "cannot be disabled"):
            self.load(content)

    def test_rejects_repository_escape_in_source_root(self) -> None:
        content = _config_text().replace(
            'source_roots = ["service/src/main/java"]',
            'source_roots = ["../outside"]',
        )
        with self.assertRaises(ConfigError):
            self.load(content)

    def test_rejects_secret_value_in_toml(self) -> None:
        content = _config_text().replace(
            'model = "test-model"',
            'model = "test-model"\napi_key = "must-not-be-here"',
        )
        with self.assertRaises(ConfigError):
            self.load(content)

    def test_environment_secret_takes_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "OPENAI_API_KEY").write_text(
                "from-file\n", encoding="utf-8"
            )
            secret = read_secret(
                "OPENAI_API_KEY",
                {
                    "OPENAI_API_KEY": "from-environment",
                    "CREDENTIALS_DIRECTORY": directory,
                },
            )
        self.assertEqual(secret, "from-environment")

    def test_reads_systemd_credential(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "OPENAI_API_KEY").write_text(
                "from-file\n", encoding="utf-8"
            )
            secret = read_secret(
                "OPENAI_API_KEY", {"CREDENTIALS_DIRECTORY": directory}
            )
        self.assertEqual(secret, "from-file")

    def test_missing_required_secret_fails_without_leaking_value(self) -> None:
        with self.assertRaisesRegex(ConfigError, "OPENAI_API_KEY"):
            require_secret("OPENAI_API_KEY", {})


if __name__ == "__main__":
    unittest.main()
