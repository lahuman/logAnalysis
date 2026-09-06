"""Strict TOML configuration models and secret lookup."""

from __future__ import annotations

import os
import hashlib
import re
import stat
import tomllib
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .errors import ConfigError


_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_JAVA_PACKAGE = re.compile(r"^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*$")
_GIT_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_MAX_CREDENTIAL_SIZE = 65_536
_ORACLE_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]{0,127}$")


class _ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RunConfig(_ConfigModel):
    batch_size: int = Field(default=500, ge=1, le=10_000)
    max_concurrency: int = Field(default=3, ge=1, le=64)
    initial_lookback_minutes: int = Field(default=60, ge=1, le=43_200)
    ingestion_delay_seconds: int = Field(default=60, ge=0, le=86_400)
    overlap_minutes: int = Field(default=5, ge=0, le=1_440)
    lock_file: Path = Path("/run/log-analyzer/run.lock")
    severities: tuple[str, ...] = ("ERROR", "FATAL")

    @field_validator("severities")
    @classmethod
    def validate_severities(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(item.strip().upper() for item in value if item.strip())
        if not normalized:
            raise ValueError("at least one severity is required")
        return normalized


class ErrorSourceConfig(_ConfigModel):
    type: Literal["elasticsearch"] = "elasticsearch"
    name: str = "elasticsearch"
    url: str
    index: str
    tls_ca: Path | None = None
    verify_tls: bool = True
    request_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    username_secret: str = "ES_USERNAME"
    password_secret: str = "ES_PASSWORD"
    api_key_secret: str = "ES_API_KEY"

    @field_validator("name", "index")
    @classmethod
    def not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        value = value.rstrip("/")
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("must be an absolute HTTPS URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("must not contain userinfo, query parameters, or a fragment")
        return value

    @field_validator("username_secret", "password_secret", "api_key_secret")
    @classmethod
    def validate_secret_name(cls, value: str) -> str:
        if not _ENV_NAME.fullmatch(value):
            raise ValueError("must be an uppercase environment variable name")
        return value

    @model_validator(mode="after")
    def validate_tls(self) -> "ErrorSourceConfig":
        if not self.verify_tls:
            raise ValueError("TLS certificate verification cannot be disabled")
        return self


class OracleColumnsConfig(_ConfigModel):
    event_id: str = "EVENT_ID"
    occurred_at: str = "OCCURRED_AT"
    service: str = "SERVICE_NAME"
    severity: str = "LOG_LEVEL"
    message: str = "ERROR_MESSAGE"
    stack_trace: str | None = "STACK_TRACE"
    error_type: str | None = None
    environment: str | None = None
    version: str | None = None
    git_commit: str | None = None
    trace_id: str | None = None

    @field_validator("*", mode="before")
    @classmethod
    def validate_column(cls, value: Any) -> str | None:
        if value is None or value == "":
            return None
        if not isinstance(value, str) or not _ORACLE_IDENTIFIER.fullmatch(value):
            raise ValueError("must be an unquoted Oracle column identifier")
        return value


class OracleSourceConfig(_ConfigModel):
    type: Literal["oracle"]
    name: str = "oracle-logs"
    dsn: str
    table: str
    columns: OracleColumnsConfig = OracleColumnsConfig()
    timestamp_type: Literal["timestamp", "timestamp_tz"] = "timestamp"
    timestamp_timezone: str = "+00:00"
    request_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    username_secret: str = "ORACLE_USERNAME"
    password_secret: str = "ORACLE_PASSWORD"
    wallet_location: Path | None = None
    wallet_password_secret: str = "ORACLE_WALLET_PASSWORD"

    @field_validator("name", "dsn")
    @classmethod
    def validate_text(cls, value: str, info: Any) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        if info.field_name == "dsn" and (
            "@" in value or re.search(r"(?i)(?:password|access_token|user)\s*=", value)
            or value.lower().startswith("config-")
        ):
            raise ValueError("use a local Oracle DSN without embedded credentials or cloud configuration")
        return value

    @field_validator("table")
    @classmethod
    def validate_table(cls, value: str) -> str:
        parts = value.split(".")
        if len(parts) not in {1, 2} or not all(_ORACLE_IDENTIFIER.fullmatch(part) for part in parts):
            raise ValueError("must be an unquoted table/view name, optionally qualified by schema")
        return value

    @field_validator("timestamp_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        if not re.fullmatch(r"[+-](?:0\d|1[0-4]):[0-5]\d", value) or (value[1:3] == "14" and value[4:] != "00"):
            raise ValueError("must be a fixed UTC offset between -14:00 and +14:00")
        return value

    @field_validator("username_secret", "password_secret", "wallet_password_secret")
    @classmethod
    def validate_secret(cls, value: str) -> str:
        if not _ENV_NAME.fullmatch(value):
            raise ValueError("must be an uppercase environment variable name")
        return value


class AnalysisConfig(_ConfigModel):
    language: Literal["java"] = "java"
    prompt_version: Literal["java-incident-v2"] = "java-incident-v2"
    analyzer_version: Literal["1"] = "1"
    max_log_characters: int = Field(default=100_000, ge=1_000, le=1_000_000)
    source_context_lines: int = Field(default=30, ge=1, le=200)
    max_source_bytes: int = Field(default=256_000, ge=1_024, le=5_000_000)
    max_git_change_chars: int = Field(default=30_000, ge=1_000, le=100_000)


class ServiceConfig(_ConfigModel):
    name: str
    repository: Path
    branch: str = "HEAD"
    diff_history: int = Field(default=5, ge=1, le=50)
    source_roots: tuple[PurePosixPath, ...] = (PurePosixPath("src/main/java"),)
    application_packages: tuple[str, ...]
    framework_packages: tuple[str, ...] = ("java", "jdk", "sun", "org.springframework")

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("branch")
    @classmethod
    def validate_branch(cls, value: str) -> str:
        value = value.strip()
        if (
            not _GIT_REFERENCE.fullmatch(value)
            or ".." in value
            or "//" in value
            or value.endswith(".lock")
        ):
            raise ValueError("must be a safe Git branch or revision name")
        return value

    @field_validator("source_roots")
    @classmethod
    def validate_source_roots(
        cls, value: tuple[PurePosixPath, ...]
    ) -> tuple[PurePosixPath, ...]:
        if not value:
            raise ValueError("at least one source root is required")
        for root in value:
            if root.is_absolute() or ".." in root.parts or str(root) in {"", "."}:
                raise ValueError("source roots must be safe repository-relative paths")
        return value

    @field_validator("application_packages", "framework_packages")
    @classmethod
    def validate_packages(cls, value: tuple[str, ...], info: Any) -> tuple[str, ...]:
        packages = tuple(package.strip().rstrip(".") for package in value if package.strip())
        if info.field_name == "application_packages" and not packages:
            raise ValueError("at least one application package is required")
        if any(not _JAVA_PACKAGE.fullmatch(package) for package in packages):
            raise ValueError("contains an invalid Java package")
        return packages


class OpenAIConfig(_ConfigModel):
    provider: Literal["openai", "nvidia_nim", "onprem"] = "openai"
    base_url: str = "https://api.openai.com/v1"
    model: str
    timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    max_output_tokens: int = Field(default=2_000, ge=128, le=100_000)
    api_key_secret: str = "OPENAI_API_KEY"
    structured_output: Literal["json_schema", "guided_json", "json_object"] = "json_schema"
    enable_thinking: bool | None = None
    auth_required: bool = True
    allow_http: bool = False
    tls_ca: Path | None = None

    @model_validator(mode="before")
    @classmethod
    def provider_defaults(cls, value: Any) -> Any:
        if isinstance(value, dict) and value.get("provider") == "nvidia_nim":
            value = dict(value)
            value.setdefault("base_url", "https://integrate.api.nvidia.com/v1")
            value.setdefault("api_key_secret", "NVIDIA_API_KEY")
        if isinstance(value, dict) and value.get("provider") == "onprem":
            value = dict(value)
            if not value.get("base_url"):
                raise ValueError("onprem requires an explicit internal base_url")
            value.setdefault("api_key_secret", "LLM_API_KEY")
        return value

    @model_validator(mode="after")
    def validate_output_mode(self) -> "OpenAIConfig":
        if self.provider == "openai" and self.structured_output != "json_schema":
            raise ValueError("OpenAI Responses requires json_schema output")
        if self.provider == "openai" and self.enable_thinking is not None:
            raise ValueError("enable_thinking requires a Chat Completions provider")
        if self.provider != "onprem" and (not self.auth_required or self.allow_http):
            raise ValueError("optional authentication and HTTP are only supported for onprem")
        if urlparse(self.base_url).scheme == "http" and not (
            self.provider == "onprem" and self.allow_http
        ):
            raise ValueError("HTTP requires provider=onprem and allow_http=true")
        return self

    def cache_analyzer_version(self, version: str) -> str:
        if self.provider == "openai" and self.base_url == "https://api.openai.com/v1":
            return version
        identity = f"{self.provider}\n{self.base_url}\n{self.structured_output}\n{self.enable_thinking}"
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        return f"{version}:{self.provider}:{digest}"

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        value = value.rstrip("/")
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("LLM base_url must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(
                "LLM base_url must not contain userinfo, query parameters, or a fragment"
            )
        return value

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("api_key_secret")
    @classmethod
    def validate_secret_name(cls, value: str) -> str:
        if not _ENV_NAME.fullmatch(value):
            raise ValueError("must be an uppercase environment variable name")
        return value


class StateConfig(_ConfigModel):
    path: Path = Path("/var/lib/log-analyzer/state.db")
    busy_timeout_seconds: float = Field(default=5.0, gt=0, le=60)


class ReportConfig(_ConfigModel):
    directory: Path = Path("/var/lib/log-analyzer/reports")
    retention_days: int = Field(default=30, ge=1, le=3_650)


class AppConfig(_ConfigModel):
    run: RunConfig = RunConfig()
    error_source: ErrorSourceConfig | OracleSourceConfig
    analysis: AnalysisConfig = AnalysisConfig()
    services: tuple[ServiceConfig, ...]
    openai: OpenAIConfig
    state: StateConfig = StateConfig()
    report: ReportConfig = ReportConfig()

    @model_validator(mode="after")
    def validate_services(self) -> "AppConfig":
        if not self.services:
            raise ValueError("at least one service is required")
        names = [service.name for service in self.services]
        if len(names) != len(set(names)):
            raise ValueError("service names must be unique")
        return self

    def service(self, name: str) -> ServiceConfig | None:
        return next((service for service in self.services if service.name == name), None)


def _normalize_services(data: dict[str, Any]) -> None:
    services = data.get("services")
    if not isinstance(services, dict):
        return
    normalized: list[dict[str, Any]] = []
    for name, service_data in services.items():
        if not isinstance(service_data, dict):
            raise ConfigError(f"services.{name} must be a TOML table")
        item = dict(service_data)
        configured_name = item.setdefault("name", name)
        if configured_name != name:
            raise ConfigError(f"services.{name}.name must match its table name")
        normalized.append(item)
    data["services"] = normalized


def load_config(path: str | os.PathLike[str]) -> AppConfig:
    """Load and strictly validate a TOML configuration file."""

    config_path = Path(path)
    try:
        with config_path.open("rb") as stream:
            data = dict(tomllib.load(stream))
        _normalize_services(data)
        return AppConfig.model_validate(data)
    except ConfigError:
        raise
    except (OSError, tomllib.TOMLDecodeError, ValidationError, TypeError, ValueError) as exc:
        raise ConfigError(f"invalid configuration {config_path}: {exc}") from exc


def read_secret(
    name: str, environ: Mapping[str, str] | None = None
) -> str | None:
    """Read a secret from the environment or a systemd credential file.

    The environment takes precedence. Credential paths are constrained to the
    directory supplied by systemd and symlinks are rejected.
    """

    if not _ENV_NAME.fullmatch(name):
        raise ConfigError(f"invalid secret name: {name!r}")
    values = os.environ if environ is None else environ
    environment_value = values.get(name)
    if environment_value:
        return environment_value

    credentials_directory = values.get("CREDENTIALS_DIRECTORY")
    if not credentials_directory:
        return None
    directory = Path(credentials_directory)
    credential = directory / name
    try:
        directory_resolved = directory.resolve(strict=True)
        if credential.is_symlink():
            raise ConfigError(f"credential {name} must not be a symlink")
        credential_resolved = credential.resolve(strict=True)
        if credential_resolved.parent != directory_resolved:
            raise ConfigError(f"credential {name} escapes CREDENTIALS_DIRECTORY")
        file_stat = credential_resolved.stat()
        if not stat.S_ISREG(file_stat.st_mode):
            raise ConfigError(f"credential {name} is not a regular file")
        if file_stat.st_size > _MAX_CREDENTIAL_SIZE:
            raise ConfigError(f"credential {name} exceeds {_MAX_CREDENTIAL_SIZE} bytes")
        value = credential_resolved.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except UnicodeError as exc:
        raise ConfigError(f"credential {name} is not valid UTF-8") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read credential {name}: {exc}") from exc
    return value or None


def require_secret(
    name: str, environ: Mapping[str, str] | None = None
) -> str:
    value = read_secret(name, environ)
    if value is None:
        raise ConfigError(
            f"required secret {name} is missing from the environment and systemd credentials"
        )
    return value
