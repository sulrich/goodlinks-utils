# goodlinks garden - developer guide

`goodlinks-gardening.py` is a CLI tool for curating a goodlinks reading collection
via the goodlinks local REST API.

## architecture

the script is organized into three layers:

```
main() / build_parser()     <- CLI wiring (argparse subcommands)
GoodLinksClient             <- API wrapper (HTTP, pagination, auth-free)
cmd_*() functions           <- one function per gardening command
```

### adding a new command

1. write a function with the signature:
   ```python
   def cmd_yourcommand(client: GoodLinksClient, args: argparse.Namespace) -> None:
   ```
   place it in the **gardening commands** section, alongside `cmd_tags`,
   `cmd_urls`, and `cmd_tag_domain`.

2. register a subparser in `build_parser()`:
   ```python
   p_foo = sub.add_parser("yourcommand", help="one-line description", ...)
   p_foo.add_argument(...)          # all flags need help= strings
   p_foo.set_defaults(func=cmd_yourcommand)
   ```
   that's it - the `main()` dispatcher calls `args.func(client, args)`
   automatically, so no other wiring is needed.

### adding a new API method

add a method to `GoodLinksClient`. keep methods thin:

- one network call per method
- raise on HTTP errors (`resp.raise_for_status()`)
- return raw parsed JSON (let callers decide what to do with it)

`get_all_links()` handles pagination already; follow the same `offset` /
`hasMore` pattern for any other paginated endpoint.

## API reference

**base URL:** `http://localhost:9428/api/v1` (configurable via `--base-url`)

key endpoints used:

| method  | path          | purpose                                               |
|---------|---------------|-------------------------------------------------------|
| `GET`   | `/lists/all`  | fetch all links (paginated via `limit` / `offset`)    |
| `PATCH` | `/links/{id}` | update a link; use `addedTags` / `removedTags` arrays |
| `GET`   | `/tags`       | fetch all tag strings (no counts - aggregate locally) |

full docs: https://goodlinks.app/api/

### pagination

`get_all_links()` handles pagination automatically. every endpoint that returns
a list uses `{"data": [...], "hasMore": bool}`. use `limit=1000` (the API max)
to minimize round-trips.

### tag mutation

use `addedTags` / `removedTags` in a `PATCH /links/{id}` body rather than
sending the full `tags` array. this avoids overwriting concurrent changes.

## conventions

- **domain normalization:** always use `_normalise_domain()` / `_domain_of()`
  so that `www.example.com` and `example.com` are treated as the same domain.
- **output formats:** commands that print tables should also support `--json`
  for machine-readable output.
- **dry-run:** any command that mutates data should accept `--dry-run` and
  print what would change without making any modifications.
- **error handling:** let `main()` catch `ConnectionError` and `HTTPError`.
  inside commands, only catch errors you can meaningfully recover from.

## ideas for future gardening tasks

- `untag` - remove a tag from all articles matching a domain or search term
- `retag` - rename a tag across the entire collection
- `report` - generate a markdown summary of reading stats
  (articles/week, top domains, etc.)
- `export` - export the collection to CSV / JSON / markdown
- `bulk-tag` - apply tags from a YAML/JSON mapping file (domain -> tags)
- `stale` - list articles saved more than N days ago that are still unread

## running the script

the script uses [uv](https://docs.astral.sh/uv/) with inline PEP 723 metadata
so it's fully self-contained - no virtual environment or manual `pip install`
needed.

```bash
# make executable once
chmod +x goodlinks-gardening.py

# then run directly
./goodlinks-gardening.py tags
./goodlinks-gardening.py tags --json

./goodlinks-gardening.py urls
./goodlinks-gardening.py urls --min-count 5
./goodlinks-gardening.py urls --urls          # raw URL dump, one per line

./goodlinks-gardening.py tag-domain --domain github.com --tag dev --dry-run
./goodlinks-gardening.py tag-domain --domain nytimes.com --tag news
```

or invoke explicitly via `uv run`:

```bash
uv run goodlinks-gardening.py tags
uv run goodlinks-gardening.py tag-domain --domain github.com --tag dev --dry-run
```

use `--help` on any subcommand for full flag documentation:

```bash
./goodlinks-gardening.py --help
./goodlinks-gardening.py tag-domain --help
```

### adding new dependencies

add packages to the `# dependencies = [...]` block at the top of
`goodlinks-gardening.py` - `uv` will install them automatically on the next run.
