# safari reading list -> goodlinks import

start time: 2026-11-11
end time:

## overview

save articles to the safari reading list on the work laptop (no goodlinks
there), let icloud sync them to this mac, then pull them into goodlinks and
clear them from the reading list.

findings:

- the reading list lives in `~/Library/Safari/Bookmarks.plist` (binary plist),
  in the child folder titled `com.apple.ReadingList`. each item is a leaf with
  `URLString`, `URIDictionary.title`, `ReadingList.DateAdded` and
  `WebBookmarkUUID`.
- reading/writing that file requires full disk access for the calling app.
- safari has no scripting hook for removing reading-list items (applescript
  can only add), so removal means editing the plist directly. whether icloud
  sync propagates (or reverts) that edit has to be checked by hand.

## stage 1: reading-list parse/remove helpers
**goal**: `_sl_items()` returns {url, title, addedAt, uuid} per item;
`_sl_remove()` drops items by uuid from the loaded plist.
**success criteria**: unit tests pass against a synthetic binary plist.
**tests**: items parsed with ISO-8601 UTC dates; missing reading-list folder
-> []; removal leaves other bookmarks and non-matching items untouched.
**status**: complete

## stage 2: safari-import command
**goal**: `safari-import` adds missing items to goodlinks (tag `safari`,
preserve DateAdded), then removes imported + already-present items from the
reading list (backup first, refuse while safari is running).
**success criteria**: `--dry-run` and `--json` make no changes; `--keep`
skips removal; `--limit N` caps how many items are processed; a failed add is
never removed.
**status**: complete

## stage 3: real-world check
**goal**: dry-run against the real reading list; import + remove a single item
with `--limit 1`; confirm it stays gone after icloud sync on both machines.
**success criteria**: item in goodlinks, gone from reading list on both macs
after sync.
**status**: blocked - needs full disk access to read the real Bookmarks.plist; removal path verified against a synthetic plist

## commit log
