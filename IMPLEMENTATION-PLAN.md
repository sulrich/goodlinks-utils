# safari reading list -> goodlinks import

start time: 2026-11-11
end time: 2026-11-11

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
  can only add).
- **plist removal doesn't sync.** tested 2026-11-11 with `--limit 1`: the
  item disappeared on this mac but stayed in icloud and on the work laptop.
  every item carries a cloudkit record id (`Sync.ServerID`), and safari's
  sync agent only pushes changes safari made itself, so a missing record is
  never sent as a delete.
- decision (user): import only. the user clears the reading list in safari
  ("Clear All Items") once the command reports everything is in goodlinks.
  `--keep`, `--limit`, the plist write path and the safari-running check
  were removed.

## stage 1: reading-list parse/remove helpers
**goal**: `_sl_items()` returns {url, title, addedAt, uuid} per item;
`_sl_remove()` drops items by uuid from the loaded plist.
**success criteria**: unit tests pass against a synthetic binary plist.
**tests**: items parsed with ISO-8601 UTC dates; missing reading-list folder
-> []; titles flattened to one line. (removal helpers later dropped.)
**status**: complete

## stage 2: safari-import command
**goal**: `safari-import` adds missing items to goodlinks (tag `safari`,
preserve DateAdded) and says when the reading list is safe to clear.
**success criteria**: `--dry-run` and `--json` make no changes; re-running
adds nothing new; never writes Bookmarks.plist.
**status**: complete (first version also removed items from the plist; that
was dropped after stage 3 showed it doesn't sync)

## stage 3: real-world check
**goal**: dry-run against the real reading list; import + remove a single item;
confirm removal syncs.
**success criteria**: item in goodlinks; reading list state consistent across
devices.
**status**: complete. import works (12 items found, 1 imported). removal
did not sync to icloud, which led to the import-only decision above.

## commit log
- 47cbc5b add safari-import: move reading list items into goodlinks
- 5933205 safari-import: flatten line breaks in reading list titles
- d9cff3d safari-import: import only, stop editing Bookmarks.plist
