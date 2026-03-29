#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "jinja2",
#   "requests",
# ]
# ///
"""
goodlinks-visuals: generate visualization datasets from the goodlinks collection.

Fetches all links via the goodlinks local REST API and writes a JSON dataset
plus a stub HTML file (with plotly.js) into an output directory.

goodlinks API docs: https://goodlinks.app/api/
default API base URL: http://localhost:9428/api/v1
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

import jinja2
import requests

DEFAULT_BASE_URL = "http://localhost:9428/api/v1"
TOKEN_FILE = Path("~/.credentials/goodlinks-token.txt").expanduser()
DEFAULT_OUTPUT_DIR = Path("goodlinks-stats")
TEMPLATES_DIR = Path(__file__).parent / "templates"
SHORTCODES_TEMPLATES_DIR = TEMPLATES_DIR / "shortcodes"

SHORTCODE_NAMES = [
    "goodlinks-plotly",
    "goodlinks-heatmap",
    "goodlinks-sunburst",
    "goodlinks-table",
]


# ---------------------------------------------------------------------------
# Token resolution (same precedence as goodlinks-gardening.py)
# ---------------------------------------------------------------------------


def _resolve_token(cli_token: str | None = None) -> str | None:
    """
    resolve the API token using ascending precedence:
      1. ~/.credentials/goodlinks-token.txt  (lowest)
      2. GOODLINKS_API environment variable
      3. --token CLI flag                    (highest)
    returns None if no token source is available.
    """
    token: str | None = None

    if TOKEN_FILE.is_file():
        value = TOKEN_FILE.read_text().strip()
        if value:
            token = value

    env_token = os.environ.get("GOODLINKS_API", "").strip()
    if env_token:
        token = env_token

    if cli_token:
        token = cli_token

    return token


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------


class GoodLinksClient:
    """thin wrapper around the goodlinks local REST API."""

    def __init__(self, base_url: str = DEFAULT_BASE_URL, token: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.session.headers.update(headers)

    def get_all_links(self) -> list[dict]:
        """fetch every link in the collection, handling pagination automatically."""
        links = []
        offset = 0
        limit = 1000  # API maximum
        while True:
            resp = self.session.get(
                f"{self.base_url}/lists/all",
                params={"limit": limit, "offset": offset},
            )
            resp.raise_for_status()
            data = resp.json()
            batch = data.get("data", [])
            links.extend(batch)
            if not data.get("hasMore", False):
                break
            offset += len(batch)
        return links


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalise_domain(netloc: str) -> str:
    domain = netloc.lower()
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def _domain_of(url: str) -> str:
    try:
        return _normalise_domain(urlparse(url).netloc)
    except Exception:
        return ""


def _iso_date(ts: str | None) -> str | None:
    """return the date portion of an ISO-8601 timestamp, or None."""
    if not ts:
        return None
    return ts[:10]  # "YYYY-MM-DD"


def _year_month(ts: str | None) -> str | None:
    """return 'YYYY-MM' from an ISO-8601 timestamp, or None."""
    if not ts:
        return None
    return ts[:7]


# ---------------------------------------------------------------------------
# Dataset builders
# ---------------------------------------------------------------------------


def build_dataset(links: list[dict]) -> dict:
    """
    transform raw goodlinks link objects into the visualization dataset.

    returns a dict with keys:
      - articles   : list of article rows for the sortable table
      - heatmap    : {date -> count} for read dates (github-style heatmap)
      - tag_series : {tag -> {month -> count}} for the stacked area / sparklines
      - domain_series : {domain -> {month -> count}} for domain over time
    """
    articles = []
    read_date_counts: dict[str, int] = defaultdict(int)
    tag_month_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    domain_month_counts: dict[str, dict[str, int]] = defaultdict(
        lambda: defaultdict(int)
    )

    for link in links:
        url = link.get("url", "")
        domain = _domain_of(url)
        tags: list[str] = link.get("tags", []) or []
        added_at = link.get("addedAt") or link.get("savedAt")
        read_at = link.get("readAt")
        title = link.get("title") or url

        articles.append(
            {
                "id": link.get("id"),
                "title": title,
                "url": url,
                "domain": domain,
                "tags": tags,
                "addedDate": _iso_date(added_at),
                "readDate": _iso_date(read_at),
            }
        )

        # heatmap: count reads per calendar day
        read_day = _iso_date(read_at)
        if read_day:
            read_date_counts[read_day] += 1

        # tag / domain series: count per month using addedAt
        month = _year_month(added_at)
        if month:
            for tag in tags:
                tag_month_counts[tag][month] += 1
            if domain:
                domain_month_counts[domain][month] += 1

    # sort articles by read date descending (unread last)
    articles.sort(key=lambda a: a["readDate"] or "", reverse=True)

    return {
        "articles": articles,
        "heatmap": dict(read_date_counts),
        "tag_series": {t: dict(v) for t, v in tag_month_counts.items()},
        "domain_series": {d: dict(v) for d, v in domain_month_counts.items()},
    }


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------


def render_html() -> str:
    """render index.html from Jinja2 templates in the templates/ directory."""
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=False,
    )
    return env.get_template("index.html").render()


def export_hugo(
    hugo_dir: Path,
    page_bundle: str,
    dataset: dict,
    indent: int | None,
) -> None:
    """
    write goodlinks-data.json into the Hugo page bundle and copy shortcode
    templates into the Hugo project's layouts/shortcodes/ directory.

    args:
      hugo_dir    - root of the Hugo project (must exist)
      page_bundle - path relative to hugo_dir for the page bundle
                    (e.g. "content/posts/reading-stats")
      dataset     - the dataset dict from build_dataset()
      indent      - JSON indent level (None for compact, 2 for pretty)
    """
    bundle_dir = hugo_dir / page_bundle
    bundle_dir.mkdir(parents=True, exist_ok=True)

    json_path = bundle_dir / "goodlinks-data.json"
    json_path.write_text(json.dumps(dataset, indent=indent, ensure_ascii=False))
    print(f"  wrote {json_path}", file=sys.stderr)

    shortcodes_dir = hugo_dir / "layouts" / "shortcodes"
    shortcodes_dir.mkdir(parents=True, exist_ok=True)

    for name in SHORTCODE_NAMES:
        src = SHORTCODES_TEMPLATES_DIR / f"{name}.html"
        dst = shortcodes_dir / f"{name}.html"
        dst.write_text(src.read_text())
        print(f"  wrote {dst}", file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="generate goodlinks visualization dataset and HTML stub",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"goodlinks API base URL (default: {DEFAULT_BASE_URL})",
    )
    p.add_argument(
        "--token",
        default=None,
        help="API bearer token (overrides file and env var)",
    )
    p.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help=f"output directory for dataset and HTML (default: {DEFAULT_OUTPUT_DIR})",
    )
    p.add_argument(
        "--pretty",
        action="store_true",
        help="pretty-print the JSON output",
    )
    p.add_argument(
        "--hugo-dir",
        default=None,
        metavar="PATH",
        help="Hugo project root; enables shortcode export when set",
    )
    p.add_argument(
        "--page-bundle",
        default=None,
        metavar="PATH",
        help=(
            "path relative to --hugo-dir for the page bundle that embeds the charts "
            "(e.g. content/posts/reading-stats); required when --hugo-dir is set"
        ),
    )
    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    token = _resolve_token(args.token)
    client = GoodLinksClient(base_url=args.base_url, token=token)

    print("fetching links from goodlinks API…", file=sys.stderr)
    try:
        links = client.get_all_links()
    except requests.exceptions.ConnectionError as exc:
        sys.exit(f"error: could not connect to goodlinks API at {args.base_url}: {exc}")
    except requests.exceptions.HTTPError as exc:
        sys.exit(f"error: HTTP error from goodlinks API: {exc}")

    print(f"  fetched {len(links)} links", file=sys.stderr)

    dataset = build_dataset(links)
    print(
        f"  built dataset: {len(dataset['articles'])} articles, "
        f"{len(dataset['tag_series'])} tags, "
        f"{len(dataset['heatmap'])} read-days",
        file=sys.stderr,
    )

    out_dir = Path(args.output_dir)
    data_dir = out_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    # write dataset JSON
    json_path = data_dir / "goodlinks-data.json"
    indent = 2 if args.pretty else None
    json_path.write_text(json.dumps(dataset, indent=indent, ensure_ascii=False))
    print(f"  wrote {json_path}", file=sys.stderr)

    html_path = out_dir / "index.html"
    html_path.write_text(render_html())
    print(f"  wrote {html_path}", file=sys.stderr)

    if args.hugo_dir:
        if not args.page_bundle:
            sys.exit("error: --page-bundle is required when --hugo-dir is set")
        hugo_dir = Path(args.hugo_dir).expanduser()
        if not hugo_dir.is_dir():
            sys.exit(f"error: --hugo-dir {hugo_dir} does not exist")
        print(f"exporting Hugo shortcodes to {hugo_dir}…", file=sys.stderr)
        export_hugo(hugo_dir, args.page_bundle, dataset, indent)

    print("done.", file=sys.stderr)


if __name__ == "__main__":
    main()
