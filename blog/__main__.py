"""dynamic-blog 로컬 발행 CLI.

설치:
    cd cli && uv pip install -e .

설정 (둘 중 하나):
    1) ~/.config/dynamic-blog/config.toml
        api_url = "http://localhost:8000"
        token = "blogapi_..."
    2) 환경변수: BLOG_API_URL, BLOG_API_TOKEN

사용:
    blog whoami
    blog list
    blog publish path/to/post.md             # 새 글 작성
    blog publish path/to/post.md --update    # 기존 글 갱신 (frontmatter의 id로)
"""

from __future__ import annotations

import re as _re
import sys
from pathlib import Path
from typing import Annotated, Optional

import frontmatter
import typer
from rich.console import Console
from rich.table import Table

from blog.client import BlogClient
from blog.config import Config
from blog.migrate import app as migrate_app

app = typer.Typer(no_args_is_help=True, add_completion=False)
app.add_typer(migrate_app, name="migrate", help="Jekyll → DB 마이그레이션")
console = Console()


def _client() -> BlogClient:
    cfg = Config.load()
    return BlogClient(cfg.api_url, cfg.token)


@app.command()
def whoami() -> None:
    """현재 토큰으로 인증된 사용자 정보."""
    with _client() as c:
        info = c.me()
    console.print(info)


@app.command("list")
def list_cmd(
    admin: Annotated[bool, typer.Option("--admin", help="draft 포함")] = False,
    limit: int = 50,
) -> None:
    """발행된(또는 전체) 글 목록."""
    with _client() as c:
        posts = c.list_posts(admin=admin, limit=limit)

    table = Table(show_lines=False)
    table.add_column("date", style="cyan")
    table.add_column("status", style="magenta")
    table.add_column("title")
    table.add_column("slug", style="dim")
    for p in posts:
        date = (p.get("published_at") or p["created_at"])[:10]
        table.add_row(date, p["status"], p["title"], p["slug"])
    console.print(table)


_VIDEO_OPEN_RE = _re.compile(r"<video\b([^>]*)>", _re.IGNORECASE)


def _normalize_video_tags(body: str) -> str:
    """본문의 <video> 태그를 표준 형태로 보강.

    규칙:
    - preload 속성 없으면 'metadata' 추가 (재생 전 첫 프레임 빠르게).
    - autoplay+loop+muted 조합이면 (GIF 대체 의도) controls 안 박음.
    - 그 외에는 controls 자동 추가 (재생 컨트롤 의도).
    - aria-label 없으면 '동영상' 추가 (a11y).
    글 작성마다 사용자가 깜빡해도 일관된 패턴 보장.
    """
    def repl(m):
        attrs = m.group(1) or ""
        lo = attrs.lower()
        is_gif_like = "autoplay" in lo and "loop" in lo and "muted" in lo
        out = attrs
        if "preload=" not in lo:
            out += ' preload="metadata"'
        if not is_gif_like and "controls" not in lo:
            out += " controls"
        if "aria-label=" not in lo:
            out += ' aria-label="동영상"'
        return f"<video{out}>"
    return _VIDEO_OPEN_RE.sub(repl, body)


def _parse_post(path: Path) -> tuple[dict, str]:
    """Frontmatter + body로 분리. Jekyll 호환 키 우선."""
    post = frontmatter.load(str(path))
    meta = dict(post.metadata)
    body = _normalize_video_tags(post.content)

    tags = meta.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]

    # Jekyll에선 categories가 다수일 수 있는데, 우리는 단일 category. 첫 값만 사용.
    category = meta.get("category")
    if category is None:
        cats = meta.get("categories")
        if isinstance(cats, list) and cats:
            category = cats[0]
        elif isinstance(cats, str) and cats.strip():
            category = cats.strip()

    # 대표 이미지: cover_image > image > header.image (Jekyll 호환)
    cover_image_url: str | None = None
    for key in ("cover_image", "image"):
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            cover_image_url = value.strip()
            break
        if isinstance(value, dict):
            # Jekyll 형식: image: { path: ... } 또는 image: { src: ... }
            nested = value.get("path") or value.get("src") or value.get("url")
            if isinstance(nested, str) and nested.strip():
                cover_image_url = nested.strip()
                break
    if cover_image_url is None:
        header = meta.get("header")
        if isinstance(header, dict):
            nested = header.get("image") or header.get("teaser")
            if isinstance(nested, str) and nested.strip():
                cover_image_url = nested.strip()
    # 본문 첫 이미지 자동 추출 fallback (frontmatter에 명시 안 된 경우)
    if cover_image_url is None:
        m = _re.search(r"!\[[^\]]*\]\(([^)\s]+)", body)
        if m:
            cover_image_url = m.group(1).strip()

    status = meta.get("status")
    if status is None:
        status = "draft" if meta.get("draft") else "published"

    payload = {
        "title": meta.get("title") or path.stem,
        "body_md": body,
        "status": status,
        "tags": list(tags),
    }
    if category:
        payload["category"] = str(category)
    if cover_image_url:
        payload["cover_image_url"] = cover_image_url
    if "slug" in meta:
        payload["slug"] = meta["slug"]
    if "date" in meta:
        payload["published_at"] = str(meta["date"])
    if "excerpt" in meta:
        payload["excerpt"] = meta["excerpt"]

    return payload, meta.get("id")


# 본문의 로컬 미디어 경로 자동 감지/업로드용 정규식.
_MD_IMG_LOCAL = _re.compile(r"!\[([^\]]*)\]\(((?!https?://|#|data:|/api/media/)[^)\s]+)")
_HTML_SRC_LOCAL = _re.compile(
    r'<(?:img|source|video|audio)\b[^>]*?\bsrc=["\']((?!https?://|#|data:|/api/media/)[^"\'\s]+)',
    _re.IGNORECASE,
)


def _resolve_local_media(md_path: Path, ref: str) -> Path | None:
    """본문에 박힌 상대 경로(또는 file:// 절대)를 .md 위치 기준 실제 파일로."""
    if ref.startswith("file://"):
        p = Path(ref.removeprefix("file://"))
    elif ref.startswith("/"):
        # 절대 경로 그대로
        p = Path(ref)
    else:
        # 상대 경로 — .md 파일 위치 기준
        p = (md_path.parent / ref).resolve()
    return p if p.is_file() else None


def _auto_upload_body_media(
    body: str, md_path: Path, client_factory
) -> tuple[str, int]:
    """본문의 로컬 미디어 경로를 모두 업로드 → URL 치환.
    반환: (새 본문, 업로드한 파일 수)
    """
    cache: dict[str, str] = {}

    def upload(ref: str) -> str | None:
        if ref in cache:
            return cache[ref]
        local = _resolve_local_media(md_path, ref)
        if local is None:
            return None
        with client_factory() as c:
            res = c.upload_image(local)
        new = res.get("url")
        if isinstance(new, str):
            cache[ref] = new
            return new
        return None

    def md_sub(m):
        alt, src = m.group(1), m.group(2).strip()
        new = upload(src)
        return f"![{alt}]({new})" if new else m.group(0)

    def html_sub(m):
        src = m.group(1)
        new = upload(src)
        if not new:
            return m.group(0)
        return m.group(0).replace(src, new)

    out = _MD_IMG_LOCAL.sub(md_sub, body)
    out = _HTML_SRC_LOCAL.sub(html_sub, out)
    return out, len(cache)


@app.command()
def publish(
    path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    update: Annotated[bool, typer.Option("--update", "-u", help="frontmatter의 id로 기존 글 갱신")] = False,
    draft: Annotated[bool, typer.Option("--draft", help="draft 상태로 강제")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="발행 안 하고 업로드 대상만 출력")] = False,
) -> None:
    """로컬 .md 파일을 API로 발행.

    본문에 로컬 경로(`./images/foo.png`, `../assets/bar.mp4`) 가 있으면
    자동으로 우리 미디어 API 에 업로드 + URL 치환 후 글 발행.
    """
    payload, existing_id = _parse_post(path)
    if draft:
        payload["status"] = "draft"

    if dry_run:
        # 본문에서 로컬 ref 찾기만
        body = payload.get("body_md", "")
        refs = set()
        for m in _MD_IMG_LOCAL.finditer(body):
            refs.add(m.group(2).strip())
        for m in _HTML_SRC_LOCAL.finditer(body):
            refs.add(m.group(1))
        console.print(f"[blue]dry-run[/blue]: 본문 로컬 미디어 참조 {len(refs)} 개")
        for r in refs:
            local = _resolve_local_media(path, r)
            console.print(f"  {r}  → {'[green]ok[/green]' if local else '[red]not found[/red]'}")
        console.print(f"\ntitle: {payload.get('title')}")
        console.print(f"status: {payload.get('status')}")
        console.print(f"slug: {payload.get('slug', '(auto)')}")
        return

    # 본문 로컬 미디어 자동 업로드
    body = payload.get("body_md", "")
    new_body, n_uploaded = _auto_upload_body_media(body, path, _client)
    if n_uploaded:
        console.print(f"[blue]본문 미디어 자동 업로드: {n_uploaded}개[/blue]")
        payload["body_md"] = new_body

    with _client() as c:
        if update:
            if not existing_id:
                console.print("[red]--update 모드는 frontmatter에 id: <uuid>가 있어야 합니다[/red]")
                raise typer.Exit(2)
            result = c.update_post(existing_id, payload)
            console.print(f"[green]✓ updated[/green] {result['slug']}")
        else:
            result = c.create_post(payload)
            console.print(f"[green]✓ created[/green] {result['slug']}")
            console.print(f"  id: {result['id']}")
            console.print(f"  (frontmatter에 id를 추가하면 다음에 --update로 갱신 가능)")


@app.command("upload-image")
def upload_image(path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)]) -> None:
    """이미지 업로드. 반환된 URL을 마크다운에 그대로 붙여넣기."""
    with _client() as c:
        result = c.upload_image(path)
    console.print(result["url"])


@app.command()
def search(
    q: Annotated[str, typer.Argument(help="검색어")],
    limit: int = 10,
    private: Annotated[bool, typer.Option("--private", help="비공개 글 포함")] = False,
) -> None:
    """제목/본문/발췌 키워드 검색."""
    with _client() as c:
        items = c.search(q, limit=limit, include_private=private)
    if not items:
        console.print("[yellow]일치하는 글 없음[/yellow]")
        return
    table = Table()
    table.add_column("date", style="cyan")
    table.add_column("status", style="magenta")
    table.add_column("title")
    for p in items:
        date = (p.get("published_at") or p["created_at"])[:10]
        table.add_row(date, p["status"], p["title"])
    console.print(table)


@app.command("style-guide")
def style_guide_cmd() -> None:
    """글쓰기 스타일 요약 — 로컬 AI 컨텍스트용."""
    with _client() as c:
        guide = c.style_guide()
    console.print(f"[bold]총 글 수:[/bold] {guide['total_posts']}")
    console.print(f"[bold]평균 글 길이:[/bold] {guide['avg_body_chars']}자")
    console.print()
    console.print("[bold]자주 쓰는 태그:[/bold]")
    for t in guide["top_tags"][:10]:
        console.print(f"  - {t['name']} ({t['count']})")
    if guide["top_categories"]:
        console.print()
        console.print("[bold]자주 쓰는 카테고리:[/bold]")
        for cat in guide["top_categories"]:
            console.print(f"  - {cat['name']} ({cat['count']})")
    console.print()
    console.print("[bold]최근 글 제목 (상위 10):[/bold]")
    for p in guide["recent_posts"][:10]:
        console.print(f"  - {p['title']}")


@app.command()
def similar(slug: str, limit: int = 5) -> None:
    """slug와 비슷한 글 (태그 기반)."""
    with _client() as c:
        items = c.similar(slug, limit=limit)
    if not items:
        console.print("[yellow]비슷한 글 없음[/yellow]")
        return
    for p in items:
        console.print(f"- {p['title']} [dim]({p['slug']})[/dim]")


if __name__ == "__main__":
    try:
        app()
    except RuntimeError as e:
        console.print(f"[red]error:[/red] {e}", style=None)
        sys.exit(1)
