"""Reads the facts an APK site's app page lists (developer, version, size, update date, category, Android needed).

Each label sits on its own line of the page text ("App By:", "Version:", ...) and its value is on the same line or the
next one. Returns only the fields found; a missing label leaves its key out.
"""

from __future__ import annotations

from selectolax.lexbor import LexborHTMLParser as HTMLParser

_LABELS = {
    "App By": "developer",
    "Version": "version",
    "Updated On": "updated_on",
    "Size": "size",
    "Required Android": "required_android",
    "Category": "category",
}


def parse_app_page(html: str) -> dict[str, str]:
    tree = HTMLParser(html)
    fields: dict[str, str] = {}

    title = tree.css_first("h1")
    if title and (text := title.text(strip=True)):
        fields["title"] = text

    # The app icon: the page lazy-loads it, so data-src holds the real address. A data: placeholder is no icon.
    img = tree.css_first(".appcls img") or tree.css_first("figure img")
    if img:
        src = (img.attributes.get("data-src") or img.attributes.get("src") or "").strip()
        if src and not src.startswith("data:"):
            fields["icon"] = src

    lines = [line.strip() for line in tree.body.text(separator="\n", strip=True).split("\n") if line.strip()] if tree.body else []
    for i, line in enumerate(lines):
        label, _, rest = line.partition(":")
        key = _LABELS.get(label.strip())
        if not key or key in fields:
            continue
        # The value is the rest of the label's line, or the next line when the label stands alone.
        value = rest.strip()
        if not value and i + 1 < len(lines):
            value = lines[i + 1]
        if key == "version":
            value = value.split()[0] if value else ""  # "3.3.6 for" -> "3.3.6"
        if value:
            fields[key] = value
    return fields
