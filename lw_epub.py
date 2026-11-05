"""Build EPUB ebooks from LessWrong sequences using the LessWrong GraphQL API.

A rewrite of bartbussmann/lesswrong_ebook_library (MIT). Instead of driving a
headless Chrome per post, it asks the LessWrong API for the sequence, its
author and every post's HTML, then fetches images in parallel.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import io
import re
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from ebooklib import epub

__version__ = "0.1.0"

API = "https://www.lesswrong.com/graphql"
SITE = "https://www.lesswrong.com"
CLOUDINARY = "https://res.cloudinary.com/lesswrong-2-0/image/upload/"
USER_AGENT = f"lw-epub/{__version__} (+https://github.com/float3/lw-epub)"

# Formats most e-readers display. Anything else is converted to PNG if Pillow
# is installed, or left as a remote link if not.
READER_IMAGE_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/svg+xml": "svg",
}

SEQUENCE_QUERY = """
query Sequence($id: String) {
  sequence(input: {selector: {_id: $id}}) {
    result {
      _id
      title
      gridImageId
      bannerImageId
      user { displayName }
      contents { html }
      chapters {
        title
        number
        posts { _id title slug postedAt user { displayName } }
      }
    }
  }
}
"""

POST_QUERY = """
query Post($id: String) {
  post(input: {selector: {_id: $id}}) {
    result {
      _id
      title
      slug
      postedAt
      user { displayName }
      coauthors { displayName }
      contents { html }
    }
  }
}
"""

CSS = """
body { font-family: serif; line-height: 1.5; }
h1.post-title { margin: 1em 0 0.25em; }
p.byline { margin: 0 0 1.5em; font-style: italic; }
p.source { margin-top: 2em; font-size: 0.8em; }
img { max-width: 100%; height: auto; }
blockquote { margin: 1em 1.5em; font-style: italic; }
pre, code { font-family: monospace; white-space: pre-wrap; }
"""


@dataclass
class Session:
    """HTTP session with retries and backoff, shared by all workers."""

    retries: int = 5
    delay: float = 0.0
    http: requests.Session = field(default_factory=requests.Session)

    def __post_init__(self) -> None:
        self.http.headers["User-Agent"] = USER_AGENT

    def request(self, method: str, url: str, **kw) -> requests.Response:
        wait = 2.0
        for attempt in range(self.retries + 1):
            if self.delay:
                time.sleep(self.delay)
            try:
                r = self.http.request(method, url, timeout=60, **kw)
            except requests.RequestException:
                if attempt == self.retries:
                    raise
            else:
                if r.status_code not in (429, 500, 502, 503, 504):
                    r.raise_for_status()
                    return r
                if attempt == self.retries:
                    r.raise_for_status()
                retry_after = r.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    wait = max(wait, float(retry_after))
            time.sleep(wait)
            wait = min(wait * 2, 120)
        raise RuntimeError("unreachable")

    def graphql(self, query: str, variables: dict) -> dict:
        r = self.request("POST", API, json={"query": query, "variables": variables})
        data = r.json()
        if data.get("errors"):
            raise RuntimeError(f"LessWrong API error: {data['errors'][0].get('message')}")
        return data["data"]


def sequence_id(arg: str) -> str:
    """Accept a sequence URL (https://www.lesswrong.com/s/<id>) or a bare id."""
    m = re.search(r"/s/([A-Za-z0-9]+)", arg)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9]+", arg):
        return arg
    raise ValueError(f"not a LessWrong sequence URL or id: {arg}")


def sequence_ids_from_html(path: Path) -> list[str]:
    """Collect sequence ids linked from a saved HTML page (e.g. the Library)."""
    soup = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
    ids = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/s/" in href and "/p/" not in href:
            try:
                sid = sequence_id(href)
            except ValueError:
                continue
            if sid not in ids:
                ids.append(sid)
    return ids


def slugify(title: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-").lower()
    return s or "untitled"


def name(user: dict | None) -> str:
    return (user or {}).get("displayName") or "Unknown"


@dataclass
class Image:
    uid: str
    file_name: str
    media_type: str
    content: bytes


def fetch_image(session: Session, url: str) -> Image | None:
    """Download one image; convert to PNG if readers would not show it."""
    try:
        r = session.request("GET", url)
    except Exception as e:  # noqa: BLE001 - an image must never fail the book
        print(f"  image skipped ({e}): {url}", file=sys.stderr)
        return None
    media_type = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
    content = r.content
    if media_type not in READER_IMAGE_TYPES:
        try:
            from PIL import Image as PILImage
        except ImportError:
            return None
        try:
            with PILImage.open(io.BytesIO(content)) as im:
                buf = io.BytesIO()
                im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB").save(buf, "PNG")
            content, media_type = buf.getvalue(), "image/png"
        except Exception:  # noqa: BLE001
            return None
    digest = hashlib.sha1(url.encode()).hexdigest()[:16]
    ext = READER_IMAGE_TYPES[media_type]
    return Image(f"img-{digest}", f"images/{digest}.{ext}", media_type, content)


def clean_post_html(raw: str) -> BeautifulSoup:
    soup = BeautifulSoup(raw or "", "html.parser")
    for tag in soup(["script", "style", "iframe", "form", "button"]):
        tag.decompose()
    # Absolute links so they still work from inside the book.
    for a in soup.find_all("a", href=True):
        a["href"] = urljoin(SITE, a["href"])
    return soup


def build_sequence(session: Session, sid: str, out_dir: Path, jobs: int, force: bool) -> Path | None:
    seq = session.graphql(SEQUENCE_QUERY, {"id": sid})["sequence"]["result"]
    if not seq:
        print(f"sequence {sid}: not found", file=sys.stderr)
        return None
    title = seq["title"] or sid
    author = name(seq.get("user"))
    out = out_dir / f"{slugify(title)}.epub"
    if out.exists() and not force:
        print(f"{title}: {out} exists, skipping (use --force to rebuild)")
        return out

    chapters = sorted(seq.get("chapters") or [], key=lambda c: c.get("number") or 0)
    post_refs = [(ci, p) for ci, c in enumerate(chapters) for p in c.get("posts") or []]
    print(f"{title} by {author}: {len(post_refs)} posts")

    # Post bodies are fetched one per request; the sequence query stays small
    # so LessWrong does not time out on long sequences.
    def get_post(ref):
        return session.graphql(POST_QUERY, {"id": ref[1]["_id"]})["post"]["result"]

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        posts = list(pool.map(get_post, post_refs))

    book = epub.EpubBook()
    book.set_identifier(f"lesswrong-sequence-{sid}")
    book.set_title(title)
    book.add_author(author)
    book.set_language("en")
    book.add_metadata("DC", "source", f"{SITE}/s/{sid}")
    book.add_metadata("DC", "publisher", "LessWrong")

    style = epub.EpubItem(uid="style", file_name="style/book.css", media_type="text/css", content=CSS)
    book.add_item(style)

    cover_id = seq.get("gridImageId") or seq.get("bannerImageId")
    if cover_id:
        cover = fetch_image(session, CLOUDINARY + cover_id)
        if cover:
            book.set_cover(f"cover.{READER_IMAGE_TYPES[cover.media_type]}", cover.content)

    # Parse every post, then fetch all distinct images in parallel.
    soups, image_urls = [], {}
    for (_, ref), post in zip(post_refs, posts):
        soup = clean_post_html(((post or {}).get("contents") or {}).get("html", ""))
        for img in soup.find_all("img", src=True):
            src = urljoin(SITE, img["src"])
            if urlparse(src).scheme in ("http", "https"):
                image_urls.setdefault(src, None)
        soups.append(soup)
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        for src, im in zip(image_urls, pool.map(lambda u: fetch_image(session, u), image_urls)):
            image_urls[src] = im
    for im in {id(i): i for i in image_urls.values() if i}.values():
        book.add_item(epub.EpubImage(uid=im.uid, file_name=im.file_name, media_type=im.media_type, content=im.content))

    intro_html = ((seq.get("contents") or {}).get("html") or "").strip()
    spine, toc_sections, current = ["nav"], [], None
    if intro_html:
        intro = epub.EpubHtml(title="About this sequence", file_name="intro.xhtml", lang="en")
        intro.content = f"<h1>{html.escape(title)}</h1><p class='byline'>{html.escape(author)}</p>{clean_post_html(intro_html)}"
        intro.add_item(style)
        book.add_item(intro)
        spine.append(intro)
        toc_sections.append(intro)

    for n, ((ci, ref), post, soup) in enumerate(zip(post_refs, posts, soups), 1):
        post = post or {}
        for img in soup.find_all("img", src=True):
            im = image_urls.get(urljoin(SITE, img["src"]))
            if im:
                img["src"] = im.file_name
                img.attrs.pop("srcset", None)
                img.attrs.pop("sizes", None)
        p_title = post.get("title") or ref.get("title") or f"Post {n}"
        authors = [name(post.get("user"))] + [name(u) for u in post.get("coauthors") or []]
        posted = (post.get("postedAt") or "")[:10]
        byline = ", ".join(authors) + (f" · {posted}" if posted else "")
        url = f"{SITE}/s/{sid}/p/{ref['_id']}"
        ch = epub.EpubHtml(title=p_title, file_name=f"post-{n:03d}.xhtml", lang="en")
        ch.content = (
            f"<h1 class='post-title'>{html.escape(p_title)}</h1>"
            f"<p class='byline'>{html.escape(byline)}</p>{soup}"
            f"<p class='source'>Source: <a href='{url}'>{url}</a></p>"
        )
        ch.add_item(style)
        book.add_item(ch)
        spine.append(ch)

        section_title = chapters[ci].get("title")
        if section_title and len(chapters) > 1:
            if current is None or current[0].title != section_title:
                current = (epub.Section(section_title), [])
                toc_sections.append(current)
            current[1].append(ch)
        else:
            current = None
            toc_sections.append(ch)

    book.toc = toc_sections
    book.spine = spine
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(f".{uuid.uuid4().hex[:8]}.tmp")
    epub.write_epub(str(tmp), book)
    tmp.replace(out)
    print(f"  wrote {out} ({len(posts)} posts, {sum(1 for i in image_urls.values() if i)} images)")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="lw-epub",
        description="Build EPUB ebooks from LessWrong sequences via the LessWrong API.",
    )
    ap.add_argument("sequences", nargs="*", help="sequence URLs (https://www.lesswrong.com/s/<id>) or ids")
    ap.add_argument("--from-html", type=Path, action="append", default=[], metavar="FILE",
                    help="also build every sequence linked from a saved HTML page (repeatable)")
    ap.add_argument("-o", "--output", type=Path, default=Path("."), help="output directory (default: .)")
    ap.add_argument("-j", "--jobs", type=int, default=4, help="parallel downloads (default: 4)")
    ap.add_argument("--delay", type=float, default=0.0, help="seconds to wait before each request (default: 0)")
    ap.add_argument("-f", "--force", action="store_true", help="rebuild books that already exist")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = ap.parse_args(argv)

    ids: list[str] = []
    try:
        for s in args.sequences:
            ids.append(sequence_id(s))
    except ValueError as e:
        ap.error(str(e))
    for f in args.from_html:
        ids += sequence_ids_from_html(f)
    ids = list(dict.fromkeys(ids))
    if not ids:
        ap.error("give at least one sequence URL or id, or --from-html")

    session = Session(delay=args.delay)
    failed = 0
    for sid in ids:
        try:
            build_sequence(session, sid, args.output, max(1, args.jobs), args.force)
        except Exception as e:  # noqa: BLE001 - keep going with the other books
            failed += 1
            print(f"sequence {sid}: failed: {e}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
