from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx


class BlogClient:
    def __init__(self, api_url: str, token: str) -> None:
        self._client = httpx.Client(
            base_url=api_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )

    def __enter__(self) -> "BlogClient":
        return self

    def __exit__(self, *exc) -> None:
        self._client.close()

    def _raise(self, res: httpx.Response) -> None:
        if res.is_success:
            return
        raise RuntimeError(f"API {res.request.method} {res.request.url} → {res.status_code}: {res.text}")

    def me(self) -> dict[str, Any]:
        res = self._client.get("/api/auth/me")
        self._raise(res)
        return res.json()

    def list_posts(self, *, admin: bool = False, limit: int = 50) -> list[dict[str, Any]]:
        path = "/api/posts/admin" if admin else "/api/posts"
        res = self._client.get(path, params={"limit": limit})
        self._raise(res)
        return res.json()["items"]

    def create_post(self, payload: dict[str, Any]) -> dict[str, Any]:
        res = self._client.post("/api/posts", json=payload)
        self._raise(res)
        return res.json()

    def update_post(self, post_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        res = self._client.patch(f"/api/posts/{post_id}", json=payload)
        self._raise(res)
        return res.json()

    def style_guide(self) -> dict[str, Any]:
        res = self._client.get("/api/style-guide")
        self._raise(res)
        return res.json()

    def search(self, q: str, limit: int = 10, include_private: bool = False) -> list[dict[str, Any]]:
        res = self._client.get(
            "/api/search/posts",
            params={"q": q, "limit": limit, "include_private": include_private},
        )
        self._raise(res)
        return res.json()

    def similar(self, slug: str, limit: int = 5) -> list[dict[str, Any]]:
        res = self._client.get(f"/api/posts/{slug}/similar", params={"limit": limit})
        self._raise(res)
        return res.json()

    def upload_image(self, path: Path) -> dict[str, Any]:
        with path.open("rb") as fp:
            files = {"file": (path.name, fp, _guess_content_type(path))}
            res = self._client.post("/api/media/upload", files=files)
        self._raise(res)
        return res.json()

    def upload_bytes(self, filename: str, data: bytes, content_type: str) -> dict[str, Any]:
        files = {"file": (filename, data, content_type)}
        res = self._client.post("/api/media/upload", files=files)
        self._raise(res)
        return res.json()


_CONTENT_TYPE_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
}


def _guess_content_type(path: Path) -> str:
    return _CONTENT_TYPE_BY_EXT.get(path.suffix.lower(), "application/octet-stream")
