"""사용자 설정 (~/.config/dynamic-blog/config.toml)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


def _config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "dynamic-blog" / "config.toml"


@dataclass
class Config:
    api_url: str
    token: str

    @classmethod
    def load(cls) -> Config:
        env_url = os.environ.get("BLOG_API_URL")
        env_token = os.environ.get("BLOG_API_TOKEN")

        file_data: dict = {}
        path = _config_path()
        if path.exists():
            file_data = tomllib.loads(path.read_text())

        api_url = env_url or file_data.get("api_url") or "http://localhost:8000"
        token = env_token or file_data.get("token")
        if not token:
            raise RuntimeError(
                "API token not configured.\n"
                f"  Set BLOG_API_TOKEN env var, or create {path} with:\n"
                '    api_url = "http://localhost:8000"\n'
                '    token = "blogapi_..."\n'
            )
        return cls(api_url=api_url.rstrip("/"), token=token)
