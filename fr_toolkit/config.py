"""Configuration loading for the ForgeRock data toolkit.

Secrets are never hardcoded and never committed. Values resolve from
(in increasing precedence):

1. built-in defaults
2. an optional JSON config file (see examples/config.example.json)
3. environment variables (FR_IDM_*), which always win

Environment variables:
    FR_IDM_BASE_URL       e.g. https://idm.example.com:8443/openidm (required)
    FR_IDM_USERNAME       service account username
    FR_IDM_PASSWORD       service account password
    FR_IDM_VERIFY_SSL     "true"/"false" (default "true")
    FR_IDM_TIMEOUT        request timeout in seconds (default 30)
    FR_IDM_PAGE_SIZE      page size for paged queries (default 100)
    FR_IDM_MAX_RETRIES    retries on 429/5xx (default 3)
    FR_IDM_BACKOFF        base backoff in seconds (default 1.0)
    FR_IDM_RATE_LIMIT     max requests per second (default 10)
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import List, Optional


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        return int(raw) if raw is not None else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    try:
        return float(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default


@dataclass
class ToolkitConfig:
    """Connection and behaviour settings for the toolkit."""

    base_url: str
    username: str = ""
    password: str = ""
    verify_ssl: bool = True
    timeout: int = 30
    page_size: int = 100
    max_retries: int = 3
    backoff_factor: float = 1.0
    rate_limit_per_second: float = 10.0
    correlation_attributes: List[str] = field(
        default_factory=lambda: ["userName", "mail"]
    )

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")

    def validate(self) -> None:
        """Raise if the config cannot be used to talk to IDM."""
        if not self.base_url:
            raise ValueError(
                "base_url is required: set FR_IDM_BASE_URL or provide a config file"
            )

    @classmethod
    def from_file(cls, path: str) -> "ToolkitConfig":
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    @classmethod
    def _apply_env(cls, cfg: "ToolkitConfig") -> "ToolkitConfig":
        """Overlay any set FR_IDM_* variables on top of an existing config."""
        mapping = {
            "FR_IDM_BASE_URL": ("base_url", str),
            "FR_IDM_USERNAME": ("username", str),
            "FR_IDM_PASSWORD": ("password", str),
            "FR_IDM_VERIFY_SSL": ("verify_ssl", lambda v: v.strip().lower() in {"1", "true", "yes", "y", "on"}),
            "FR_IDM_TIMEOUT": ("timeout", int),
            "FR_IDM_PAGE_SIZE": ("page_size", int),
            "FR_IDM_MAX_RETRIES": ("max_retries", int),
            "FR_IDM_BACKOFF": ("backoff_factor", float),
            "FR_IDM_RATE_LIMIT": ("rate_limit_per_second", float),
        }
        for env_name, (attr, conv) in mapping.items():
            raw = os.getenv(env_name)
            if raw is not None:
                try:
                    setattr(cfg, attr, conv(raw))
                except (TypeError, ValueError):
                    pass  # keep the file/default value on bad input
        corr = os.getenv("FR_IDM_CORRELATION_ATTRS")
        if corr:
            cfg.correlation_attributes = [c.strip() for c in corr.split(",") if c.strip()]
        cfg.base_url = cfg.base_url.rstrip("/")
        return cfg

    @classmethod
    def load(cls, path: Optional[str] = None) -> "ToolkitConfig":
        """Load config from an optional JSON file, then overlay environment."""
        cfg = cls.from_file(path) if path else cls(base_url=os.getenv("FR_IDM_BASE_URL", ""))
        return cls._apply_env(cfg)

    def redacted(self) -> dict:
        """Config as a dict with the password masked (safe to log)."""
        return {
            "base_url": self.base_url,
            "username": self.username,
            "password": "***" if self.password else "",
            "verify_ssl": self.verify_ssl,
            "timeout": self.timeout,
            "page_size": self.page_size,
            "max_retries": self.max_retries,
            "rate_limit_per_second": self.rate_limit_per_second,
            "correlation_attributes": self.correlation_attributes,
        }
