# lw-epub

Build EPUB ebooks from [LessWrong](https://www.lesswrong.com) sequences, for reading offline on an
e-reader.

It asks the LessWrong API for the sequence, its author and each post, downloads the images in
parallel, and writes one EPUB per sequence with a cover, a nested table of contents, a byline and
a source link on every chapter.

This is a rewrite of [bartbussmann/lesswrong_ebook_library](https://github.com/bartbussmann/lesswrong_ebook_library)
(MIT). That script started a headless Chrome for every post and scraped the page, which was slow
and broke when LessWrong changed its layout (authors came out as "Unknown"). This version uses the
API instead: no browser, no Selenium, and seconds rather than minutes per book.

## Usage

With Nix:

```sh
nix run github:float3/lw-epub -- https://www.lesswrong.com/s/NBDFAKt3GbFwnwzQF
```

With pip:

```sh
pip install "lw-epub[images] @ git+https://github.com/float3/lw-epub"
lw-epub https://www.lesswrong.com/s/NBDFAKt3GbFwnwzQF -o books/
```

Options:

| Option | What it does |
|---|---|
| `SEQUENCE ...` | Sequence URLs (`https://www.lesswrong.com/s/<id>`) or bare ids |
| `--from-html FILE` | Also build every sequence linked from a saved HTML page, e.g. the [Library](https://www.lesswrong.com/library). Repeatable |
| `-o, --output DIR` | Where to write the books (default: current directory) |
| `-j, --jobs N` | Parallel downloads (default: 4). Keep it low; LessWrong rate-limits |
| `--delay SECONDS` | Pause before each request, to be gentler on the server |
| `-f, --force` | Rebuild books that already exist |
| `-c, --combine TITLE` | Put all the given sequences into one book with this title, one section per sequence |
| `-a, --author NAME` | Author shown on the book. Default: whoever wrote most of its posts, since sequences are often put together by a curator |

For example, the 50 [Sequences Highlights](https://www.lesswrong.com/highlights) as one book:

```sh
lw-epub NBDFAKt3GbFwnwzQF gFvira6tHpLXnqCLH 6xgy8XYEisLk3tCjH \
        pwK9RsvCdpPgKmrf6 paoDwasxFpSpzwA2f aaTrp2g86Qo3hinXQ \
        --combine "Sequences Highlights" -j 2
```

Rate limits (HTTP 429) and server errors are retried with backoff, honouring `Retry-After`.
Images that e-readers can't show (such as WebP) are converted to PNG when Pillow is installed.

## Development

```sh
nix develop        # Python with all dependencies
python lw_epub.py --help
```

## Content and copyright

The tool is MIT-licensed; the posts it downloads are not yours or ours. Each post belongs to its
author, under LessWrong's terms. Use the books for your own reading, and don't redistribute them
without the authors' permission. That's also why this repo, unlike the original, contains no
generated books.

## Not ported from the original

- The "Best of LessWrong" monthly books.
- PDF output. Convert with Calibre (`ebook-convert book.epub book.pdf`) if you need it.

## License

MIT. See [LICENSE](LICENSE).
