"""Jekyll → dynamic-blog 마이그레이션.

기존 Jekyll(_posts/, assets/img/posts/) 디렉토리에서 글과 이미지를 가져와 우리 DB로 import.

핵심 정책:
- slug: 파일명에서 `YYYY-MM-DD-` prefix 제거 후 그대로 (Chirpy permalink 와 일치)
- 카테고리: `[대, 소]` → `"대/소"`. 1개면 그대로.
- 태그: front matter tags 그대로
- 이미지: `/assets/img/posts/<slug>/foo.png` 같은 절대 경로 → 우리 미디어 업로드 API 로 올린 뒤 URL 치환
- 외부 이미지 URL: 다운 시도 후 마찬가지로 업로드. 실패는 원본 URL 유지 + 로그
- `{% link _posts/X.md %}` Liquid 태그 → `/posts/<X 의 slug>`
- `pin: true`, `mermaid: true` 같은 Jekyll 메타: drop

dry-run: API 호출 없이 변환 결과만 출력. 검증용.
"""

from __future__ import annotations

import re
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

import frontmatter
import httpx
import typer
from rich.console import Console
from rich.table import Table

from blog.client import BlogClient
from blog.config import Config

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()

# 파일명 `2026-05-13-rag-practice-...md` → slug `rag-practice-...`
_FILENAME_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-(.+)\.(md|markdown)$")
# Liquid: {% link _posts/2026-05-13-foo.md %}  또는  {% link _posts/foo.md %}
_LIQUID_LINK_RE = re.compile(r"\{%\s*link\s+_posts/([^\s%]+)\s*%\}")
# 마크다운 이미지: ![alt](url)  — url 만 캡처
_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
# HTML img / video src
_HTML_SRC_RE = re.compile(r'<(?:img|video|source)\b[^>]*?\bsrc=["\']([^"\']+)["\']', re.IGNORECASE)


def _file_to_slug(filename: str) -> str:
    """`2026-05-13-foo-bar.md` → `foo-bar`. 형식 안 맞으면 stem 그대로."""
    m = _FILENAME_DATE_RE.match(filename)
    return m.group(2) if m else Path(filename).stem


def _date_from_filename(filename: str) -> str | None:
    m = _FILENAME_DATE_RE.match(filename)
    return m.group(1) if m else None


def _normalize_categories(meta: dict[str, Any]) -> str | None:
    """Jekyll `[대, 소]` → `"대/소"`. 단일이면 그대로."""
    cats = meta.get("categories") or meta.get("category")
    if cats is None:
        return None
    if isinstance(cats, str):
        return cats.strip() or None
    if isinstance(cats, list) and cats:
        return "/".join(str(c).strip() for c in cats if str(c).strip())
    return None


def _normalize_tags(meta: dict[str, Any]) -> list[str]:
    tags = meta.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]
    out: list[str] = []
    seen: set[str] = set()
    for t in tags:
        s = str(t).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _extract_cover(meta: dict[str, Any]) -> str | None:
    """`image: { path: ... }` 또는 `image: "..."` → cover_image_url."""
    img = meta.get("image")
    if isinstance(img, str) and img.strip():
        return img.strip()
    if isinstance(img, dict):
        for key in ("path", "src", "url"):
            v = img.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def _convert_liquid_links(body: str, jekyll_dir: Path) -> tuple[str, list[str]]:
    """`{% link _posts/X.md %}` → `/posts/<X 의 slug>`.
    파일이 실제 존재하지 않아도 변환은 시도 (slug 로직만으로). 못 찾으면 경고.
    """
    warnings: list[str] = []

    def sub(m: re.Match[str]) -> str:
        ref = m.group(1).strip()
        # ref 가 .md 확장자 들고 있을 수 있음
        if not ref.endswith((".md", ".markdown")):
            ref = ref + ".md"
        slug = _file_to_slug(Path(ref).name)
        # 실존 검증 (옵션)
        src = jekyll_dir / "_posts" / Path(ref).name
        if not src.exists():
            warnings.append(f"liquid link 미발견: {ref}")
        return f"/posts/{slug}"

    return _LIQUID_LINK_RE.sub(sub, body), warnings


_HEADING_RE = re.compile(r"^#+\s")
_HTML_TAG_LINE_RE = re.compile(r"^<[^>]+>")
_LINK_INLINE_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_MD_BOLD_ITALIC_RE = re.compile(r"[*_]{1,3}([^*_]+)[*_]{1,3}")


def _auto_excerpt(body: str, max_chars: int = 180) -> str:
    """본문에서 첫 의미 단락 추출 → max_chars 자.
    헤딩/이미지/HTML 태그/코드블록 스킵, 마크다운 강조 부호 제거.
    """
    in_code = False
    chunks: list[str] = []
    for raw in body.split("\n"):
        line = raw.strip()
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        if not line:
            if chunks:
                break
            continue
        if _HEADING_RE.match(line):
            continue
        if line.startswith("!["):
            continue
        if _HTML_TAG_LINE_RE.match(line):
            continue
        chunks.append(line)
    if not chunks:
        return ""
    text = " ".join(chunks)
    # 마크다운 inline 정리
    text = _LINK_INLINE_RE.sub(r"\1", text)
    text = _MD_BOLD_ITALIC_RE.sub(r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _collect_image_urls(body: str) -> list[str]:
    """본문에서 모든 이미지/비디오 URL 추출 (중복 제거, 등장 순서 보존)."""
    seen: set[str] = set()
    out: list[str] = []
    for m in _MD_IMAGE_RE.finditer(body):
        url = m.group(2).strip()
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    for m in _HTML_SRC_RE.finditer(body):
        url = m.group(1).strip()
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _resolve_local_image(jekyll_dir: Path, url: str) -> Path | None:
    """`/assets/img/...` 같이 사이트 루트 절대경로면 jekyll_dir 기준 실제 파일 경로 반환."""
    if url.startswith("/"):
        candidate = jekyll_dir / url.lstrip("/")
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def _download_external(url: str) -> bytes | None:
    """외부 URL 다운로드. 실패시 None."""
    try:
        with httpx.Client(timeout=30.0, follow_redirects=True) as c:
            r = c.get(url)
            if r.status_code == 200 and r.content:
                return r.content
    except Exception:
        return None
    return None


def _guess_filename(url: str) -> str:
    """URL 에서 파일명 추측. 없으면 임시명."""
    parsed = urllib.parse.urlparse(url)
    name = Path(parsed.path).name or "image"
    # 쿼리스트링 등 제거 후 안전한 이름으로
    return urllib.parse.unquote(name)


def _parse_post(path: Path, jekyll_dir: Path) -> dict[str, Any]:
    """frontmatter + body 파싱 + slug/카테고리/태그/cover 정규화 + Liquid 링크 변환."""
    post = frontmatter.load(str(path))
    meta = dict(post.metadata)
    body = post.content

    slug = _file_to_slug(path.name)

    title = meta.get("title")
    if not title:
        title = slug

    date_str: str | None = None
    raw_date = meta.get("date")
    if raw_date is not None:
        date_str = str(raw_date)
    else:
        d = _date_from_filename(path.name)
        if d:
            date_str = d

    # ISO 변환 시도 (서버는 string 받음)
    published_at: str | None = None
    if date_str:
        try:
            # `2026-05-13 20:55:00 +0900` 또는 `2026-05-13`
            cleaned = date_str.replace("/", "-")
            for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(cleaned, fmt)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    published_at = dt.isoformat()
                    break
                except ValueError:
                    continue
        except Exception:
            published_at = date_str

    body, liquid_warnings = _convert_liquid_links(body, jekyll_dir)

    payload: dict[str, Any] = {
        "title": str(title),
        "slug": slug,
        "body_md": body,
        "status": "published",
        "tags": _normalize_tags(meta),
    }
    cat = _normalize_categories(meta)
    if cat:
        payload["category"] = cat
    cover = _extract_cover(meta)
    if cover:
        payload["cover_image_url"] = cover
    if published_at:
        payload["published_at"] = published_at
    if meta.get("description"):
        payload["excerpt"] = str(meta["description"]).strip()
    elif meta.get("excerpt"):
        payload["excerpt"] = str(meta["excerpt"]).strip()
    else:
        payload["excerpt"] = _auto_excerpt(body)

    payload["_warnings"] = liquid_warnings  # private; dry-run 출력용
    payload["_image_urls"] = _collect_image_urls(body)
    return payload


def _ext_to_content_type(ext: str) -> str | None:
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".svg": "image/svg+xml",
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".mov": "video/quicktime",
    }.get(ext.lower())


def _upload_one(
    client: BlogClient,
    *,
    url: str,
    jekyll_dir: Path,
    cache: dict[str, str],
) -> str | None:
    """주어진 url(로컬 절대경로 or 외부 http) 을 우리 media API 로 업로드.
    성공 시 새 URL 반환. 실패면 None.
    cache: 같은 원본 url 은 한 번만 업로드.
    """
    if url in cache:
        return cache[url]

    # 1) 로컬 파일 (사이트 절대 경로)
    local = _resolve_local_image(jekyll_dir, url)
    data: bytes | None = None
    filename: str
    if local is not None:
        data = local.read_bytes()
        filename = local.name
    elif url.startswith(("http://", "https://")):
        data = _download_external(url)
        filename = _guess_filename(url)
    else:
        return None
    if not data:
        return None

    ext = Path(filename).suffix
    content_type = _ext_to_content_type(ext)
    if not content_type:
        # 확장자 없으면 png 기본 (안전한 fallback)
        content_type = "image/png"
        filename = filename + ".png"

    try:
        res = client.upload_bytes(filename, data, content_type)
    except Exception:
        return None
    new_url = res.get("url")
    if isinstance(new_url, str):
        cache[url] = new_url
        return new_url
    return None


def _rewrite_body(body: str, url_map: dict[str, str]) -> str:
    """본문에서 원본 URL 을 새 URL 로 치환. markdown 이미지 + HTML src."""
    if not url_map:
        return body

    def md_sub(m: re.Match[str]) -> str:
        alt, src = m.group(1), m.group(2).strip()
        new = url_map.get(src)
        return f"![{alt}]({new})" if new else m.group(0)

    def html_sub(m: re.Match[str]) -> str:
        src = m.group(1)
        new = url_map.get(src)
        if not new:
            return m.group(0)
        return m.group(0).replace(src, new)

    out = _MD_IMAGE_RE.sub(md_sub, body)
    out = _HTML_SRC_RE.sub(html_sub, out)
    return out


@app.command("dry-run")
def dry_run(
    jekyll_dir: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    limit: Annotated[int, typer.Option(help="처음 N 글만 미리보기")] = 5,
    show_body: Annotated[bool, typer.Option("--body", help="본문 일부 출력")] = False,
) -> None:
    """jekyll _posts/ 안의 .md 파일들을 파싱만 하고 결과 표로 보여줌. DB 변경 없음."""
    posts_dir = jekyll_dir / "_posts"
    if not posts_dir.exists():
        console.print(f"[red]{posts_dir} 없음. jekyll_dir 인자가 맞는지 확인[/red]")
        raise typer.Exit(2)

    files = sorted(posts_dir.glob("*.md"))
    console.print(f"[bold]총 글:[/bold] {len(files)}개 (앞에서 {limit}개만 표시)")
    console.print()

    table = Table(show_lines=True)
    table.add_column("slug", style="cyan", no_wrap=False)
    table.add_column("title")
    table.add_column("category", style="magenta")
    table.add_column("tags", style="green")
    table.add_column("이미지 수", justify="right")
    table.add_column("경고")

    all_warnings = 0
    for f in files[:limit]:
        try:
            p = _parse_post(f, jekyll_dir)
        except Exception as e:
            console.print(f"[red]✗ {f.name}: {e}[/red]")
            continue
        warns = p.pop("_warnings", [])
        img_urls = p.pop("_image_urls", [])
        all_warnings += len(warns)
        table.add_row(
            p["slug"],
            p["title"][:50],
            p.get("category", "-") or "-",
            ", ".join(p.get("tags", []))[:40] or "-",
            str(len(img_urls)),
            ("\n".join(warns) if warns else "-")[:60],
        )
        if show_body:
            console.print(f"\n[bold cyan]{p['slug']}[/bold cyan]")
            console.print(p["body_md"][:400] + ("..." if len(p["body_md"]) > 400 else ""))

    console.print(table)
    console.print(f"\n경고 합계: {all_warnings}")


@app.command("import")
def import_cmd(
    jekyll_dir: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    limit: Annotated[int, typer.Option(help="N 글만. 0=전체")] = 0,
    from_end: Annotated[bool, typer.Option("--from-end", help="최근 글부터 N개 가져옴 (기본은 오래된 글부터)")] = False,
    skip_existing: Annotated[bool, typer.Option("--skip-existing/--overwrite", help="동일 slug 글이 이미 있으면 건너뜀 (기본). overwrite 면 기존 글 update")] = True,
    update_only: Annotated[bool, typer.Option("--update-only", help="이미 있는 글만 update, 새로 만들지 않음")] = False,
) -> None:
    """jekyll _posts/ 의 글을 실제로 DB 에 import.
    이미지/asset 은 우리 미디어 API 로 업로드하고 본문 URL 을 치환.
    """
    posts_dir = jekyll_dir / "_posts"
    if not posts_dir.exists():
        console.print(f"[red]{posts_dir} 없음[/red]")
        raise typer.Exit(2)

    files = sorted(posts_dir.glob("*.md"))
    if limit > 0:
        files = files[-limit:] if from_end else files[:limit]
    console.print(f"[bold]대상 글:[/bold] {len(files)}개")

    with _client_for_migrate() as client:
        # 기존 slug 전체 조회 (페이지네이션). 200건 한도라 offset 으로 끝까지.
        # 이 단계에서 누락되면 중복 import 가 발생하므로 절대 limit=N 하나로 끝내지 말 것.
        existing: list[dict[str, Any]] = []
        existing_slugs: set[str] = set()
        try:
            offset = 0
            page_size = 200
            while True:
                batch = client.list_posts(admin=True, limit=page_size, offset=offset)
                if not batch:
                    break
                existing.extend(batch)
                if len(batch) < page_size:
                    break
                offset += page_size
            existing_slugs = {p["slug"] for p in existing}
            console.print(f"[dim]기존 글 {len(existing_slugs)}건 확인[/dim]")
        except Exception as e:
            console.print(f"[yellow]기존 글 목록 조회 실패 ({e}) — 중복 검사 없이 진행[/yellow]")

        image_cache: dict[str, str] = {}
        ok = skipped = failed = 0
        per_post_failed_images: dict[str, list[str]] = {}

        for i, f in enumerate(files, 1):
            try:
                payload = _parse_post(f, jekyll_dir)
            except Exception as e:
                console.print(f"[red]✗ parse {f.name}: {e}[/red]")
                failed += 1
                continue

            slug = payload["slug"]
            # 표준 frontmatter 키 외 _warnings, _image_urls 는 payload 에서 분리
            warnings = payload.pop("_warnings", [])
            image_urls = payload.pop("_image_urls", [])
            if warnings:
                for w in warnings:
                    console.print(f"[dim]  ⚠ {slug}: {w}[/dim]")

            # 이미지 업로드 + 본문 URL 치환
            url_map: dict[str, str] = {}
            for img_url in image_urls:
                new = _upload_one(client, url=img_url, jekyll_dir=jekyll_dir, cache=image_cache)
                if new:
                    url_map[img_url] = new
                else:
                    per_post_failed_images.setdefault(slug, []).append(img_url)
            if url_map:
                payload["body_md"] = _rewrite_body(payload["body_md"], url_map)
            # cover_image_url 도 같은 매핑으로 치환 (cover 가 본문 첫 이미지로 같은 URL 일 때)
            cover = payload.get("cover_image_url")
            if cover:
                if cover in url_map:
                    payload["cover_image_url"] = url_map[cover]
                else:
                    # cover 만 있고 본문엔 없는 경우도 업로드 시도
                    new_cover = _upload_one(client, url=cover, jekyll_dir=jekyll_dir, cache=image_cache)
                    if new_cover:
                        payload["cover_image_url"] = new_cover
                    else:
                        # 업로드 실패면 cover 제거 (404 회피)
                        per_post_failed_images.setdefault(slug, []).append(cover)
                        del payload["cover_image_url"]

            # DB 입력 — slug 중복 처리
            exists = slug in existing_slugs
            if exists and skip_existing and not update_only:
                console.print(f"  [yellow]skip[/yellow] {slug} (이미 있음)")
                skipped += 1
                continue
            if update_only and not exists:
                continue

            try:
                if exists:
                    # update: slug 로 id 조회 → patch
                    target_id = next((p["id"] for p in existing if p["slug"] == slug), None)
                    if target_id:
                        client.update_post(target_id, payload)
                        action = "updated"
                    else:
                        result = client.create_post(payload)
                        action = "created"
                else:
                    result = client.create_post(payload)
                    action = "created"
                    # 견고성: 서버가 자동 -2 접미사를 붙였으면 의도치 않은 중복이므로 경고.
                    # 우리 마이그는 기존 글과 같은 slug 면 위에서 skip 됐어야 함.
                    actual_slug = result.get("slug") if isinstance(result, dict) else None
                    if actual_slug and actual_slug != slug:
                        console.print(
                            f"  [bold yellow]WARN[/bold yellow] {slug} → 서버가 [yellow]{actual_slug}[/yellow] 로 저장 "
                            f"(slug 충돌). 기존 글과 중복 가능성 있음."
                        )
                console.print(f"  [green]{action}[/green] [{i}/{len(files)}] {slug}")
                ok += 1
            except Exception as e:
                console.print(f"  [red]fail[/red] {slug}: {e}")
                failed += 1

        console.print()
        console.print(f"[bold green]OK:[/bold green] {ok}  [bold yellow]skip:[/bold yellow] {skipped}  [bold red]fail:[/bold red] {failed}")
        if per_post_failed_images:
            console.print(f"\n[bold yellow]업로드 실패 이미지 — {len(per_post_failed_images)} 글:[/bold yellow]")
            for slug, urls in list(per_post_failed_images.items())[:20]:
                console.print(f"  {slug}: {len(urls)}건  e.g. {urls[0][:80]}")


def _client_for_migrate() -> BlogClient:
    cfg = Config.load()
    return BlogClient(cfg.api_url, cfg.token)


if __name__ == "__main__":
    app()
