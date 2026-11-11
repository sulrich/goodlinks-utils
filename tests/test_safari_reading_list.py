"""
tests for the safari reading list helpers in goodlinks-gardening.py.

run with:
  uv run --with pytest --with requests --with anthropic pytest tests/
"""

import argparse
import importlib.util
import plistlib
from datetime import datetime
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "goodlinks-gardening.py"
_spec = importlib.util.spec_from_file_location("gardening", SCRIPT)
assert _spec is not None and _spec.loader is not None
gardening = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gardening)


def _leaf(url: str, title: str, uuid: str, added: datetime | None) -> dict:
    leaf = {
        "WebBookmarkType": "WebBookmarkTypeLeaf",
        "URLString": url,
        "URIDictionary": {"title": title},
        "WebBookmarkUUID": uuid,
    }
    if added is not None:
        leaf["ReadingList"] = {"DateAdded": added, "PreviewText": "..."}
    return leaf


def _bookmarks(reading_list: list[dict] | None) -> dict:
    """build a root plist shaped like safari's Bookmarks.plist."""
    children = [
        {
            "Title": "BookmarksBar",
            "WebBookmarkType": "WebBookmarkTypeList",
            "Children": [_leaf("https://bar.example/", "bar", "BAR-1", None)],
        }
    ]
    if reading_list is not None:
        children.append(
            {
                "Title": "com.apple.ReadingList",
                "WebBookmarkType": "WebBookmarkTypeList",
                "Children": reading_list,
            }
        )
    return {"WebBookmarkType": "WebBookmarkTypeList", "Children": children}


@pytest.fixture
def root() -> dict:
    return _bookmarks(
        [
            _leaf(
                "https://a.example/one",
                "one",
                "UUID-A",
                datetime(2026, 11, 1, 12, 30, 5),
            ),
            _leaf("https://b.example/two", "two", "UUID-B", None),
        ]
    )


def test_items_parse_url_and_title(root):
    items = gardening._sl_items(root)
    assert [(i["url"], i["title"]) for i in items] == [
        ("https://a.example/one", "one"),
        ("https://b.example/two", "two"),
    ]


def test_items_date_added_is_iso_utc(root):
    assert gardening._sl_items(root)[0]["addedAt"] == "2026-11-01T12:30:05Z"


def test_items_without_date_added_have_none(root):
    assert gardening._sl_items(root)[1]["addedAt"] is None


def test_items_title_whitespace_is_collapsed():
    root = _bookmarks(
        [_leaf("https://c.example/", " Jules Verne\u2019s\n Paris ", "C", None)]
    )
    assert gardening._sl_items(root)[0]["title"] == "Jules Verne\u2019s Paris"


def test_items_missing_reading_list_folder_is_empty():
    assert gardening._sl_items(_bookmarks(None)) == []


class _FakeClient:
    """records add_link calls; goodlinks is empty."""

    def __init__(self):
        self.added: list[dict] = []

    def get_all_links(self):
        return []

    def add_link(self, **kwargs):
        self.added.append(kwargs)
        return {}


def test_import_does_not_backdate_new_links(tmp_path, root):
    # goodlinks sorts unread by addedAt, so backdated imports get buried.
    path = tmp_path / "Bookmarks.plist"
    path.write_bytes(plistlib.dumps(root, fmt=plistlib.FMT_BINARY))
    client = _FakeClient()
    args = argparse.Namespace(
        bookmarks=str(path), tag="safari", dry_run=False, json=False
    )

    gardening.cmd_safari_import(client, args)

    assert [a.get("added_at") for a in client.added] == [None, None]
