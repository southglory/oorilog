# blog-cli

[oorilog](https://oorilog.com) 블로그 플랫폼용 로컬 발행 CLI.
로컬 마크다운 파일을 그대로 발행하고, 본문의 로컬 이미지는 자동 업로드합니다.
AI 와 함께 글을 쓰고 한 줄로 발행하는 워크플로를 위해 만들었습니다.

> 이 레포는 CLI 만 담은 공개 레포입니다. 서버 본체는 별도(비공개)입니다.

## 설치

```bash
git clone https://github.com/southglory/blog-cli.git
cd blog-cli
pip install -e .          # 또는: uv pip install -e .
```

Python 3.11+ 필요.

## 설정

토큰은 블로그 웹에서 발급합니다: 로그인 → **설정 → 개발자 / API 토큰**.

환경변수:

```bash
export BLOG_API_URL="https://oorilog.com"
export BLOG_API_TOKEN="blogapi_..."
```

또는 설정 파일 `~/.config/dynamic-blog/config.toml`:

```toml
api_url = "https://oorilog.com"
token   = "blogapi_..."
```

자체 호스팅한 다른 인스턴스도 `BLOG_API_URL` 만 바꾸면 됩니다.

## 사용

```bash
blog whoami                   # 토큰 주인 확인
blog publish post.md          # 발행(로컬 이미지 자동 업로드)
blog publish post.md --draft  # 초안으로
blog publish post.md --update # 수정(frontmatter 에 id 필요)
blog publish post.md --dry-run
blog list --admin             # 내 글(초안 포함)
blog upload-image cover.png   # 이미지만 업로드 → url
blog search "키워드" --private
blog style-guide              # 내 글 톤·분류 요약(AI 컨텍스트)
blog similar my-slug
```

## 마크다운 / frontmatter

```markdown
---
title: "글 제목"
slug: my-slug            # 선택(비우면 자동)
category: 개발/Python     # 단일, '대/소' 계층 가능
tags: [FastAPI, RAG]
status: published        # draft | published | private
excerpt: 한 줄 요약
cover_image: ./cover.png # 로컬 경로면 발행 시 자동 업로드
id: <uuid>               # --update 수정 시에만
---

## 본문은 H2 부터

글 제목이 자동 H1 이 되니 본문엔 `#`(H1) 을 쓰지 마세요.

![대표 이미지](./images/shot.png)
```

전체 API 레퍼런스: https://oorilog.com/developers · https://oorilog.com/llms-full.txt
