# Reader Mode

Safari-style article extraction from HTML pages. Identifies the main article content on a web page, extracts it as clean Markdown, and pulls out metadata like links, images, and captions.

**Single dependency:** `beautifulsoup4` (MIT license). The readability scoring algorithm and HTML-to-Markdown converter are implemented from scratch — no GPL or copyleft code.

## Features

- **Article extraction** — Readability-style scoring to find the main content, stripping navigation, sidebars, ads, and other clutter
- **Markdown output** — Converts the article HTML to clean Markdown (headings, paragraphs, bold/italic, links, images, lists, blockquotes, code blocks, tables)
- **Title extraction** — Pulls the article title from `og:title`, `twitter:title`, `<h1>`, or `<title>` (with site-name suffix cleaning)
- **Link extraction** — Returns all links appearing in the article content (deduplicated, no anchor or javascript links)
- **Image + caption extraction** — Returns images from the article with alt text and captions (from `<figcaption>`, `title` attribute)
- **No-article detection** — Returns `is_article=False` when the page doesn't contain an article (e.g. homepages, login pages, search results)

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (for dependency management and running)

## Setup

```bash
uv sync
```

## Usage

```python
from reader import reader_mode

html = "<html>...</html>"  # raw HTML string
result = reader_mode(html)

result.is_article  # bool — whether an article was detected
result.title       # str | None — article title
result.markdown    # str | None — rich Markdown with inline links + images in place
result.text        # str | None — plain-text Markdown (no links, no images)
result.links       # list[dict] — [{"text": "...", "href": "..."}, ...]
result.images      # list[dict] — [{"src": "...", "alt": "...", "caption": "..."}, ...]
```

### Example: fetch a page and extract the article

```python
import urllib.request
from reader import reader_mode

url = "https://techcrunch.com/2026/02/07/india-has-changed-its-startup-rules-for-deep-tech/"
html = urllib.request.urlopen(url).read().decode()

result = reader_mode(html)

if result.is_article:
    print(result.title)
    print(result.markdown)
else:
    print("No article found on this page.")
```


## Return type

`reader_mode()` returns a `ReaderResult` dataclass:

| Field        | Type          | Description                                                    |
|--------------|---------------|----------------------------------------------------------------|
| `is_article` | `bool`        | `False` if no article was detected                             |
| `title`      | `str \| None` | Extracted article title                                        |
| `markdown`   | `str \| None` | Rich Markdown — inline links and images in their original positions, captions in italic |
| `text`       | `str \| None` | Plain-text Markdown — same structure but no links or images    |
| `links`      | `list[dict]`  | Links in the article: `{"text", "href"}`                       |
| `images`     | `list[dict]`  | Images in the article: `{"src", "alt", "caption"}`             |
