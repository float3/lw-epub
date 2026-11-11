"""Build EPUB ebooks from LessWrong sequences and posts, Substack posts, Slate
Star Codex and joecarlsmith.com.

A rewrite of bartbussmann/lesswrong_ebook_library (MIT). Instead of driving a
headless Chrome per post, it asks the LessWrong and Substack APIs for post
HTML, reads the other two sites' pages directly, then fetches images in
parallel.
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

__version__ = "0.3.0"

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


def resolve(arg: str) -> tuple[str, str]:
    """Work out what kind of source an argument is: (kind, id or URL)."""
    if re.fullmatch(r"[A-Za-z0-9]+", arg):
        return "lw-sequence", arg
    u = urlparse(arg if "://" in arg else "https://" + arg)
    host = u.netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    path = u.path
    if host == "lesswrong.com":
        if "/s/" in path:
            return "lw-sequence", sequence_id(path)
        m = re.match(r"/posts/([A-Za-z0-9]+)", path)
        if m:
            return "lw-post", m.group(1)
    elif host == "slatestarcodex.com" and re.match(r"/\d{4}/\d{2}/\d{2}/[^/]+", path):
        return "ssc", u.geturl()
    elif host == "joecarlsmith.com" and re.match(r"/\d{4}/\d{2}/\d{2}/[^/]+", path):
        return "carlsmith", u.geturl()
    elif host == "open.substack.com":
        m = re.match(r"/pub/([^/]+)/p/([^/]+)", path)
        if m:
            return "substack", f"https://{m.group(1)}.substack.com/p/{m.group(2)}"
    elif re.match(r"/p/[^/]+", path):
        # Substack publications on their own domain (astralcodexten.com &c)
        # use the same /p/<slug> paths and API as *.substack.com.
        return "substack", f"{u.scheme}://{u.netloc}{path}"
    raise ValueError(
        f"not a supported URL: {arg} (LessWrong sequence or post, Substack post, "
        "slatestarcodex.com or joecarlsmith.com post)"
    )


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


# Site furniture that is not part of the text: share buttons, subscribe boxes,
# Substack's image-expand icons, audio players.
JUNK = (".sharedaddy, .jp-relatedposts, .subscription-widget-wrap, .subscription-widget-wrap-editor, "
        ".button-wrapper, .image-link-expand, .post-comments")


def clean_post_html(raw: str, base: str = SITE) -> BeautifulSoup:
    soup = BeautifulSoup(raw or "", "html.parser")
    for tag in soup(["script", "style", "iframe", "form", "button", "audio", "video", "source"]):
        tag.decompose()
    for tag in soup.select(JUNK):
        tag.decompose()
    # Absolute links so they still work from inside the book; anchors whose
    # target is in the post (footnotes) stay relative so they jump within it.
    ids = {t["id"] for t in soup.find_all(id=True)}
    for a in soup.find_all("a", href=True):
        if not (a["href"].startswith("#") and a["href"][1:] in ids):
            a["href"] = urljoin(base, a["href"])
    return soup


@dataclass
class Post:
    title: str
    authors: list
    date: str  # YYYY-MM-DD or ""
    html: str
    url: str


@dataclass
class Part:
    """One sequence or one post, fetched: metadata and post bodies."""

    uid: str
    url: str
    site: str
    title: str
    author: str
    posts: list  # Post, in reading order
    sections: list  # section title per post, or None
    intro_html: str = ""
    cover_url: str | None = None


def lw_post(p: dict, url: str) -> Post:
    return Post(
        title=p.get("title") or "Untitled",
        authors=[name(p.get("user"))] + [name(u) for u in p.get("coauthors") or []],
        date=(p.get("postedAt") or "")[:10],
        html=(p.get("contents") or {}).get("html") or "",
        url=url,
    )


def fetch_sequence(session: Session, sid: str, jobs: int) -> Part | None:
    seq = session.graphql(SEQUENCE_QUERY, {"id": sid})["sequence"]["result"]
    if not seq:
        print(f"sequence {sid}: not found", file=sys.stderr)
        return None
    chapters = sorted(seq.get("chapters") or [], key=lambda c: c.get("number") or 0)
    refs = [(c, p) for c in chapters for p in c.get("posts") or []]
    title = seq["title"] or sid

    # Post bodies are fetched one per request; the sequence query stays small
    # so LessWrong does not time out on long sequences.
    def get_post(ref):
        return session.graphql(POST_QUERY, {"id": ref[1]["_id"]})["post"]["result"]

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        results = list(pool.map(get_post, refs))

    posts = [lw_post(r or {"title": ref.get("title")}, f"{SITE}/s/{sid}/p/{ref['_id']}")
             for (_, ref), r in zip(refs, results)]
    # Credit whoever wrote most of the posts: sequences are often assembled by
    # a curator (e.g. a LessWrong admin), whose name is on the sequence itself.
    writers = Counter(p.authors[0] for p in posts)
    author = writers.most_common(1)[0][0] if writers else name(seq.get("user"))
    print(f"{title} by {author}: {len(posts)} posts")
    cover_id = seq.get("gridImageId") or seq.get("bannerImageId")
    return Part(
        uid=f"lesswrong-{sid}",
        url=f"{SITE}/s/{sid}",
        site="LessWrong",
        title=title,
        author=author,
        posts=posts,
        sections=[c.get("title") if len(chapters) > 1 else None for c, _ in refs],
        intro_html=((seq.get("contents") or {}).get("html") or "").strip(),
        cover_url=CLOUDINARY + cover_id if cover_id else None,
    )


def single(post: Post, site: str, cover_url: str | None = None) -> Part:
    print(f"{post.title} by {', '.join(post.authors)}")
    return Part(uid=post.url, url=post.url, site=site, title=post.title, author=post.authors[0],
                posts=[post], sections=[None], cover_url=cover_url)


def fetch_lw_post(session: Session, pid: str) -> Part | None:
    p = session.graphql(POST_QUERY, {"id": pid})["post"]["result"]
    if not p:
        print(f"post {pid}: not found", file=sys.stderr)
        return None
    return single(lw_post(p, f"{SITE}/posts/{pid}/{p.get('slug') or ''}"), "LessWrong")


def fetch_substack(session: Session, url: str) -> Part:
    u = urlparse(url)
    slug = u.path.split("/p/", 1)[1].strip("/")
    d = session.request("GET", f"{u.scheme}://{u.netloc}/api/v1/posts/{slug}").json()
    if d.get("audience") not in (None, "everyone"):
        print(f"  {url}: paid post, only the free preview is included", file=sys.stderr)
    body = d.get("body_html") or ""
    if d.get("subtitle"):
        body = f"<p><i>{html.escape(d['subtitle'])}</i></p>{body}"
    post = Post(
        title=d.get("title") or slug,
        authors=[b["name"] for b in d.get("publishedBylines") or [] if b.get("name")] or ["Unknown"],
        date=(d.get("post_date") or "")[:10],
        html=body,
        url=d.get("canonical_url") or url,
    )
    return single(post, u.netloc, d.get("cover_image"))


def page(session: Session, url: str) -> BeautifulSoup:
    return BeautifulSoup(session.request("GET", url).text, "html.parser")


def url_date(url: str) -> str:
    m = re.search(r"/(\d{4})/(\d{2})/(\d{2})/", url)
    return "-".join(m.groups()) if m else ""


def og_image(soup: BeautifulSoup) -> str | None:
    m = soup.find("meta", property="og:image")
    return m.get("content") if m else None


def fetch_ssc(session: Session, url: str) -> Part:
    soup = page(session, url)
    body = soup.select_one(".pjgm-postcontent")
    if body is None:
        raise RuntimeError("no post body found; is this a post page?")
    author = soup.select_one(".pjgm-postmeta a[rel=author], .pjgm-postmeta .author a")
    post = Post(
        title=soup.select_one(".pjgm-posttitle").get_text(strip=True),
        authors=[author.get_text(strip=True) if author else "Scott Alexander"],
        date=url_date(url),
        html=body.decode_contents(),
        url=url,
    )
    return single(post, "Slate Star Codex")


def fetch_carlsmith(session: Session, url: str) -> Part:
    soup = page(session, url)
    body = soup.select_one(".single-essay__main-content")
    if body is None:
        raise RuntimeError("no essay body found; is this an essay page?")
    post = Post(
        title=soup.select_one(".single-essay__header-title").get_text(strip=True),
        authors=["Joe Carlsmith"],
        date=url_date(url),
        html=body.decode_contents(),
        url=url,
    )
    return single(post, "joecarlsmith.com", og_image(soup))


def fetch(session: Session, kind: str, ref: str, jobs: int) -> Part | None:
    if kind == "lw-sequence":
        return fetch_sequence(session, ref, jobs)
    if kind == "lw-post":
        return fetch_lw_post(session, ref)
    return {"substack": fetch_substack, "ssc": fetch_ssc, "carlsmith": fetch_carlsmith}[kind](session, ref)


def write_book(session: Session, parts: list[Part], title: str, out: Path, jobs: int,
               author: str | None = None) -> Path:
    """Write one EPUB. With one part it is a normal book; with several, each
    sequence or post becomes an entry of the table of contents."""
    combined = len(parts) > 1
    authors = [author] if author else list(dict.fromkeys(p.author for p in parts))
    book = epub.EpubBook()
    book.set_identifier("-".join(p.uid for p in parts))
    book.set_title(title)
    for a in authors:
        book.add_author(a)
    book.set_language("en")
    for p in parts:
        book.add_metadata("DC", "source", p.url)
    for site in dict.fromkeys(p.site for p in parts):
        book.add_metadata("DC", "publisher", site)

    style = epub.EpubItem(uid="style", file_name="style/book.css", media_type="text/css", content=CSS)
    book.add_item(style)

    cover_url = next((p.cover_url for p in parts if p.cover_url), None)
    if cover_url:
        cover = fetch_image(session, cover_url)
        if cover:
            book.set_cover(f"cover.{READER_IMAGE_TYPES[cover.media_type]}", cover.content)

    # Parse every post, then fetch all distinct images in parallel.
    image_urls: dict = {}
    soups = []
    for p in parts:
        part_soups = []
        for post in p.posts:
            soup = clean_post_html(post.html, post.url)
            for img in soup.find_all("img", src=True):
                src = urljoin(post.url, img["src"])
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
        if combined and len(p.posts) > 1:
            part_toc: list = []
            toc.append((epub.Section(p.title), part_toc))
        else:
            part_toc = toc
        if p.intro_html:
            intro = epub.EpubHtml(title=p.title if combined else "About this sequence",
                                  file_name=f"intro-{pi:02d}.xhtml", lang="en")
            intro.content = (f"<h1>{html.escape(p.title)}</h1><p class='byline'>{html.escape(p.author)}</p>"
                             f"{clean_post_html(p.intro_html, p.url)}")
            intro.add_item(style)
            book.add_item(intro)
            spine.append(intro)
            part_toc.append(intro)

        current = None
        for post, section_title, soup in zip(p.posts, p.sections, part_soups):
            n += 1
            for img in soup.find_all("img", src=True):
                im = image_urls.get(urljoin(post.url, img["src"]))
                if im:
                    img["src"] = im.file_name
                    for attr in ("srcset", "sizes", "data-src", "data-attrs"):
                        img.attrs.pop(attr, None)
            byline = ", ".join(post.authors) + (f" · {post.date}" if post.date else "")
            ch = epub.EpubHtml(title=post.title, file_name=f"post-{n:03d}.xhtml", lang="en")
            ch.content = (
                f"<h1 class='post-title'>{html.escape(post.title)}</h1>"
                f"<p class='byline'>{html.escape(byline)}</p>{soup}"
                f"<p class='source'>Source: <a href='{html.escape(post.url)}'>{html.escape(post.url)}</a></p>"
            )
            ch.add_item(style)
            book.add_item(ch)
            spine.append(ch)

            if section_title:
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
        description="Build EPUB ebooks from LessWrong sequences and posts, Substack posts, "
                    "Slate Star Codex and joecarlsmith.com.",
    )
    ap.add_argument("sources", nargs="*", metavar="URL",
                    help="LessWrong sequence (or bare sequence id) or post, Substack post (including "
                         "custom domains like astralcodexten.com), slatestarcodex.com or joecarlsmith.com post")
    ap.add_argument("--from-html", type=Path, action="append", default=[], metavar="FILE",
                    help="also build every sequence linked from a saved HTML page (repeatable)")
    ap.add_argument("-o", "--output", type=Path, default=Path("."), help="output directory (default: .)")
    ap.add_argument("-j", "--jobs", type=int, default=4, help="parallel downloads (default: 4)")
    ap.add_argument("--delay", type=float, default=0.0, help="seconds to wait before each request (default: 0)")
    ap.add_argument("-f", "--force", action="store_true", help="rebuild books that already exist")
    ap.add_argument("-c", "--combine", metavar="TITLE",
                    help="put everything given into one book with this title, one section per sequence")
    ap.add_argument("-a", "--author", help="author shown on the book (default: whoever wrote most of its posts)")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = ap.parse_args(argv)

    sources: list[tuple[str, str]] = []
    try:
        for s in args.sources:
            sources.append(resolve(s))
    except ValueError as e:
        ap.error(str(e))
    for f in args.from_html:
        sources += [("lw-sequence", sid) for sid in sequence_ids_from_html(f)]
    sources = list(dict.fromkeys(sources))
    if not sources:
        ap.error("give at least one URL or sequence id, or --from-html")

    session = Session(delay=args.delay)
    jobs = max(1, args.jobs)
    failed = 0

    if args.combine:
        out = args.output / f"{slugify(args.combine)}.epub"
        if out.exists() and not args.force:
            print(f"{out} exists, skipping (use --force to rebuild)")
            return 0
        parts = []
        for kind, ref in sources:
            try:
                part = fetch(session, kind, ref, jobs)
            except Exception as e:  # noqa: BLE001
                print(f"{ref}: failed: {e}", file=sys.stderr)
                return 1
            if part is None:
                return 1
            parts.append(part)
        if not args.author:
            # One author for the whole book: whoever wrote the most posts in it.
            writers = Counter(post.authors[0] for part in parts for post in part.posts)
            args.author = writers.most_common(1)[0][0] if writers else None
        write_book(session, parts, args.combine, out, jobs, args.author)
        return 0

    for kind, ref in sources:
        try:
            part = fetch(session, kind, ref, jobs)
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
            print(f"{ref}: failed: {e}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
