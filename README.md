# goodlinks-gardening

goodlinks (https://goodlinks.app) remains my favorite read-it-later app. i store
things in it like they're going out of style -- it's the final resting place of my
flirtations with pocket, instapaper, and whatever else i tried before accepting that
i was just going to hoard articles indefinitely instead of actually reading them.

when they dropped a local HTTP API in the 3.2 release, i could not look away. the
collection had become a hot mess of untagged links, dead URLs, and duplicates that
would make a librarian cry. this script is the shovel.

## how it works

it's a single python script with inline PEP 723 metadata, so [uv](https://docs.astral.sh/uv/)
handles all the dependencies automatically. no virtualenv ceremony, no pip install,
just run it.

```bash
chmod +x goodlinks-gardening.py
./goodlinks-gardening.py --help
```

or if you prefer being explicit about it:

```bash
uv run goodlinks-gardening.py --help
```

goodlinks needs to be running with the local API enabled (settings -> API) before
any of this works. it talks to `http://localhost:9428/api/v1` by default.

## auth

the script looks for an API bearer token in ascending precedence order:

1. `~/.credentials/goodlinks-token.txt` (lowest priority)
2. `GOODLINKS_API` environment variable
3. `--token` flag on the CLI (wins)

if goodlinks doesn't require auth on your setup, you can skip this entirely.

## commands

### `tags`

shows every tag in the collection with a count of articles per tag, sorted by
frequency. useful for getting a feel for what the taxonomy actually looks like.

```bash
./goodlinks-gardening.py tags
./goodlinks-gardening.py tags --json
```

### `urls`

domain frequency stats -- which sites have you saved the most articles from? useful
for identifying sources worth tagging in bulk, or for confronting your news diet.

```bash
./goodlinks-gardening.py urls
./goodlinks-gardening.py urls --min-count 5   # only domains with 5+ articles
./goodlinks-gardening.py urls --urls          # raw URL dump, one per line
```

### `tag-domain`

bulk-tags every article from a given domain that doesn't already have the tag.
subdomains are handled automatically -- targeting `nytimes.com` also catches
`www.nytimes.com`. use `--dry-run` first if you value your sanity.

```bash
./goodlinks-gardening.py tag-domain --domain github.com --tag dev --dry-run
./goodlinks-gardening.py tag-domain --domain nytimes.com --tag news
```

### `dedupe`

finds articles with identical URLs. by default just reports them. pass `--delete`
to remove all but the oldest saved copy of each duplicate. keeps the oldest because
at least that one you presumably meant to save.

```bash
./goodlinks-gardening.py dedupe
./goodlinks-gardening.py dedupe --delete
./goodlinks-gardening.py dedupe --json
```

### `dead-links`

probes your collection for articles that have gone dark. an article is flagged as
dead if goodlinks couldn't fetch the content (word count of zero) or if the URL
returns a 4xx/5xx, times out, or outright refuses to connect. dead articles get
tagged with the reason -- `http-404`, `http-timeout`, `http-error`, or
`offline-unavailable` -- so you can filter and clean them up later.

requires `--tag TAG` to scope to a specific tag, or `--all` to check everything.
combine with `--unread` or `--untagged` to narrow the blast radius.

```bash
./goodlinks-gardening.py dead-links --tag dev --dry-run
./goodlinks-gardening.py dead-links --all --unread
./goodlinks-gardening.py dead-links --tag news --workers 10 --timeout 15
```

### `auto-tag`

the lazy option. uses claude (via the anthropic API) to look at the content of
untagged articles and pick the best matching tag from what already exists in your
collection. it will not invent new tags. articles get a `claude-auto` tag in
addition to whatever it suggests, so you can audit the results later.

content comes from the goodlinks local API first; if that's not available it fetches
the URL directly. articles where content can't be retrieved get tagged
`content-unavailable` instead.

requires `ANTHROPIC_API_KEY` to be set. uses claude haiku to keep costs reasonable.

```bash
./goodlinks-gardening.py auto-tag --dry-run
./goodlinks-gardening.py auto-tag
./goodlinks-gardening.py auto-tag --json
```

## output formats

most commands support `--json` for machine-readable output. commands that mutate
data support `--dry-run` to preview what would change without touching anything.

## goodlinks-visuals

if gardening is the shovel, visuals is the graph you make after shoveling to feel
like the hoarding was somehow intentional. it fetches the same collection via the
local API and produces a JSON dataset plus an HTML stub you can open in a browser
or drop into a hugo site.

```bash
chmod +x goodlinks-visuals.py
./goodlinks-visuals.py
```

or explicitly:

```bash
uv run goodlinks-visuals.py
```

same auth precedence as the gardening script. goodlinks must be running with the
API enabled before this does anything useful.

### what it generates

running the script writes into `goodlinks-stats/` by default:

```
goodlinks-stats/
  data/goodlinks-data.json   ← the full dataset
  index.html                 ← HTML stub that loads the visualizations
```

the dataset has four keys:

- `articles` -- every link as a table row, sorted by read date descending
- `heatmap` -- `{date: count}` of reads per calendar day (github-style grid)
- `tag_series` -- `{tag: {month: count}}` for tracking tag volume over time
- `domain_series` -- `{domain: {month: count}}` for the same but by source

### options

```bash
./goodlinks-visuals.py --output-dir ~/Sites/reading-stats
./goodlinks-visuals.py --pretty           # human-readable JSON (larger file)
./goodlinks-visuals.py --base-url http://localhost:9428/api/v1
./goodlinks-visuals.py --token mytoken
```

### hugo export

if you maintain a hugo site, `--hugo-dir` copies the dataset JSON into a page
bundle and installs the shortcode templates into `layouts/shortcodes/`. the
shortcodes let you embed the charts anywhere in your content without duplicating
the visualization code.

```bash
./goodlinks-visuals.py \
  --hugo-dir ~/Sites/my-hugo-site \
  --page-bundle content/posts/reading-stats
```

this writes `goodlinks-data.json` into the page bundle directory and drops four
shortcode templates into `layouts/shortcodes/`:

| shortcode              | what it renders                          |
|------------------------|------------------------------------------|
| `goodlinks-plotly`     | stacked area chart of tags over time     |
| `goodlinks-heatmap`    | calendar heatmap of reading activity     |
| `goodlinks-sunburst`   | sunburst chart of domain/tag breakdown   |
| `goodlinks-table`      | sortable/filterable article table        |

`--page-bundle` is required when `--hugo-dir` is set.

### templates

the `templates/` directory is where the HTML lives. the top-level templates
(`index.html`, `heatmap.html`, `sunburst.html`, `table.html`) are rendered via
jinja2 to produce the standalone output. `templates/shortcodes/` holds the four
hugo shortcode templates that get copied on `--hugo-dir` export. if you want to
customize the look, edit the templates -- the script just renders them, it doesn't
own the markup.

## ideas on the backlog

- `untag` -- remove a tag from all articles matching a domain or search term
- `retag` -- rename a tag across the entire collection
- `report` -- markdown summary of reading stats (articles/week, top domains, etc.)
- `export` -- dump the collection to CSV / JSON / markdown
- `bulk-tag` -- apply tags from a YAML/JSON mapping file (domain -> tags)
- `stale` -- list articles saved more than N days ago that are still unread
