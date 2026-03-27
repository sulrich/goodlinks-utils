#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "requests",
#   "anthropic",
# ]
# ///
"""
goodlinks-gardening: CLI tool for managing and curating your goodlinks collection.

goodlinks API docs: https://goodlinks.app/api/
default API base URL: http://localhost:9428/api/v1
"""

import argparse
import concurrent.futures
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse

import requests
from anthropic import Anthropic

DEFAULT_BASE_URL = "http://localhost:9428/api/v1"
TOKEN_FILE = Path("~/.credentials/goodlinks-token.txt").expanduser()


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

    def update_link(
        self,
        link_id: str,
        added_tags: list[str] | None = None,
        removed_tags: list[str] | None = None,
    ) -> dict:
        """patch a link's tags using the addedTags / removedTags fields."""
        payload: dict = {}
        if added_tags:
            payload["addedTags"] = added_tags
        if removed_tags:
            payload["removedTags"] = removed_tags
        resp = self.session.patch(f"{self.base_url}/links/{link_id}", json=payload)
        resp.raise_for_status()
        return resp.json()

    def get_all_tags(self) -> list[str]:
        """fetch all tag strings in the collection."""
        resp = self.session.get(f"{self.base_url}/tags")
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return data
        return data.get("data", [])

    def get_link_content(self, link_id: str, format: str = "plaintext") -> str | None:
        """fetch the content of a link. format can be 'plaintext', 'html', or 'markdown'."""
        try:
            resp = self.session.get(
                f"{self.base_url}/links/{link_id}/content", params={"format": format}
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("data")
        except (requests.exceptions.HTTPError, requests.exceptions.JSONDecodeError):
            return None

    def get_untagged_links(self) -> list[dict]:
        """fetch all untagged links in the collection."""
        links = []
        offset = 0
        limit = 1000
        while True:
            resp = self.session.get(
                f"{self.base_url}/lists/untagged",
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


def _normalise_domain(netloc: str) -> str:
    """
    lowercase and strip a leading 'www.' so nytimes.com and www.nytimes.com
    group together.
    """
    domain = netloc.lower()
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def _domain_of(url: str) -> str:
    """return the normalised domain for a URL, or '' if unparseable."""
    try:
        return _normalise_domain(urlparse(url).netloc)
    except Exception:
        return ""


def _fetch_url_content(url: str, timeout: int = 30) -> str | None:
    """fetch a URL and return the HTML content, or None on error."""
    try:
        resp = requests.get(
            url,
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
        )
        resp.raise_for_status()
        return resp.text if resp.text.strip() else None
    except Exception:
        return None


def _extract_text_sample(content: str, max_length: int = 4000) -> str:
    """extract and sample text from HTML/plaintext content."""
    if not content:
        return ""
    # simple heuristic: take first N characters, breaking at sentence boundaries
    text = content[:max_length].strip()
    # try to break at a sentence end
    for i in range(len(text) - 1, max(0, len(text) - 200), -1):
        if text[i] in ".!?\n":
            text = text[: i + 1].strip()
            break
    return text


def _suggest_tag_for_content(
    client: Anthropic, content: str, available_tags: list[str]
) -> str | None:
    """use claude to analyze content and suggest the best tag from available tags."""
    if not content.strip() or not available_tags:
        return None

    tags_str = ", ".join(sorted(available_tags))
    prompt = f"""analyze this article content and suggest the single best tag from the following list:
{tags_str}

only respond with the tag name itself, nothing else. if none fit well, respond with just the word "general" (even if not in the list).

article content:
{content}"""

    try:
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=20,
            messages=[{"role": "user", "content": prompt}],
        )
        suggested = message.content[0].text.strip().lower()
        # verify the suggestion is from available tags (case-insensitive match)
        for tag in available_tags:
            if tag.lower() == suggested:
                return tag
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# gardening commands
# each command function signature is: (client: GoodLinksClient, args: Namespace) -> None
# ---------------------------------------------------------------------------


def cmd_tags(client: GoodLinksClient, args: argparse.Namespace) -> None:
    """
    list every tag in the collection with a per-tag article count.

    fetches all links, aggregates tags locally, then displays them sorted
    by count (descending) then alphabetically.
    """
    links = client.get_all_links()
    tag_counts: Counter = Counter()
    for link in links:
        for tag in link.get("tags", []):
            tag_counts[tag] += 1

    if not tag_counts:
        print("No tagged articles found.")
        return

    sorted_tags = sorted(tag_counts.items(), key=lambda x: (-x[1], x[0]))

    if args.json:
        print(json.dumps([{"tag": t, "count": c} for t, c in sorted_tags], indent=2))
        return

    max_len = max(len(t) for t, _ in sorted_tags)
    print(f"{'Tag':<{max_len}}  Count")
    print("-" * (max_len + 8))
    for tag, count in sorted_tags:
        print(f"{tag:<{max_len}}  {count}")
    total_assignments = sum(tag_counts.values())
    print(
        f"\nTotal: {len(tag_counts)} unique tag(s), {total_assignments} tag assignment(s) across {len(links)} article(s)"
    )


def cmd_urls(client: GoodLinksClient, args: argparse.Namespace) -> None:
    """
    list article URLs and display domain frequency statistics.

    without --urls, prints a table of domains ordered by article count.
    with --urls, prints one URL per line (suitable for piping).
    """
    links = client.get_all_links()
    domain_counts: Counter = Counter()

    for link in links:
        domain = _domain_of(link.get("url", ""))
        if domain:
            domain_counts[domain] += 1

    if args.urls:
        for link in links:
            url = link.get("url", "")
            if url:
                print(url)
        return

    min_count = args.min_count
    filtered = [(d, c) for d, c in domain_counts.items() if c >= min_count]
    sorted_domains = sorted(filtered, key=lambda x: (-x[1], x[0]))

    if args.json:
        print(
            json.dumps([{"domain": d, "count": c} for d, c in sorted_domains], indent=2)
        )
        return

    if not sorted_domains:
        print(f"No domains with {min_count}+ articles.")
        return

    max_len = max(len(d) for d, _ in sorted_domains)
    print(f"{'Domain':<{max_len}}  Count")
    print("-" * (max_len + 8))
    for domain, count in sorted_domains:
        print(f"{domain:<{max_len}}  {count}")
    print(
        f"\nShowing {len(sorted_domains)} domain(s) with {min_count}+ articles | "
        f"{len(domain_counts)} total domains | {len(links)} total articles"
    )


def cmd_tag_domain(client: GoodLinksClient, args: argparse.Namespace) -> None:
    """
    add a tag to every article from a given domain that doesn't already have it.

    subdomains of the specified domain are included automatically
    (e.g. --domain nytimes.com also matches www.nytimes.com).
    use --dry-run to preview changes without modifying anything.
    """
    target_domain = _normalise_domain(args.domain)
    tag = args.tag
    dry_run = args.dry_run

    links = client.get_all_links()

    needs_tag = []
    for link in links:
        link_domain = _domain_of(link.get("url", ""))
        if not link_domain:
            continue
        # Match exact domain or any subdomain of it
        if link_domain == target_domain or link_domain.endswith(f".{target_domain}"):
            current_tags = link.get("tags", [])
            if tag not in current_tags:
                needs_tag.append(link)

    if not needs_tag:
        print(
            f"no articles from '{target_domain}' are missing the tag '{tag}'. nothing to do."
        )
        return

    print(
        f"found {len(needs_tag)} article(s) from '{target_domain}' without tag '{tag}':\n"
    )
    for link in needs_tag:
        title = (link.get("title") or "Untitled")[:70]
        print(f"  {title}")
        print(f"    {link.get('url', '')}")

    if dry_run:
        print(
            f"\n[dry-run] would add tag '{tag}' to {len(needs_tag)} article(s). no changes made."
        )
        return

    print(f"\nadding tag '{tag}' to {len(needs_tag)} article(s)...")
    updated = 0
    for link in needs_tag:
        try:
            client.update_link(link["id"], added_tags=[tag])
            updated += 1
        except requests.exceptions.HTTPError as e:
            print(f"  error updating '{link.get('url', '')}': {e}", file=sys.stderr)

    print(f"done. tagged {updated}/{len(needs_tag)} article(s).")


def cmd_dedupe(client: GoodLinksClient, args: argparse.Namespace) -> None:
    """
    find articles that share the same URL.

    groups the collection by normalized URL and reports any URL that appears
    more than once, showing the title and saved date for each copy so you can
    decide which to keep. pass --delete to automatically remove all but the
    oldest saved copy of each duplicate.
    """
    links = client.get_all_links()

    seen: dict[str, list[dict]] = defaultdict(list)
    for link in links:
        url = link.get("url", "").strip()
        if url:
            seen[url].append(link)

    dupes = {url: copies for url, copies in seen.items() if len(copies) > 1}

    if not dupes:
        print("No duplicate URLs found.")
        return

    total_extra = sum(len(copies) - 1 for copies in dupes.values())
    print(
        f"found {len(dupes)} url(s) with duplicates ({total_extra} extra article(s)):\n"
    )

    # sort oldest-first within each group so the keeper is always copies[0]
    for url, copies in sorted(dupes.items()):
        copies.sort(key=lambda l: l.get("addedAt") or "")
        print(f"  {url}")
        for i, link in enumerate(copies):
            added = (link.get("addedAt") or "unknown date")[:10]
            title = (link.get("title") or "Untitled")[:60]
            marker = "(keep)" if i == 0 else "(dupe)"
            print(f"    {marker}  [{added}]  {title}")
        print()

    if args.json:
        output = []
        for url, copies in sorted(dupes.items()):
            copies_sorted = sorted(copies, key=lambda l: l.get("addedAt") or "")
            output.append(
                {
                    "url": url,
                    "copies": [
                        {
                            "id": l["id"],
                            "title": l.get("title"),
                            "addedAt": l.get("addedAt"),
                        }
                        for l in copies_sorted
                    ],
                }
            )
        print(json.dumps(output, indent=2))
        return

    if not args.delete:
        print(f"run with --delete to remove the {total_extra} extra copy/copies.")
        return

    print(f"deleting {total_extra} duplicate(s) (keeping oldest saved copy of each)...")
    deleted = 0
    for copies in dupes.values():
        copies.sort(key=lambda l: l.get("addedAt") or "")
        for link in copies[1:]:  # skip the oldest; delete the rest
            try:
                resp = client.session.delete(
                    f"{client.base_url}/links",
                    params={"id": link["id"]},
                )
                resp.raise_for_status()
                deleted += 1
            except requests.exceptions.HTTPError as e:
                print(f"  error deleting '{link.get('url', '')}': {e}", file=sys.stderr)

    print(f"done. deleted {deleted}/{total_extra} duplicate article(s).")


def _probe_url(url: str, timeout: int) -> tuple[str, int | None, str]:
    """
    check whether a URL is reachable. returns (url, status_code, disposition)
    where disposition is one of: 'ok', 'http', 'timeout', 'error'.

    tries HEAD first; falls back to a streaming GET if the server rejects HEAD.
    """
    try:
        resp = requests.head(url, timeout=timeout, allow_redirects=True)
        if resp.status_code == 405:
            resp = requests.get(url, timeout=timeout, allow_redirects=True, stream=True)
            resp.close()
        if resp.status_code >= 400:
            return url, resp.status_code, "http"
        return url, resp.status_code, "ok"
    except requests.exceptions.Timeout:
        return url, None, "timeout"
    except Exception:
        return url, None, "error"


def cmd_dead_links(client: GoodLinksClient, args: argparse.Namespace) -> None:
    """
    identify links that are likely no longer viable.

    an article is flagged as dead if:
      - its word count is 0 or missing (goodlinks could not fetch the content,
        indicating the article was unavailable offline), or
      - its URL returns an HTTP 4xx / 5xx response, times out, or fails to
        connect.

    when a live HTTP error code is returned, that code is added as a tag on
    the article (e.g. 'http-404'). connection failures get 'http-error' and
    timeouts get 'http-timeout'. offline-only failures get 'offline-unavailable'.

    requires one of --tag or --all to define the scope. combine with --unread
    to restrict to unread articles, or --untagged to restrict to articles with
    no tags at all.
    """
    if not args.tag and not args.all:
        print("error: specify a scope with --tag TAG or --all", file=sys.stderr)
        sys.exit(1)

    links = client.get_all_links()

    # -- scope filter ---------------------------------------------------------
    if args.tag:
        links = [l for l in links if args.tag in l.get("tags", [])]
    if args.unread:
        links = [l for l in links if l.get("readAt") is None]
    if args.untagged:
        links = [l for l in links if not l.get("tags")]

    if not links:
        print("no articles match the given filters.")
        return

    print(f"checking {len(links)} article(s) for dead links ...\n")

    # -- offline availability check (wordCount == 0 or absent) ---------------
    offline_ids: set[str] = {l["id"] for l in links if not l.get("wordCount")}

    # -- live HTTP probe (parallel) -------------------------------------------
    http_results: dict[str, tuple[int | None, str]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(_probe_url, l["url"], args.timeout): l["id"]
            for l in links
            if l.get("url")
        }
        for future in concurrent.futures.as_completed(futures):
            link_id = futures[future]
            _url, status, disposition = future.result()
            http_results[link_id] = (status, disposition)

    # -- collate results ------------------------------------------------------
    dead: list[dict] = []
    for link in links:
        lid = link["id"]
        is_offline = lid in offline_ids
        status, disposition = http_results.get(lid, (None, "ok"))
        is_http_dead = disposition in ("http", "timeout", "error")

        if not is_offline and not is_http_dead:
            continue

        new_tags: list[str] = []
        reasons: list[str] = []

        if is_offline:
            reasons.append("unavailable offline (word count: 0)")
            if "offline-unavailable" not in link.get("tags", []):
                new_tags.append("offline-unavailable")

        if disposition == "http" and status is not None:
            reasons.append(f"HTTP {status}")
            tag = f"http-{status}"
            if tag not in link.get("tags", []):
                new_tags.append(tag)
        elif disposition == "timeout":
            reasons.append("connection timed out")
            if "http-timeout" not in link.get("tags", []):
                new_tags.append("http-timeout")
        elif disposition == "error":
            reasons.append("connection error")
            if "http-error" not in link.get("tags", []):
                new_tags.append("http-error")

        dead.append(
            {
                "link": link,
                "reasons": reasons,
                "new_tags": new_tags,
            }
        )

    if not dead:
        print("No dead links found.")
        return

    total_new_tags = sum(len(d["new_tags"]) for d in dead)
    print(f"found {len(dead)} dead link(s):\n")

    for entry in dead:
        link = entry["link"]
        title = (link.get("title") or "Untitled")[:70]
        print(f"  {title}")
        print(f"    {link.get('url', '')}")
        print(f"    reasons : {', '.join(entry['reasons'])}")
        if entry["new_tags"]:
            print(f"    new tags: {', '.join(entry['new_tags'])}")
        print()

    if args.json:
        output = [
            {
                "id": e["link"]["id"],
                "url": e["link"].get("url"),
                "title": e["link"].get("title"),
                "reasons": e["reasons"],
                "new_tags": e["new_tags"],
            }
            for e in dead
        ]
        print(json.dumps(output, indent=2))
        return

    if args.dry_run:
        print(
            f"[dry-run] would add {total_new_tags} tag(s) across {len(dead)} article(s). no changes made."
        )
        return

    if total_new_tags == 0:
        print("all dead articles already have the appropriate error tags.")
        return

    print(f"tagging {len(dead)} dead article(s)...")
    tagged = 0
    for entry in dead:
        if not entry["new_tags"]:
            continue
        try:
            client.update_link(entry["link"]["id"], added_tags=entry["new_tags"])
            tagged += 1
        except requests.exceptions.HTTPError as e:
            print(
                f"  error tagging '{entry['link'].get('url', '')}': {e}",
                file=sys.stderr,
            )

    print(f"Done. Tagged {tagged} article(s).")


def cmd_auto_tag(client: GoodLinksClient, args: argparse.Namespace) -> None:
    """
    auto-tag untagged articles using claude AI analysis.

    fetches untagged articles and analyzes their content (from local goodlinks
    or via curl) to suggest appropriate tags from the existing tag collection.
    applies both 'claude-auto' tag and the suggested tag to each article.

    does not create new tags - only uses tags that already exist in the collection.
    """
    print("Fetching existing tags...")
    available_tags = client.get_all_tags()
    if not available_tags:
        print(
            "no tags found in collection. cannot proceed without existing tags to suggest from."
        )
        return

    print(f"found {len(available_tags)} existing tag(s)")
    print(f"  {', '.join(available_tags)}\n")

    print("fetching untagged articles...")
    all_links = client.get_all_links()
    untagged = [l for l in all_links if not l.get("tags")]
    if not untagged:
        print("no untagged articles found. nothing to do.")
        return

    print(f"found {len(untagged)} untagged article(s)\n")

    # initialize claude client
    claude = Anthropic()

    BATCH_SIZE = 20
    to_tag: list[dict] = []
    tagged = 0
    flushed = 0  # index into to_tag up to which we've already written

    for i, link in enumerate(untagged, 1):
        link_id = link.get("id")
        url = link.get("url", "")
        title = (link.get("title") or "Untitled")[:70]

        print(f"[{i}/{len(untagged)}] {title}")

        # try to get content from local API first
        content = client.get_link_content(link_id)

        # if not available locally, try curl
        if not content and url:
            print(f"      (fetching from {_domain_of(url)}...)", end=" ", flush=True)
            html = _fetch_url_content(url, timeout=args.timeout)
            if html:
                content = html
                print("done")
            else:
                print("unavailable")

        if not content:
            if not args.dry_run and not args.json:
                try:
                    client.update_link(link_id, added_tags=["content-unavailable"])
                    print(f"      tagged: content-unavailable")
                except requests.exceptions.HTTPError as e:
                    print(
                        f"      error tagging content-unavailable: {e}", file=sys.stderr
                    )
            else:
                print(
                    f"      skipped: could not fetch content (would tag content-unavailable)"
                )
            continue

        # sample the content
        sample = _extract_text_sample(content, max_length=4000)

        # suggest a tag
        suggested_tag = _suggest_tag_for_content(claude, sample, available_tags)
        if not suggested_tag:
            print(f"      skipped: could not determine tag")
            continue

        tags_to_add = ["claude-auto"]
        if suggested_tag not in tags_to_add:
            tags_to_add.append(suggested_tag)

        to_tag.append(
            {
                "link": link,
                "suggested_tag": suggested_tag,
                "tags_to_add": tags_to_add,
            }
        )
        print(f"      -> {suggested_tag}")

        # flush a batch every BATCH_SIZE articles (skip if dry-run or json)
        if not args.dry_run and not args.json and len(to_tag) - flushed >= BATCH_SIZE:
            batch = to_tag[flushed : flushed + BATCH_SIZE]
            print(
                f"\n  -- writing batch {flushed // BATCH_SIZE + 1} "
                f"({len(batch)} articles) to goodlinks --"
            )
            for entry in batch:
                try:
                    client.update_link(
                        entry["link"]["id"], added_tags=entry["tags_to_add"]
                    )
                    tagged += 1
                except requests.exceptions.HTTPError as e:
                    print(
                        f"  error tagging '{entry['link'].get('url', '')}': {e}",
                        file=sys.stderr,
                    )
            flushed += BATCH_SIZE
            print()

    if not to_tag:
        print("\nno articles could be auto-tagged.")
        return

    if args.json:
        output = [
            {
                "id": e["link"]["id"],
                "url": e["link"].get("url"),
                "title": e["link"].get("title"),
                "suggested_tag": e["suggested_tag"],
                "tags_to_add": e["tags_to_add"],
            }
            for e in to_tag
        ]
        print(json.dumps(output, indent=2))
        return

    if args.dry_run:
        print(f"\n[dry-run] would tag {len(to_tag)} article(s). no changes made.")
        return

    # flush any remaining articles that didn't fill a full batch
    remaining = to_tag[flushed:]
    if remaining:
        print(
            f"\n  -- writing final batch ({len(remaining)} article(s)) to goodlinks --"
        )
        for entry in remaining:
            try:
                client.update_link(entry["link"]["id"], added_tags=entry["tags_to_add"])
                tagged += 1
            except requests.exceptions.HTTPError as e:
                print(
                    f"  error tagging '{entry['link'].get('url', '')}': {e}",
                    file=sys.stderr,
                )

    print(f"\ndone. tagged {tagged}/{len(to_tag)} article(s).")


# ---------------------------------------------------------------------------
# CLI wiring


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="goodlinks-gardening",
        description=(
            "curate and manage your goodlinks reading collection via the local REST API.\n\n"
            "goodlinks must be running with the API enabled (Settings -> API) before use.\n"
            "default API endpoint: " + DEFAULT_BASE_URL
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        metavar="URL",
        help=f"GoodLinks API base URL (default: {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--token",
        default=None,
        metavar="TOKEN",
        help=(
            "API bearer token; overrides GOODLINKS_API env var and "
            "~/.credentials/goodlinks-token.txt"
        ),
    )

    sub = parser.add_subparsers(
        dest="command", required=True, title="gardening commands", metavar="<command>"
    )

    # ---- tags ---------------------------------------------------------------
    p_tags = sub.add_parser(
        "tags",
        help="list all tags with per-tag article counts",
        description=(
            "display every tag in your collection alongside the number of articles that use it.\n"
            "results are sorted by count (highest first), then alphabetically."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_tags.add_argument(
        "--json",
        action="store_true",
        help="emit results as a JSON array instead of a formatted table",
    )
    p_tags.set_defaults(func=cmd_tags)

    # ---- urls ---------------------------------------------------------------
    p_urls = sub.add_parser(
        "urls",
        help="list URLs and show domain frequency statistics",
        description=(
            "by default, prints a table of domains ordered by how many articles come from each.\n"
            "use --urls to dump every article URL (one per line) for scripting.\n"
            "use --min-count to adjust the visibility threshold for the domain table."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_urls.add_argument(
        "--urls",
        action="store_true",
        help="print all article URLs (one per line) instead of the domain frequency table",
    )
    p_urls.add_argument(
        "--min-count",
        type=int,
        default=2,
        metavar="N",
        help="only show domains that have at least N articles (default: 2)",
    )
    p_urls.add_argument(
        "--json",
        action="store_true",
        help="emit domain statistics as a JSON array instead of a formatted table",
    )
    p_urls.set_defaults(func=cmd_urls)

    # ---- tag-domain ---------------------------------------------------------
    p_td = sub.add_parser(
        "tag-domain",
        help="add a tag to all articles from a specific domain",
        description=(
            "find every article whose URL belongs to DOMAIN and add TAG to those that\n"
            "don't already have it. subdomains are matched automatically.\n\n"
            "example:\n"
            "  goodlinks-gardening.py tag-domain --domain nytimes.com --tag news\n"
            "  goodlinks-gardening.py tag-domain --domain github.com --tag dev --dry-run"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_td.add_argument(
        "--domain",
        required=True,
        metavar="DOMAIN",
        help=(
            "root domain to match (e.g. 'nytimes.com'); subdomains such as "
            "'www.nytimes.com' are included automatically"
        ),
    )
    p_td.add_argument(
        "--tag",
        required=True,
        metavar="TAG",
        help="tag to add to matching articles that don't already have it",
    )
    p_td.add_argument(
        "--dry-run",
        action="store_true",
        help="preview which articles would be tagged without making any changes",
    )
    p_td.set_defaults(func=cmd_tag_domain)

    # ---- dedupe -------------------------------------------------------------
    p_dd = sub.add_parser(
        "dedupe",
        help="find (and optionally delete) duplicate URLs",
        description=(
            "scan the collection for articles that share the same URL and report them.\n"
            "by default this is read-only. pass --delete to remove all but the oldest\n"
            "saved copy of each duplicated URL.\n\n"
            "example:\n"
            "  goodlinks-gardening.py dedupe\n"
            "  goodlinks-gardening.py dedupe --delete"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_dd.add_argument(
        "--delete",
        action="store_true",
        help=(
            "delete duplicate copies, keeping the oldest saved version of each URL; "
            "without this flag the command is read-only"
        ),
    )
    p_dd.add_argument(
        "--json",
        action="store_true",
        help="emit duplicate groups as a JSON array instead of a formatted report",
    )
    p_dd.set_defaults(func=cmd_dedupe)

    # ---- dead-links ---------------------------------------------------------
    p_dl = sub.add_parser(
        "dead-links",
        help="identify links that are no longer reachable or available offline",
        description=(
            "scan articles for dead links using two checks:\n"
            "  1. offline availability -- articles with a word count of 0 were never\n"
            "     successfully fetched by goodlinks (tagged 'offline-unavailable')\n"
            "  2. live HTTP probe -- URLs returning 4xx/5xx are tagged 'http-NNN';\n"
            "     timeouts get 'http-timeout'; connection failures get 'http-error'\n\n"
            "scope is required: pass --tag to check a specific tag or --all for the\n"
            "full collection. combine with --unread or --untagged to narrow the scope.\n\n"
            "example:\n"
            "  goodlinks-gardening.py dead-links --tag dev\n"
            "  goodlinks-gardening.py dead-links --all --unread --dry-run\n"
            "  goodlinks-gardening.py dead-links --all --untagged\n"
            "  goodlinks-gardening.py dead-links --tag news --timeout 5"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    scope = p_dl.add_mutually_exclusive_group(required=True)
    scope.add_argument(
        "--tag",
        metavar="TAG",
        help="check only articles that have this tag",
    )
    scope.add_argument(
        "--all",
        action="store_true",
        help="check all articles in the collection",
    )
    p_dl.add_argument(
        "--unread",
        action="store_true",
        help="further restrict the scope to articles that have not been read yet",
    )
    p_dl.add_argument(
        "--untagged",
        action="store_true",
        help="further restrict the scope to articles that have no tags",
    )
    p_dl.add_argument(
        "--timeout",
        type=int,
        default=30,
        metavar="SECONDS",
        help="HTTP request timeout in seconds per URL (default: 30)",
    )
    p_dl.add_argument(
        "--workers",
        type=int,
        default=10,
        metavar="N",
        help="number of parallel HTTP probes to run at once (default: 10)",
    )
    p_dl.add_argument(
        "--dry-run",
        action="store_true",
        help="report dead links and proposed tags without modifying any articles",
    )
    p_dl.add_argument(
        "--json",
        action="store_true",
        help="emit results as a JSON array instead of a formatted report",
    )
    p_dl.set_defaults(func=cmd_dead_links)

    # ---- auto-tag -----------------------------------------------------------
    p_at = sub.add_parser(
        "auto-tag",
        help="auto-tag untagged articles using Claude AI analysis",
        description=(
            "analyze untagged articles to suggest tags from your existing tag collection.\n"
            "uses claude to read article content and recommend the best matching tag.\n"
            "applies both 'claude-auto' tag and the suggested tag to each article.\n\n"
            "content is fetched from:\n"
            "  1. local goodlinks API (if article was previously cached)\n"
            "  2. direct HTTP fetch via curl (if not cached)\n\n"
            "example:\n"
            "  goodlinks-gardening.py auto-tag --dry-run\n"
            "  goodlinks-gardening.py auto-tag"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_at.add_argument(
        "--timeout",
        type=int,
        default=30,
        metavar="SECONDS",
        help="HTTP request timeout in seconds per URL (default: 30)",
    )
    p_at.add_argument(
        "--dry-run",
        action="store_true",
        help="preview which articles would be tagged without making any changes",
    )
    p_at.add_argument(
        "--json",
        action="store_true",
        help="emit results as a JSON array instead of a formatted report",
    )
    p_at.set_defaults(func=cmd_auto_tag)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    token = _resolve_token(cli_token=args.token)
    client = GoodLinksClient(base_url=args.base_url, token=token)

    try:
        args.func(client, args)
    except requests.exceptions.ConnectionError:
        print(
            f"\nerror: could not connect to the goodlinks API at {args.base_url}\n"
            "  - make sure goodlinks is running.\n"
            "  - enable the API under Settings -> API in goodlinks.\n"
            "  - if you changed the port, pass --base-url with the correct address.",
            file=sys.stderr,
        )
        sys.exit(1)
    except requests.exceptions.HTTPError as e:
        print(f"API error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
