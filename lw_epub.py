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
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from ebooklib import epub

__version__ = "0.2.0"

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


@dataclass
class Part:
    """One sequence, fetched: its metadata, chapters and post bodies."""

    sid: str
    title: str
    author: str
    intro_html: str
    cover_id: str | None
    chapters: list
    refs: list  # (chapter index, post ref) in reading order
    posts: list  # full post results, aligned with refs


def fetch_sequence(session: Session, sid: str, jobs: int) -> Part | None:
    seq = session.graphql(SEQUENCE_QUERY, {"id": sid})["sequence"]["result"]
    if not seq:
        print(f"sequence {sid}: not found", file=sys.stderr)
        return None
    chapters = sorted(seq.get("chapters") or [], key=lambda c: c.get("number") or 0)
    refs = [(ci, p) for ci, c in enumerate(chapters) for p in c.get("posts") or []]
    title = seq["title"] or sid

    # Post bodies are fetched one per request; the sequence query stays small
    # so LessWrong does not time out on long sequences.
    def get_post(ref):
        return session.graphql(POST_QUERY, {"id": ref[1]["_id"]})["post"]["result"]

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        posts = list(pool.map(get_post, refs))

    # Credit whoever wrote most of the posts: sequences are often assembled by
    # a curator (e.g. a LessWrong admin), whose name is on the sequence itself.
    writers = Counter(name((p or {}).get("user")) for p in posts)
    author = writers.most_common(1)[0][0] if writers else name(seq.get("user"))
    print(f"{title} by {author}: {len(refs)} posts")
    return Part(
        sid=sid,
        title=title,
        author=author,
        intro_html=((seq.get("contents") or {}).get("html") or "").strip(),
        cover_id=seq.get("gridImageId") or seq.get("bannerImageId"),
        chapters=chapters,
        refs=refs,
        posts=posts,
    )


def write_book(session: Session, parts: list[Part], title: str, out: Path, jobs: int,
               author: str | None = None) -> Path:
    """Write one EPUB. With one part it is a normal sequence book; with several,
    each sequence becomes a section of the table of contents."""
    combined = len(parts) > 1
    authors = [author] if author else list(dict.fromkeys(p.author for p in parts))
    book = epub.EpubBook()
    book.set_identifier("lesswrong-" + "-".join(p.sid for p in parts))
    book.set_title(title)
    for a in authors:
        book.add_author(a)
    book.set_language("en")
    for p in parts:
        book.add_metadata("DC", "source", f"{SITE}/s/{p.sid}")
    book.add_metadata("DC", "publisher", "LessWrong")

    style = epub.EpubItem(uid="style", file_name="style/book.css", media_type="text/css", content=CSS)
    book.add_item(style)

    cover_id = next((p.cover_id for p in parts if p.cover_id), None)
    if cover_id:
        cover = fetch_image(session, CLOUDINARY + cover_id)
        if cover:
            book.set_cover(f"cover.{READER_IMAGE_TYPES[cover.media_type]}", cover.content)

    # Parse every post, then fetch all distinct images in parallel.
    image_urls: dict = {}
    soups = []
    for p in parts:
        part_soups = []
        for post in p.posts:
            soup = clean_post_html(((post or {}).get("contents") or {}).get("html", ""))
            for img in soup.find_all("img", src=True):
                src = urljoin(SITE, img["src"])
                if urlparse(src).scheme in ("http", "https"):
                    image_urls.setdefault(src, None)
            part_soups.append(soup)
        soups.append(part_soups)
    urls = list(image_urls)
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        for src, im in zip(urls, pool.map(lambda u: fetch_image(session, u), urls)):
            image_urls[src] = im
    for im in {i.uid: i for i in image_urls.values() if i}.values():
        book.add_item(epub.EpubImage(uid=im.uid, file_name=im.file_name, media_type=im.media_type, content=im.content))

    spine, toc, n = ["nav"], [], 0
    for pi, (p, part_soups) in enumerate(zip(parts, soups), 1):
        if combined:
            part_toc: list = []
            toc.append((epub.Section(p.title), part_toc))
        else:
            part_toc = toc
        if p.intro_html:
            intro = epub.EpubHtml(title=p.title if combined else "About this sequence",
                                  file_name=f"intro-{pi:02d}.xhtml", lang="en")
            intro.content = (f"<h1>{html.escape(p.title)}</h1><p class='byline'>{html.escape(p.author)}</p>"
                             f"{clean_post_html(p.intro_html)}")
            intro.add_item(style)
            book.add_item(intro)
            spine.append(intro)
            part_toc.append(intro)

        current = None
        for (ci, ref), post, soup in zip(p.refs, p.posts, part_soups):
            n += 1
            post = post or {}
            for img in soup.find_all("img", src=True):
                im = image_urls.get(urljoin(SITE, img["src"]))
                if im:
                    img["src"] = im.file_name
                    img.attrs.pop("srcset", None)
                    img.attrs.pop("sizes", None)
            p_title = post.get("title") or ref.get("title") or f"Post {n}"
            names = [name(post.get("user"))] + [name(u) for u in post.get("coauthors") or []]
            posted = (post.get("postedAt") or "")[:10]
            byline = ", ".join(names) + (f" · {posted}" if posted else "")
            url = f"{SITE}/s/{p.sid}/p/{ref['_id']}"
            ch = epub.EpubHtml(title=p_title, file_name=f"post-{n:03d}.xhtml", lang="en")
            ch.content = (
                f"<h1 class='post-title'>{html.escape(p_title)}</h1>"
                f"<p class='byline'>{html.escape(byline)}</p>{soup}"
                f"<p class='source'>Source: <a href='{url}'>{url}</a></p>"
            )
            ch.add_item(style)
            book.add_item(ch)
            spine.append(ch)

            section_title = p.chapters[ci].get("title")
            if section_title and len(p.chapters) > 1:
                if current is None or current[0].title != section_title:
                    current = (epub.Section(section_title), [])
                    part_toc.append(current)
                current[1].append(ch)
            else:
                current = None
                part_toc.append(ch)

    book.toc = toc
    book.spine = spine
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(f".{uuid.uuid4().hex[:8]}.tmp")
    epub.write_epub(str(tmp), book)
    tmp.replace(out)
    images = sum(1 for i in image_urls.values() if i)
    print(f"  wrote {out} ({n} posts, {images} images)")
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
    ap.add_argument("-c", "--combine", metavar="TITLE",
                    help="put all the given sequences into one book with this title, one section per sequence")
    ap.add_argument("-a", "--author", help="author shown on the book (default: whoever wrote most of its posts)")
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
    jobs = max(1, args.jobs)
    failed = 0

    if args.combine:
        out = args.output / f"{slugify(args.combine)}.epub"
        if out.exists() and not args.force:
            print(f"{out} exists, skipping (use --force to rebuild)")
            return 0
        parts = []
        for sid in ids:
            try:
                part = fetch_sequence(session, sid, jobs)
            except Exception as e:  # noqa: BLE001
                print(f"sequence {sid}: failed: {e}", file=sys.stderr)
                return 1
            if part is None:
                return 1
            parts.append(part)
        if not args.author:
            # One author for the whole book: whoever wrote the most posts in it.
            writers = Counter(name((p or {}).get("user")) for part in parts for p in part.posts)
            args.author = writers.most_common(1)[0][0] if writers else None
        write_book(session, parts, args.combine, out, jobs, args.author)
        return 0

    for sid in ids:
        try:
            part = fetch_sequence(session, sid, jobs)
            if part is None:
                failed += 1
                continue
            out = args.output / f"{slugify(part.title)}.epub"
            if out.exists() and not args.force:
                print(f"  {out} exists, skipping (use --force to rebuild)")
                continue
            write_book(session, [part], part.title, out, jobs, args.author)
        except Exception as e:  # noqa: BLE001 - keep going with the other books
            failed += 1
            print(f"sequence {sid}: failed: {e}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
