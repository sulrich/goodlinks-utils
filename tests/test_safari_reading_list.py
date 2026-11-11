"""
tests for the safari reading list helpers in goodlinks-gardening.py.

run with:
  uv run --with pytest --with requests --with anthropic pytest tests/
"""

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


def test_items_parse_url_title_and_uuid(root):
    items = gardening._sl_items(root)
    assert [(i["url"], i["title"], i["uuid"]) for i in items] == [
        ("https://a.example/one", "one", "UUID-A"),
        ("https://b.example/two", "two", "UUID-B"),
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


def test_remove_drops_only_matching_uuids(root):
    removed = gardening._sl_remove(root, {"UUID-A"})
    assert removed == 1
    assert [i["uuid"] for i in gardening._sl_items(root)] == ["UUID-B"]


def test_remove_leaves_other_folders_untouched(root):
    gardening._sl_remove(root, {"UUID-A", "UUID-B", "BAR-1"})
    bar = root["Children"][0]["Children"]
    assert [c["WebBookmarkUUID"] for c in bar] == ["BAR-1"]


def test_write_round_trips_and_keeps_backup(tmp_path, root):
    path = tmp_path / "Bookmarks.plist"
    path.write_bytes(plistlib.dumps(root, fmt=plistlib.FMT_BINARY))
    loaded = gardening._sl_load(path)
    gardening._sl_remove(loaded, {"UUID-A"})

    backup = gardening._sl_write(path, loaded)

    remaining = gardening._sl_items(gardening._sl_load(path))
    assert [i["uuid"] for i in remaining] == ["UUID-B"]
    assert len(gardening._sl_items(gardening._sl_load(backup))) == 2
