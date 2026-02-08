from __future__ import annotations

import re
from dataclasses import dataclass, field
from bs4 import BeautifulSoup, Tag
from bs4.element import NavigableString, PageElement

# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class ReaderResult:
    """Result of reader-mode extraction."""
    title: str | None = None
    markdown: str | None = None
    text: str | None = None
    links: list[dict] = field(default_factory=list)
    images: list[dict] = field(default_factory=list)
    is_article: bool = False


# ---------------------------------------------------------------------------
# Constants / regex patterns (based on arc90 / Mozilla Readability)
# ---------------------------------------------------------------------------

_UNLIKELY_CANDIDATES = re.compile(
    r"combx|comment|community|disqus|extra|foot|header|menu|related|remark|rss|"
    r"shoutbox|sidebar|sponsor|ad-break|agegate|pagination|pager|popup|tweet|"
    r"social|share|login|signup|subscribe|newsletter|cookie|consent|modal|"
    r"nav|toolbar|banner|masthead|widget",
    re.IGNORECASE,
)

_OK_MAYBE_CANDIDATE = re.compile(
    r"and|article|body|column|main|shadow|content|story|entry|post",
    re.IGNORECASE,
)

_POSITIVE_SCORE = re.compile(
    r"article|body|content|entry|hentry|h-entry|main|page|post|text|blog|story",
    re.IGNORECASE,
)

_NEGATIVE_SCORE = re.compile(
    r"hidden|banner|combx|comment|com-|contact|foot|footer|footnote|masthead|"
    r"media|meta|outbrain|promo|related|scroll|shoutbox|sidebar|sponsor|"
    r"shopping|tags|tool|widget|ad|newsletter|cookie|popup|modal",
    re.IGNORECASE,
)

_TAGS_TO_STRIP = {
    "script", "style", "noscript", "iframe", "form", "svg", "button",
    "input", "select", "textarea",
}

# Class/id patterns for elements that should be stripped entirely because
# they are promotional or CTA blocks embedded in articles.
_PROMOTIONAL_PATTERNS = re.compile(
    r"wp-block-button|cta|promo|newsletter|event-card|"
    r"ad-unit|ad-slot|signup|subscribe|callout|related-post",
    re.IGNORECASE,
)

# Patterns for locating the article's featured / hero image
_FEATURED_IMAGE_PATTERNS = re.compile(
    r"featured.image|wp-post-image|post-thumbnail|hero.*image|article-hero|"
    r"lead.image|cover.image|header.image",
    re.IGNORECASE,
)

_BLOCK_TAGS = {
    "div", "article", "section", "td", "main", "aside", "details",
    "fieldset", "figure", "li", "blockquote",
}

_MIN_ARTICLE_LENGTH = 200  # chars – below this we say "no article"
_MIN_SCORE_THRESHOLD = 10

# ---------------------------------------------------------------------------
# Preprocessing
# ---------------------------------------------------------------------------

def _get_class_id(tag: Tag) -> str:
    """Return the combined class + id string for regex matching."""
    cls_val = tag.get("class")
    if isinstance(cls_val, list):
        cls = " ".join(str(c) for c in cls_val)
    else:
        cls = str(cls_val) if cls_val else ""
    id_ = str(tag.get("id") or "")
    return f"{cls} {id_}"


def _is_hidden(tag: Tag) -> bool:
    """Check if an element is hidden via inline style or aria attributes."""
    if not tag.attrs:
        return False
    style = str(tag.attrs.get("style") or "").lower()
    if "display:none" in style.replace(" ", "") or "visibility:hidden" in style.replace(" ", ""):
        return True
    if tag.attrs.get("aria-hidden") == "true":
        return True
    if tag.attrs.get("hidden") is not None:
        return True
    return False


def _preprocess(soup: BeautifulSoup) -> None:
    """Strip non-content elements from the DOM in-place."""
    # Remove tags that never contain article content
    for tag_name in _TAGS_TO_STRIP:
        for el in soup.find_all(tag_name):
            el.decompose()

    # Remove hidden elements
    for el in list(soup.find_all(True)):
        if isinstance(el, Tag) and el.parent is not None and _is_hidden(el):
            el.decompose()

    # Remove promotional / hero / CTA blocks embedded in articles
    for el in list(soup.find_all(True)):
        if not isinstance(el, Tag) or el.parent is None:
            continue
        ci = _get_class_id(el)
        if _PROMOTIONAL_PATTERNS.search(ci):
            el.decompose()

    # Remove elements with data-event="button" (CTA buttons on many sites)
    for el in list(soup.find_all(attrs={"data-event": "button"})):
        if isinstance(el, Tag) and el.parent is not None:
            el.decompose()

    # Remove unlikely candidates (but keep OK-maybe ones)
    for el in list(soup.find_all(True)):
        if not isinstance(el, Tag) or el.parent is None:
            continue
        ci = _get_class_id(el)
        if _UNLIKELY_CANDIDATES.search(ci) and not _OK_MAYBE_CANDIDATE.search(ci):
            tag_name = el.name.lower()
            # Don't remove the body, html, or article tags themselves
            if tag_name not in ("body", "html", "article", "main"):
                el.decompose()


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _link_density(tag: Tag) -> float:
    """Ratio of link text to total text within a tag (0.0 – 1.0)."""
    text_len = len(tag.get_text(strip=False))
    if text_len == 0:
        return 1.0
    link_len = sum(len(a.get_text(strip=False)) for a in tag.find_all("a"))
    return link_len / text_len


def _class_id_score(tag: Tag) -> float:
    """Score based on class/id attribute contents."""
    score = 0.0
    ci = _get_class_id(tag)
    if _POSITIVE_SCORE.search(ci):
        score += 25
    if _NEGATIVE_SCORE.search(ci):
        score -= 25
    return score


def _tag_name_score(tag: Tag) -> float:
    """Bonus/penalty based on the tag name itself."""
    name = tag.name.lower()
    if name == "article":
        return 20
    if name in ("section", "main"):
        return 10
    if name == "div":
        return 5
    if name in ("pre", "td", "blockquote"):
        return 3
    if name in ("form", "aside"):
        return -5
    return 0


_ANCESTOR_LEVELS = 4  # How many ancestor levels to propagate scores to


def _score_candidates(soup: BeautifulSoup) -> dict[Tag, float]:
    """
    Walk all <p> and text-heavy elements, score them and their ancestors.
    Returns a dict mapping Tag -> score.
    """
    scores: dict[Tag, float] = {}

    # Collect paragraphs and other text containers
    text_tags = soup.find_all(["p", "pre", "td"])

    for el in text_tags:
        inner_text = el.get_text(strip=True)
        if len(inner_text) < 25:
            continue

        # Base score for this text node
        content_score = 1.0
        content_score += inner_text.count(",")  # commas = prose
        content_score += min(len(inner_text) / 100.0, 3.0)  # length bonus

        # Propagate score up through ancestors (with decay)
        ancestor = el.parent
        for level in range(_ANCESTOR_LEVELS):
            if not isinstance(ancestor, Tag):
                break
            # Initialise ancestor score if first time
            if ancestor not in scores:
                scores[ancestor] = (
                    _class_id_score(ancestor) + _tag_name_score(ancestor)
                )
            # Each level up gets half the score of the previous level
            # level 0 (parent) = full, level 1 = 1/2, level 2 = 1/3, etc.
            scores[ancestor] += content_score / (1 + level)
            ancestor = ancestor.parent

    # Also ensure <article> and <main> tags are always candidates
    for semantic in soup.find_all(["article", "main"]):
        if isinstance(semantic, Tag) and semantic not in scores:
            text = semantic.get_text(strip=True)
            if len(text) > _MIN_ARTICLE_LENGTH:
                scores[semantic] = (
                    _class_id_score(semantic)
                    + _tag_name_score(semantic)
                    + len(semantic.find_all("p")) * 2.0
                )

    # Apply link-density penalty
    for tag in list(scores):
        ld = _link_density(tag)
        scores[tag] *= (1.0 - ld)

    return scores


def _find_article_element(soup: BeautifulSoup) -> Tag | None:
    """Return the DOM element most likely to be the article content."""
    scores = _score_candidates(soup)
    if not scores:
        return None

    # Pick the top-scoring candidate
    top = max(scores, key=lambda t: scores[t])
    top_score = scores[top]

    if top_score < _MIN_SCORE_THRESHOLD:
        return None

    # Sibling expansion: include adjacent siblings that look like content
    parent = top.parent
    if isinstance(parent, Tag):
        threshold = max(10, top_score * 0.2)
        siblings = [
            sib for sib in parent.children
            if isinstance(sib, Tag) and (
                sib is top
                or scores.get(sib, 0) >= threshold
                or (sib.name == "p" and len(sib.get_text(strip=True)) > 80
                    and _link_density(sib) < 0.25)
            )
        ]
        if len(siblings) > 1:
            # Wrap them in a new div so we return one element
            wrapper = soup.new_tag("div")
            for sib in siblings:
                wrapper.append(sib.extract())
            return wrapper

    return top


# ---------------------------------------------------------------------------
# Title extraction
# ---------------------------------------------------------------------------

_TITLE_SEPARATORS = re.compile(r"\s*[\|\-–—:»/\\]\s*")


def _extract_title(soup: BeautifulSoup) -> str | None:
    """Extract the article title from meta tags or <title>."""
    # Try OpenGraph first
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and isinstance(og, Tag) and og.get("content"):
        return str(og["content"]).strip()

    # Twitter card
    tw = soup.find("meta", attrs={"name": "twitter:title"})
    if tw and isinstance(tw, Tag) and tw.get("content"):
        return str(tw["content"]).strip()

    # <h1> in the page – often the article headline
    h1 = soup.find("h1")
    if h1 and isinstance(h1, Tag):
        text = h1.get_text(strip=True)
        if text:
            return text

    # Fall back to <title> (with suffix cleaning)
    title_tag = soup.find("title")
    if title_tag:
        raw = title_tag.get_text(strip=True)
        parts = _TITLE_SEPARATORS.split(raw)
        # Pick the longest segment (usually the actual title)
        if parts:
            return max(parts, key=len).strip()

    return None


# ---------------------------------------------------------------------------
# Link extraction
# ---------------------------------------------------------------------------

def _extract_links(article: Tag) -> list[dict]:
    """Extract content links from the article element."""
    seen: set[str] = set()
    links: list[dict] = []
    for a in article.find_all("a", href=True):
        href = str(a["href"]).strip()
        text = a.get_text(strip=True)
        if not href or not text:
            continue
        # Skip anchors and javascript
        if href.startswith("#") or href.startswith("javascript:"):
            continue
        # Skip CTA / button links (not inline article text)
        if a.get("data-event") == "button" or a.get("data-ctatext"):
            continue
        ci = _get_class_id(a)
        if re.search(r"button|cta|btn", ci, re.IGNORECASE):
            continue
        # Skip if a parent is a button-like container
        parent = a.parent
        if isinstance(parent, Tag):
            pci = _get_class_id(parent)
            if re.search(r"button|cta|btn", pci, re.IGNORECASE):
                continue
        if href in seen:
            continue
        seen.add(href)
        links.append({"text": text, "href": href})
    return links


# ---------------------------------------------------------------------------
# Image + caption extraction
# ---------------------------------------------------------------------------

def _is_real_image_src(src: str) -> bool:
    """Return True if the src looks like a real article image (not a logo/icon/SVG)."""
    if not src:
        return False
    lower = src.lower()
    # SVGs are almost always logos/icons, not article photos
    if lower.endswith(".svg") or "/svg" in lower:
        return False
    # Data URIs for tiny images
    if lower.startswith("data:"):
        return False
    return True


def _extract_featured_image(soup: BeautifulSoup) -> dict | None:
    """
    Extract the article's featured / hero image before preprocessing.

    This image typically lives outside the article body (in a hero banner)
    but IS the article's primary image. Must be called on the original soup
    before _preprocess strips those sections.
    """
    # Strategy 1: og:image meta tag — most reliable signal across all sites
    og_img = soup.find("meta", attrs={"property": "og:image"})
    og_src = ""
    if og_img and isinstance(og_img, Tag):
        og_src = str(og_img.get("content") or "").strip()

    if og_src and _is_real_image_src(og_src):
        # Try to find a matching <img> in the DOM to get alt/caption
        caption = ""
        alt = ""
        for img in soup.find_all("img"):
            if not isinstance(img, Tag):
                continue
            img_src = str(img.get("src") or img.get("data-src") or "")
            # Check if the img src matches (exact or partial)
            if og_src in img_src or img_src in og_src:
                alt = str(img.get("alt") or "").strip()
                figure = img.find_parent("figure")
                if figure:
                    figcaption = figure.find("figcaption")
                    if figcaption:
                        caption = figcaption.get_text(strip=True)
                break
        return {"src": og_src, "alt": alt, "caption": caption}

    # Strategy 2: <figure> with featured-image / hero class
    for fig in soup.find_all("figure"):
        if not isinstance(fig, Tag):
            continue
        ci = _get_class_id(fig)
        if _FEATURED_IMAGE_PATTERNS.search(ci):
            img = fig.find("img")
            if img and isinstance(img, Tag):
                src = str(img.get("src") or img.get("data-src") or "").strip()
                if _is_real_image_src(src):
                    alt = str(img.get("alt") or "").strip()
                    caption = ""
                    figcaption = fig.find("figcaption")
                    if figcaption:
                        caption = figcaption.get_text(strip=True)
                    return {"src": src, "alt": alt, "caption": caption}

    # Strategy 3: <img> with featured/hero class patterns
    for img in soup.find_all("img"):
        if not isinstance(img, Tag):
            continue
        ci = _get_class_id(img)
        if _FEATURED_IMAGE_PATTERNS.search(ci):
            src = str(img.get("src") or img.get("data-src") or "").strip()
            if _is_real_image_src(src):
                alt = str(img.get("alt") or "").strip()
                caption = ""
                figure = img.find_parent("figure")
                if figure:
                    figcaption = figure.find("figcaption")
                    if figcaption:
                        caption = figcaption.get_text(strip=True)
                return {"src": src, "alt": alt, "caption": caption}

    # Strategy 4: <img> inside ancestor with hero patterns
    for img in soup.find_all("img"):
        if not isinstance(img, Tag):
            continue
        src = str(img.get("src") or img.get("data-src") or "").strip()
        if not _is_real_image_src(src):
            continue
        ancestor = img.parent
        for _ in range(4):
            if not isinstance(ancestor, Tag):
                break
            aci = _get_class_id(ancestor)
            if _FEATURED_IMAGE_PATTERNS.search(aci):
                alt = str(img.get("alt") or "").strip()
                caption = ""
                figure = img.find_parent("figure")
                if figure:
                    figcaption = figure.find("figcaption")
                    if figcaption:
                        caption = figcaption.get_text(strip=True)
                return {"src": src, "alt": alt, "caption": caption}
            ancestor = ancestor.parent

    return None


def _extract_images(article: Tag) -> list[dict]:
    """Extract images and their captions from the article element."""
    images: list[dict] = []
    seen_src: set[str] = set()

    for img in article.find_all("img"):
        src = str(img.get("src") or img.get("data-src") or "").strip()
        if not src or src in seen_src:
            continue
        # Skip tiny tracking pixels / icons
        width = str(img.get("width") or "")
        height = str(img.get("height") or "")
        if width and height:
            try:
                if int(width) < 50 or int(height) < 50:
                    continue
            except (ValueError, TypeError):
                pass

        seen_src.add(src)
        alt = str(img.get("alt") or "").strip()

        # Look for caption
        caption = ""
        # 1) Inside a <figure>, look for <figcaption>
        figure = img.find_parent("figure")
        if figure:
            figcaption = figure.find("figcaption")
            if figcaption:
                caption = figcaption.get_text(strip=True)

        # 2) Fallback: title attribute
        if not caption:
            caption = str(img.get("title") or "").strip()

        images.append({
            "src": src,
            "alt": alt,
            "caption": caption,
        })
    return images


# ---------------------------------------------------------------------------
# HTML-to-Markdown converter
# ---------------------------------------------------------------------------

def _html_to_markdown(element: Tag, *, plain: bool = False) -> str:
    """
    Convert an HTML element tree to Markdown text.

    Args:
        element: The root tag to convert.
        plain:   If True, omit links and images — produce text-only Markdown.
    """
    parts: list[str] = []
    _walk(element, parts, indent="", ol_counter=None, plain=plain)
    md = "\n".join(parts)
    # Clean up excessive blank lines
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


def _walk(
    node: PageElement,
    parts: list[str],
    indent: str = "",
    ol_counter: list[int] | None = None,
    *,
    plain: bool = False,
) -> None:
    """Recursive tree walker that emits Markdown."""
    if isinstance(node, NavigableString):
        text = str(node)
        # Collapse whitespace in inline text (but preserve single newlines)
        text = re.sub(r"[ \t]+", " ", text)
        if text.strip():
            parts.append(text)
        return

    if not isinstance(node, Tag):
        return

    name = node.name.lower()

    # --- Block elements ---

    if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
        level = int(name[1])
        prefix = "#" * level + " "
        text = node.get_text(strip=True)
        parts.append(f"\n\n{prefix}{text}\n\n")
        return

    if name == "p":
        inner = _inline_children(node, plain=plain)
        if inner.strip():
            parts.append(f"\n\n{inner.strip()}\n\n")
        return

    if name == "br":
        parts.append("  \n")
        return

    if name == "hr":
        parts.append("\n\n---\n\n")
        return

    if name == "blockquote":
        inner = _inline_children(node, plain=plain).strip()
        # Prefix each line with >
        quoted = "\n".join(f"> {line}" for line in inner.split("\n"))
        parts.append(f"\n\n{quoted}\n\n")
        return

    if name == "pre":
        code_tag = node.find("code")
        if code_tag:
            code_text = code_tag.get_text(strip=False)
        else:
            code_text = node.get_text(strip=False)
        lang = ""
        if code_tag and code_tag.get("class"):
            for cls in code_tag["class"]:
                if cls.startswith("language-"):
                    lang = cls[9:]
                    break
        parts.append(f"\n\n```{lang}\n{code_text.strip()}\n```\n\n")
        return

    if name == "ul":
        parts.append("\n")
        for li in node.find_all("li", recursive=False):
            inner = _inline_children(li, plain=plain).strip()
            parts.append(f"\n{indent}- {inner}")
        parts.append("\n")
        return

    if name == "ol":
        parts.append("\n")
        counter = 1
        for li in node.find_all("li", recursive=False):
            inner = _inline_children(li, plain=plain).strip()
            parts.append(f"\n{indent}{counter}. {inner}")
            counter += 1
        parts.append("\n")
        return

    if name == "figure":
        if plain:
            # In plain mode, skip images/figcaptions entirely
            return
        for child in node.children:
            _walk(child, parts, indent, ol_counter, plain=plain)
        return

    if name == "figcaption":
        if plain:
            return
        text = node.get_text(strip=True)
        if text:
            parts.append(f"\n*{text}*\n")
        return

    if name == "img":
        if plain:
            return
        src = node.get("src") or node.get("data-src") or ""
        alt = node.get("alt") or ""
        if src:
            parts.append(f"\n\n![{alt}]({src})\n\n")
        return

    if name == "table":
        _convert_table(node, parts)
        return

    # --- Inline / generic container: recurse ---
    for child in node.children:
        _walk(child, parts, indent, ol_counter, plain=plain)


def _inline_children(node: Tag, *, plain: bool = False) -> str:
    """Render inline children of a tag to Markdown."""
    result: list[str] = []
    for child in node.children:
        if isinstance(child, NavigableString):
            text = str(child)
            text = re.sub(r"[ \t]+", " ", text)
            result.append(text)
        elif isinstance(child, Tag):
            name = child.name.lower()

            if name in ("strong", "b"):
                inner = _inline_children(child, plain=plain).strip()
                if inner:
                    result.append(f"**{inner}**")
            elif name in ("em", "i"):
                inner = _inline_children(child, plain=plain).strip()
                if inner:
                    result.append(f"*{inner}*")
            elif name == "code":
                result.append(f"`{child.get_text()}`")
            elif name == "a":
                text = _inline_children(child, plain=plain).strip()
                if plain:
                    # Plain mode: emit just the link text
                    if text:
                        result.append(text)
                else:
                    href = child.get("href") or ""
                    if text and href:
                        result.append(f"[{text}]({href})")
                    elif text:
                        result.append(text)
            elif name == "img":
                if not plain:
                    src = child.get("src") or child.get("data-src") or ""
                    alt = child.get("alt") or ""
                    if src:
                        result.append(f"![{alt}]({src})")
            elif name == "br":
                result.append("  \n")
            elif name in ("ul", "ol"):
                # Nested lists inside paragraphs – render as block
                sub_parts: list[str] = []
                _walk(child, sub_parts, indent="  ", plain=plain)
                result.append("".join(sub_parts))
            elif name in ("span", "mark", "del", "s", "sub", "sup",
                          "abbr", "time", "small", "label", "cite"):
                result.append(_inline_children(child, plain=plain))
            else:
                # Unknown inline: just emit text
                result.append(_inline_children(child, plain=plain))
    return "".join(result)


def _convert_table(table: Tag, parts: list[str]) -> None:
    """Simple table -> Markdown table conversion."""
    rows: list[list[str]] = []
    for tr in table.find_all("tr"):
        cells = []
        for td in tr.find_all(["td", "th"]):
            cells.append(td.get_text(strip=True).replace("|", "\\|"))
        if cells:
            rows.append(cells)
    if not rows:
        return
    # Normalise column count
    max_cols = max(len(r) for r in rows)
    for r in rows:
        while len(r) < max_cols:
            r.append("")
    parts.append("\n\n")
    # Header row
    parts.append("| " + " | ".join(rows[0]) + " |")
    parts.append("\n| " + " | ".join(["---"] * max_cols) + " |")
    for row in rows[1:]:
        parts.append("\n| " + " | ".join(row) + " |")
    parts.append("\n\n")


# ---------------------------------------------------------------------------
# Deduplication helpers
# ---------------------------------------------------------------------------

def _url_core(url: str) -> str:
    """Extract the core identifier from an image URL for fuzzy matching.

    CDNs often serve the same image at different sizes/formats, e.g.:
      .../3183/live/8a040f50-0515.jpg
      .../3183/live/8a040f50-0515.jpg.webp
    We strip query params, common size prefixes, and the extension to get a
    stable key for comparison.
    """
    # Remove query string and fragment
    core = url.split("?")[0].split("#")[0]
    # Remove trailing format suffixes like .webp, .jpg, .png, .jpeg
    for ext in (".webp", ".avif", ".jpeg", ".jpg", ".png", ".gif"):
        if core.endswith(ext):
            core = core[: -len(ext)]
    # Take the last meaningful path segment(s) as the identifier
    parts = [p for p in core.split("/") if p]
    # Use last 3 segments to capture hash-based filenames
    return "/".join(parts[-3:]).lower() if parts else core.lower()


def _image_already_present(src: str, images: list[dict]) -> bool:
    """Check if an image with a similar URL is already in the list."""
    key = _url_core(src)
    return any(_url_core(img["src"]) == key for img in images)


# ---------------------------------------------------------------------------
# Main public API
# ---------------------------------------------------------------------------

def reader_mode(html_content: str) -> ReaderResult:
    """
    Extract article content from an HTML page, similar to Safari Reader Mode.

    Args:
        html_content: Raw HTML string of the page.

    Returns:
        ReaderResult with title, markdown, links, images, and is_article flag.
    """
    soup = BeautifulSoup(html_content, "html.parser")

    # Extract title and featured image before we mutate the DOM
    title = _extract_title(soup)
    featured_image = _extract_featured_image(soup)

    # Preprocess: strip junk elements
    _preprocess(soup)

    # Find the article element
    article = _find_article_element(soup)

    if article is None:
        return ReaderResult(title=title, is_article=False)

    # Check minimum content length
    article_text = article.get_text(strip=True)
    if len(article_text) < _MIN_ARTICLE_LENGTH:
        return ReaderResult(title=title, is_article=False)

    # Extract structured data from the article element
    links = _extract_links(article)
    images = _extract_images(article)

    # Convert article HTML to rich Markdown (with links + images inline)
    markdown = _html_to_markdown(article, plain=False)

    if not markdown or len(markdown.strip()) < _MIN_ARTICLE_LENGTH:
        return ReaderResult(title=title, is_article=False)

    # Convert article HTML to plain-text Markdown (no links, no images)
    text = _html_to_markdown(article, plain=True)

    # Prepend featured image to markdown (it lives outside the article body)
    if featured_image:
        fi_src = featured_image["src"]
        fi_alt = featured_image["alt"]
        fi_caption = featured_image["caption"]
        # Only prepend if it's not already in the article body images.
        # Compare by core filename, not full URL, because CDNs serve the same
        # image at different resolutions / formats (e.g. .jpg vs .jpg.webp).
        if not _image_already_present(fi_src, images):
            fi_md = f"![{fi_alt}]({fi_src})"
            if fi_caption:
                fi_md += f"\n\n*{fi_caption}*"
            markdown = fi_md + "\n\n" + markdown
            images.insert(0, featured_image)

    return ReaderResult(
        title=title,
        markdown=markdown,
        text=text,
        links=links,
        images=images,
        is_article=True,
    )
