#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "jinja2",
#   "pyyaml",
#   "requests",
# ]
# ///
"""
goodlinks-visuals: generate visualization datasets from the goodlinks collection.

Fetches all links via the goodlinks local REST API and writes split JSON datasets
into an output directory or Hugo page bundle.

Output files:
  goodlinks-heatmap.json.gz       -- {heatmap, tag_series}
  goodlinks-domain-stats.json.gz  -- {year: {domain: {tag: count}}} for sunburst
  goodlinks-articles-YYYY.json.gz -- articles whose readDate falls in that year
  goodlinks-articles-unread.json.gz -- articles with no readDate

goodlinks API docs: https://goodlinks.app/api/
default API base URL: http://localhost:9428/api/v1
"""

import argparse
import gzip
import json
import os
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import jinja2
import requests
import yaml

DEFAULT_BASE_URL = "http://localhost:9428/api/v1"
TOKEN_FILE = Path("~/.credentials/goodlinks-token.txt").expanduser()
DEFAULT_OUTPUT_DIR = Path("goodlinks-stats")
DEFAULT_CONFIG_FILE = Path(".goodlinks-visuals.conf")
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

    def get_all_highlights(self) -> list[dict]:
        """fetch every highlight in the collection, handling pagination automatically."""
        highlights = []
        offset = 0
        limit = 1000
        while True:
            resp = self.session.get(
                f"{self.base_url}/highlights",
                params={"limit": limit, "offset": offset},
            )
            resp.raise_for_status()
            data = resp.json()
            batch = data.get("data", [])
            highlights.extend(batch)
            if not data.get("hasMore", False):
                break
            offset += len(batch)
        return highlights


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


def _format_update_date(d: date | None = None) -> str:
    """return a date string like '18-may-2026'."""
    if d is None:
        d = date.today()
    return f"{d.day}-{d.strftime('%b').lower()}-{d.year}"


def _update_frontmatter_field(filepath: Path, field: str, value: str) -> None:
    """update or insert a YAML frontmatter field in a Hugo markdown file."""
    text = filepath.read_text()
    lines = text.splitlines(keepends=True)

    if not lines or lines[0].rstrip() != "---":
        print(f"  warning: no frontmatter found in {filepath}", file=sys.stderr)
        return

    close_idx = None
    for i in range(1, len(lines)):
        if lines[i].rstrip() == "---":
            close_idx = i
            break
    if close_idx is None:
        print(f"  warning: unclosed frontmatter in {filepath}", file=sys.stderr)
        return

    key_prefix = f"{field}:"
    new_lines = list(lines)
    updated = False
    for i in range(1, close_idx):
        if lines[i].lstrip().startswith(key_prefix):
            new_lines[i] = f'{field}: "{value}"\n'
            updated = True
            break

    if not updated:
        new_lines.insert(close_idx, f'{field}: "{value}"\n')

    new_text = "".join(new_lines)
    if new_text != text:
        filepath.write_text(new_text)
        print(f"  updated {field} in {filepath}", file=sys.stderr)


def _write_gz(path: Path, data: dict | list, indent: int | None) -> None:
    """write data as gzip-compressed JSON to path."""
    json_bytes = json.dumps(data, indent=indent, ensure_ascii=False).encode()
    with gzip.open(path, "wb") as f:
        f.write(json_bytes)
    print(
        f"  wrote {path} ({len(json_bytes):,} → {path.stat().st_size:,} bytes)",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# Dataset builders
# ---------------------------------------------------------------------------


def build_dataset(links: list[dict]) -> dict:
    """
    build heatmap and tag_series from raw goodlinks link objects.

    returns a dict with keys:
      - heatmap    : {date -> count} for read dates (github-style heatmap)
      - tag_series : {tag -> {month -> count}} for the stacked area / sparklines
    """
    read_date_counts: dict[str, int] = defaultdict(int)
    tag_month_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for link in links:
        tags: list[str] = link.get("tags", []) or []
        added_at = link.get("addedAt") or link.get("savedAt")
        read_at = link.get("readAt")

        read_day = _iso_date(read_at)
        if read_day:
            read_date_counts[read_day] += 1

        month = _year_month(added_at)
        if month:
            for tag in tags:
                tag_month_counts[tag][month] += 1

    return {
        "heatmap": dict(read_date_counts),
        "tag_series": {t: dict(v) for t, v in tag_month_counts.items()},
    }


def build_year_articles(
    links: list[dict],
    highlights_by_link: dict[str, list[dict]] | None = None,
) -> dict[str, list[dict]]:
    """
    group processed articles by readDate year.

    articles with no readDate go in the 'unread' bucket.
    returns {year_or_unread: [article, ...]} sorted by readDate desc within each bucket.

    when highlights_by_link is provided, each article gains optional keys:
      note       - article-level summary (str)
      highlights - list of {content, note?} passage objects (list[dict])
    """
    highlights_by_link = highlights_by_link or {}
    year_articles: dict[str, list] = defaultdict(list)

    for link in links:
        url = link.get("url", "")
        tags: list[str] = link.get("tags", []) or []
        added_at = link.get("addedAt") or link.get("savedAt")
        read_at = link.get("readAt")
        title = link.get("title") or url
        link_id = link.get("id", "")

        article: dict = {
            "title": title,
            "url": url,
            "tags": tags,
            "addedDate": _iso_date(added_at),
            "readDate": _iso_date(read_at),
        }

        raw_highlights = highlights_by_link.get(link_id, [])
        if raw_highlights:
            hl_list = []
            for h in raw_highlights:
                content = (h.get("content") or "").strip()
                if not content:
                    continue
                entry: dict = {"content": content}
                hl_note = (h.get("note") or "").strip()
                if hl_note:
                    entry["note"] = hl_note
                hl_list.append(entry)
            if hl_list:
                article["highlights"] = hl_list

        bucket = read_at[:4] if read_at else "unread"
        year_articles[bucket].append(article)

    for bucket, articles in year_articles.items():
        sort_key = "readDate" if bucket != "unread" else "addedDate"
        articles.sort(key=lambda a: a[sort_key] or "", reverse=True)

    return dict(year_articles)


def build_domain_stats(links: list[dict]) -> dict[str, dict[str, dict[str, int]]]:
    """
    build year→domain→tag count tree keyed by addedDate year.

    this is the pre-computed form of what the sunburst previously derived
    on-the-fly from the full articles list.
    """
    tree: dict[str, dict[str, dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(int))
    )

    for link in links:
        url = link.get("url", "")
        domain = _domain_of(url) or "(unknown)"
        tags: list[str] = link.get("tags", []) or []
        added_at = link.get("addedAt") or link.get("savedAt")
        year = added_at[:4] if added_at else None
        if not year:
            continue
        tag_list = tags if tags else ["(untagged)"]
        for tag in tag_list:
            tree[year][domain][tag] += 1

    return {
        year: {domain: dict(tags) for domain, tags in domains.items()}
        for year, domains in tree.items()
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


def export_bundle_split(
    bundle_dir: Path,
    dataset: dict,
    year_articles: dict[str, list],
    domain_stats: dict,
    indent: int | None,
) -> None:
    """
    write split JSON files into a Hugo page bundle directory.

    files written:
      goodlinks-heatmap.json.gz       -- heatmap + tag_series
      goodlinks-domain-stats.json.gz  -- year→domain→tag counts for sunburst
      goodlinks-articles-YYYY.json.gz -- per read-year article arrays
      goodlinks-articles-unread.json.gz -- articles with no readDate
    """
    bundle_dir.mkdir(parents=True, exist_ok=True)

    _write_gz(
        bundle_dir / "goodlinks-heatmap.json.gz",
        {"heatmap": dataset["heatmap"], "tag_series": dataset["tag_series"]},
        indent,
    )
    _write_gz(bundle_dir / "goodlinks-domain-stats.json.gz", domain_stats, indent)

    for bucket, articles in sorted(year_articles.items()):
        _write_gz(
            bundle_dir / f"goodlinks-articles-{bucket}.json.gz", articles, indent
        )

    total = sum(len(a) for a in year_articles.values())
    print(
        f"  split {total} articles across {len(year_articles)} year-files",
        file=sys.stderr,
    )


def export_shortcodes(shortcode_dir: Path) -> None:
    """
    copy shortcode templates into shortcode_dir.

    args:
      shortcode_dir - directory to write shortcode HTML files into
    """
    shortcode_dir.mkdir(parents=True, exist_ok=True)
    for name in SHORTCODE_NAMES:
        src = SHORTCODES_TEMPLATES_DIR / f"{name}.html"
        dst = shortcode_dir / f"{name}.html"
        dst.write_text(src.read_text())
        print(f"  wrote {dst}", file=sys.stderr)


def _resolve_bundle_dir(hugo_dir: Path | None, page_bundle: str) -> Path:
    """resolve the absolute bundle directory from hugo_dir (optional) + page_bundle."""
    if hugo_dir is not None:
        return hugo_dir / page_bundle
    bundle = Path(page_bundle)
    return bundle if bundle.is_absolute() else Path.cwd() / bundle


def load_config(config_path: Path) -> dict:
    """
    load a YAML config file.  expected structure:

      base_url: http://localhost:9428/api/v1   # optional
      exports:
        - bundle: content/posts/reading-stats
          hugo_dir: /path/to/hugo/site         # optional; overrides CLI --hugo-dir
        - bundle: content/links/stats

    returns a dict with keys 'base_url' (str|None) and 'exports' (list[dict]).
    """
    with config_path.open() as fh:
        data = yaml.safe_load(fh) or {}
    def _expand(value: str | None) -> str | None:
        return os.path.expandvars(value) if value else value

    exports = []
    for entry in data.get("exports", []):
        if isinstance(entry, str):
            exports.append({"bundle": _expand(entry), "hugo_dir": None})
        else:
            exports.append(
                {
                    "bundle": _expand(entry.get("bundle", "")),
                    "hugo_dir": _expand(entry.get("hugo_dir")),
                }
            )
    updated_files = []
    for entry in data.get("updated_files", []):
        if isinstance(entry, str):
            updated_files.append({"path": _expand(entry), "hugo_dir": None})
        else:
            updated_files.append(
                {
                    "path": _expand(entry.get("path", "")),
                    "hugo_dir": _expand(entry.get("hugo_dir")),
                }
            )

    return {
        "base_url": _expand(data.get("base_url")),
        "exports": exports,
        "updated_files": updated_files,
    }


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
        help="Hugo project root; used as the base directory for --page-bundle",
    )
    p.add_argument(
        "--page-bundle",
        default=None,
        metavar="PATH",
        help=(
            "write split gzip'd JSON datasets into this page bundle directory "
            "(relative to --hugo-dir when set, otherwise relative to CWD)"
        ),
    )
    p.add_argument(
        "--shortcode-dir",
        default=None,
        metavar="PATH",
        help="write Hugo shortcode templates into this directory",
    )
    p.add_argument(
        "--config",
        default=None,
        metavar="PATH",
        help=(
            f"YAML config file for data-only bundle exports "
            f"(auto-detected from ./{DEFAULT_CONFIG_FILE} when present)"
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

    print("fetching highlights from goodlinks API…", file=sys.stderr)
    try:
        raw_highlights = client.get_all_highlights()
    except requests.exceptions.ConnectionError as exc:
        sys.exit(f"error: could not connect to goodlinks API at {args.base_url}: {exc}")
    except requests.exceptions.HTTPError as exc:
        sys.exit(f"error: HTTP error from goodlinks API: {exc}")

    print(f"  fetched {len(raw_highlights)} highlights", file=sys.stderr)

    highlights_by_link: dict[str, list[dict]] = defaultdict(list)
    for h in sorted(raw_highlights, key=lambda x: x.get("createdAt") or ""):
        link_id = h.get("linkID", "")
        if link_id:
            highlights_by_link[link_id].append(h)

    dataset = build_dataset(links)
    year_articles = build_year_articles(links, dict(highlights_by_link))
    domain_stats = build_domain_stats(links)

    year_buckets = [y for y in year_articles if y != "unread"]
    print(
        f"  built dataset: {sum(len(a) for a in year_articles.values())} articles "
        f"across {len(year_buckets)} years, "
        f"{len(dataset['tag_series'])} tags, "
        f"{len(dataset['heatmap'])} read-days",
        file=sys.stderr,
    )

    indent = 2 if args.pretty else None

    # resolve hugo_dir for CLI-driven bundle export
    hugo_dir: Path | None = None
    if args.hugo_dir:
        hugo_dir = Path(args.hugo_dir).expanduser()
        if not hugo_dir.is_dir():
            sys.exit(f"error: --hugo-dir {hugo_dir} does not exist")

    # resolve config early so we know whether to write to the local output dir
    config_path: Path | None = None
    if args.config:
        config_path = Path(args.config).expanduser()
    elif DEFAULT_CONFIG_FILE.is_file():
        config_path = DEFAULT_CONFIG_FILE

    cfg: dict | None = None
    if config_path is not None:
        if not config_path.is_file():
            sys.exit(f"error: config file {config_path} does not exist")
        print(f"loading config from {config_path}…", file=sys.stderr)
        cfg = load_config(config_path)

    # only write to the local output dir when no config-driven exports are available
    has_config_exports = cfg is not None and bool(cfg.get("exports"))
    if not has_config_exports:
        out_dir = Path(args.output_dir)
        data_dir = out_dir / "data"
        data_dir.mkdir(parents=True, exist_ok=True)

        export_bundle_split(data_dir, dataset, year_articles, domain_stats, indent)

        if TEMPLATES_DIR.is_dir():
            html_path = out_dir / "index.html"
            html_path.write_text(render_html())
            print(f"  wrote {html_path}", file=sys.stderr)
        else:
            print(f"  skipping index.html (templates dir not found: {TEMPLATES_DIR})", file=sys.stderr)

    if args.page_bundle:
        bundle_dir = _resolve_bundle_dir(hugo_dir, args.page_bundle)
        print(f"exporting split JSON to {bundle_dir}…", file=sys.stderr)
        export_bundle_split(bundle_dir, dataset, year_articles, domain_stats, indent)

    if args.shortcode_dir:
        shortcode_dir = Path(args.shortcode_dir).expanduser()
        print(f"exporting shortcodes to {shortcode_dir}…", file=sys.stderr)
        export_shortcodes(shortcode_dir)

    # config-file driven exports
    if cfg is not None:
        assert config_path is not None
        config_base = config_path.parent.resolve()
        for entry in cfg["exports"]:
            bundle = entry["bundle"]
            entry_hugo_dir: Path | None = None
            if entry["hugo_dir"]:
                entry_hugo_dir = Path(entry["hugo_dir"]).expanduser()
                if not entry_hugo_dir.is_absolute():
                    entry_hugo_dir = config_base / entry_hugo_dir
            else:
                entry_hugo_dir = hugo_dir  # fall back to CLI --hugo-dir
            bundle_dir = _resolve_bundle_dir(entry_hugo_dir, bundle)
            print(f"  exporting split JSON to {bundle_dir}…", file=sys.stderr)
            export_bundle_split(bundle_dir, dataset, year_articles, domain_stats, indent)

        date_str = _format_update_date()
        for entry in cfg.get("updated_files", []):
            entry_hugo_dir = None
            if entry["hugo_dir"]:
                entry_hugo_dir = Path(entry["hugo_dir"]).expanduser()
                if not entry_hugo_dir.is_absolute():
                    entry_hugo_dir = config_base / entry_hugo_dir
            else:
                entry_hugo_dir = hugo_dir
            if entry_hugo_dir:
                filepath = entry_hugo_dir / entry["path"]
            else:
                filepath = Path(entry["path"])
                if not filepath.is_absolute():
                    filepath = Path.cwd() / entry["path"]
            if not filepath.is_file():
                print(f"  warning: {filepath} not found, skipping", file=sys.stderr)
                continue
            print(f"  updating frontmatter in {filepath}…", file=sys.stderr)
            _update_frontmatter_field(filepath, "data_updated", date_str)

    print("done.", file=sys.stderr)


if __name__ == "__main__":
    main()
