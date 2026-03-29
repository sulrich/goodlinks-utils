#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
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

import requests

DEFAULT_BASE_URL = "http://localhost:9428/api/v1"
TOKEN_FILE = Path("~/.credentials/goodlinks-token.txt").expanduser()
DEFAULT_OUTPUT_DIR = Path("goodlinks-stats")


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
# HTML stub
# ---------------------------------------------------------------------------

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>goodlinks stats</title>
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js" charset="utf-8"></script>
  <style>
    *, *::before, *::after { box-sizing: border-box; }
    body {
      font-family: system-ui, -apple-system, sans-serif;
      margin: 0;
      padding: 1rem 2rem;
      background: #f9f9f9;
      color: #222;
    }
    h1 { font-size: 1.4rem; margin-bottom: 0.25rem; }
    h2 { font-size: 1.1rem; margin: 2rem 0 0.5rem; color: #444; }
    .chart { background: #fff; border-radius: 6px; padding: 0.5rem; margin-bottom: 1.5rem; }
    .chart-error { color: #c00; font-size: 0.85rem; padding: 0.5rem; }

    /* heatmap */
    .heatmap-toolbar { margin-bottom: 0.4rem; font-size: 0.85rem; }
    #heatmap-year { padding: 0.2rem 0.4rem; font-size: 0.85rem; border: 1px solid #ccc; border-radius: 3px; }
    #heatmap { overflow-x: auto; }

    /* sortable / resizable table */
    .table-section { width: 80%; }
    .table-toolbar { display: flex; align-items: center; gap: 1rem; margin-bottom: 0.4rem; }
    #table-filter { padding: 0.3rem 0.5rem; font-size: 0.85rem; width: 260px; border: 1px solid #ccc; border-radius: 3px; }
    #table-count { font-size: 0.78rem; color: #666; }
    #table-container { overflow-x: auto; overflow-y: auto; max-height: 430px; border: 1px solid #ddd; border-radius: 4px; width: 100%; }
    table { border-collapse: collapse; width: 100%; font-size: 0.78rem; table-layout: fixed; }
    thead th { position: sticky; top: 0; z-index: 1; background: #eee; }
    th, td { padding: 0.28rem 0.5rem; border-bottom: 1px solid #e4e4e4; border-right: 1px solid #e4e4e4; text-align: left; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    th { position: relative; cursor: pointer; user-select: none; padding-right: 14px; }
    th:hover { background: #ddd; }
    th.sort-asc::after  { content: " ▲"; }
    th.sort-desc::after { content: " ▼"; }
    tr:nth-child(even) td { background: #fafafa; }
    td a { color: #0057b7; text-decoration: none; }
    td a:hover { text-decoration: underline; }
    /* column widths: fixed cols take declared widths, title gets the rest */
    .col-date   { width: 74px; }
    .col-domain { width: 140px; }
    .col-tags   { width: 180px; white-space: normal; }
    /* column resize handle */
    .col-resize-handle {
      position: absolute; right: 0; top: 0; bottom: 0; width: 5px;
      cursor: col-resize; z-index: 2;
    }
    .col-resize-handle:hover { background: rgba(0,0,0,0.18); }
    .tag-pill {
      display: inline-block;
      background: #e2eaff;
      border-radius: 3px;
      padding: 1px 4px;
      margin: 1px 1px;
      font-size: 0.7rem;
    }
  </style>
</head>
<body>
  <h1>goodlinks stats</h1>
  <p id="summary-line">loading dataset…</p>

  <h2>reading heatmap</h2>
  <div class="heatmap-toolbar">
    <label for="heatmap-year">year: </label>
    <select id="heatmap-year"></select>
  </div>
  <div id="heatmap" class="chart"></div>



  <h2>reading by year › domain › tag</h2>
  <p style="font-size:0.8rem;color:#666;margin:-0.4rem 0 0.4rem">
    click any segment to drill down · click the centre to go back up
  </p>
  <div id="sunburst" class="chart"></div>

  <div class="table-section">
  <h2>articles</h2>
  <div class="table-toolbar">
    <input id="table-filter" type="search" placeholder="filter by title, tag, domain…" />
    <span id="table-count"></span>
  </div>
  <div id="table-container">
    <table id="articles-table">
      <thead>
        <tr>
          <th data-col="addedDate" class="col-date sort-desc">added</th>
          <th data-col="readDate"  class="col-date">read</th>
          <th data-col="title">title</th>
          <th data-col="domain"    class="col-domain">domain</th>
          <th data-col="tags"      class="col-tags">tags</th>
        </tr>
      </thead>
      <tbody></tbody>
    </table>
  </div>
  </div>

  <script>
  // ---------------------------------------------------------------------------
  // load dataset
  // ---------------------------------------------------------------------------
  async function loadData() {
    const resp = await fetch('data/goodlinks-data.json');
    if (!resp.ok) throw new Error(`fetch failed: ${resp.status}`);
    return resp.json();
  }

  function chartError(divId, err) {
    document.getElementById(divId).innerHTML =
      `<p class="chart-error">render error: ${err.message}</p>`;
    console.error(divId, err);
  }

  // ---------------------------------------------------------------------------
  // heatmap (github-style contribution graph, one year at a time)
  // ---------------------------------------------------------------------------

  // Build a 7-row × NUM_WEEKS-col grid for a single calendar year.
  // Rows: 0=Sun … 6=Sat (matching JS getDay()).
  // Cols: week index 0-53, where week 0 is the column containing Jan 1.
  // Returns numeric column tick positions for month labels.
  function buildYearGrid(year, heatmap) {
    const NUM_WEEKS = 54;
    const MONTH_NAMES = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

    const z    = Array.from({length: 7}, () => Array(NUM_WEEKS).fill(null));
    const text = Array.from({length: 7}, () => Array(NUM_WEEKS).fill(''));

    const jan1 = new Date(year, 0, 1, 12);
    const jan1dow = jan1.getDay(); // 0=Sun

    const cur = new Date(year, 0, 1, 12);
    while (cur.getFullYear() === year) {
      const mm  = String(cur.getMonth() + 1).padStart(2, '0');
      const dd  = String(cur.getDate()).padStart(2, '0');
      const dateStr = `${year}-${mm}-${dd}`;
      const doy     = Math.round((cur - jan1) / 86400000);
      const weekCol = Math.floor((doy + jan1dow) / 7);
      const dayRow  = cur.getDay();

      if (weekCol < NUM_WEEKS) {
        const count = heatmap[dateStr] || 0;
        z[dayRow][weekCol]    = count;
        text[dayRow][weekCol] = `${dateStr}: ${count} article(s)`;
      }
      cur.setDate(cur.getDate() + 1);
    }

    // Month tick positions: numeric column index where each month starts
    const monthTickVals = [];
    const monthTickText = [];
    for (let m = 0; m < 12; m++) {
      const d    = new Date(year, m, 1, 12);
      const doy  = Math.round((d - jan1) / 86400000);
      const wcol = Math.floor((doy + jan1dow) / 7);
      if (wcol < NUM_WEEKS) {
        monthTickVals.push(wcol);
        monthTickText.push(MONTH_NAMES[m]);
      }
    }

    return { z, text, monthTickVals, monthTickText };
  }

  function renderHeatmap(heatmap, year) {
    try {
      if (Object.keys(heatmap).length === 0) {
        document.getElementById('heatmap').innerHTML =
          '<p style="padding:0.5rem;color:#888">no read dates in dataset</p>';
        return;
      }

      // Fixed GitHub-style dimensions: ~750 × 160 px
      const CHART_W  = 750;
      const CHART_H  = 160;
      const NUM_WEEKS = 53;   // columns (weeks); most years need 52-53
      const NUM_ROWS  = 7;    // rows (Sun–Sat)
      const GAP_PX    = 3;    // pixel gap between cells
      // margin: top for month labels, bottom for legend, left for day labels
      const MARGIN    = { t: 22, b: 44, l: 34, r: 8 };

      // plot area in px — used to convert px offsets to paper-coord fractions
      const plotW = CHART_W - MARGIN.l - MARGIN.r;  // 708 px
      const plotH = CHART_H - MARGIN.t - MARGIN.b;  // 94 px
      // cell slots: ~13.4 × 13.4 px → nearly square

      const { z, text, monthTickVals, monthTickText } = buildYearGrid(year, heatmap);

      // Quantize counts to 4 discrete levels (matches GitHub's 4-shade palette)
      const allCounts = z.flat().filter(v => v !== null && v > 0);
      const maxCount  = allCounts.length ? Math.max(...allCounts) : 1;
      const qz = z.map(row => row.map(v => {
        if (v === null) return null;
        if (v === 0)    return 0;
        const r = v / maxCount;
        if (r < 0.15)  return 1;
        if (r < 0.40)  return 2;
        if (r < 0.70)  return 3;
        return 4;
      }));

      const PALETTE = ['#ebedf0', '#9be9a8', '#40c463', '#30a14e', '#216e39'];
      const colorscale = [
        [0,    PALETTE[0]],
        [0.25, PALETTE[1]],
        [0.5,  PALETTE[2]],
        [0.75, PALETTE[3]],
        [1.0,  PALETTE[4]],
      ];

      // ---- "Less □□□□□ More" legend in bottom-right margin ----
      // Place squares centred 22 px below the plot bottom edge.
      // Paper y=0 is the plot bottom; 1 paper unit = plotH px.
      const SQ_PX   = 11;                        // legend square size
      const LEG_GAP = 3;                         // gap between squares
      const sqW = SQ_PX  / plotW;               // paper-x fraction
      const sqH = SQ_PX  / plotH;               // paper-y fraction
      const gW  = LEG_GAP / plotW;
      const legY = -(22 / plotH);               // centre: 22 px below plot bottom
      const legX0 = (plotW - (5*SQ_PX + 4*LEG_GAP + 2*28)) / plotW; // right-aligned with "More"

      const shapes = PALETTE.map((color, i) => ({
        type: 'rect', xref: 'paper', yref: 'paper',
        x0: legX0 + 28/plotW + i*(sqW + gW),
        x1: legX0 + 28/plotW + i*(sqW + gW) + sqW,
        y0: legY - sqH/2,  y1: legY + sqH/2,
        fillcolor: color, line: { width: 0 },
      }));

      const legEnd = legX0 + 28/plotW + 5*(sqW + gW) - gW;
      const annotations = [
        { xref:'paper', yref:'paper',
          x: legX0 + 24/plotW, y: legY,
          text: 'Less', showarrow: false,
          xanchor: 'right', yanchor: 'middle',
          font: { size: 10, color: '#666' } },
        { xref:'paper', yref:'paper',
          x: legEnd + 4/plotW, y: legY,
          text: 'More', showarrow: false,
          xanchor: 'left', yanchor: 'middle',
          font: { size: 10, color: '#666' } },
      ];

      Plotly.react('heatmap', [{
        type: 'heatmap',
        x: Array.from({length: NUM_WEEKS}, (_, i) => i),
        y: Array.from({length: NUM_ROWS},  (_, i) => i),
        z: qz,
        text: text,
        hoverinfo: 'text',
        xgap: GAP_PX,
        ygap: GAP_PX,
        colorscale: colorscale,
        zmin: 0, zmax: 4,
        showscale: false,
      }], {
        width: CHART_W, height: CHART_H,
        xaxis: {
          tickvals: monthTickVals,
          ticktext: monthTickText,
          side: 'top',
          showgrid: false, zeroline: false,
          range: [-0.5, NUM_WEEKS - 0.5],
          fixedrange: true,
          tickfont: { size: 11 },
        },
        yaxis: {
          autorange: 'reversed',          // Sun(0) at top, Sat(6) at bottom
          tickvals: [1, 3, 5],            // Mon, Wed, Fri
          ticktext: ['Mon', 'Wed', 'Fri'],
          showgrid: false, zeroline: false,
          range: [-0.5, NUM_ROWS - 0.5],
          fixedrange: true,
          tickfont: { size: 10 },
        },
        shapes: shapes,
        annotations: annotations,
        margin: MARGIN,
        plot_bgcolor: '#fff',
        paper_bgcolor: '#fff',
      }, { responsive: false, displayModeBar: false });
    } catch (err) { chartError('heatmap', err); }
  }

  // ---------------------------------------------------------------------------
  // sunburst: year > domain > tag, sized by article count, click to drill down
  // ---------------------------------------------------------------------------
  function renderSunburst(articles) {
    try {
      // Build tree: year -> domain -> tag -> count
      // Uses addedDate year; articles with multiple tags appear in each tag's slice.
      const tree = {};
      for (const a of articles) {
        const year = (a.addedDate || '').slice(0, 4);
        if (!year) continue;
        const domain = a.domain || '(unknown)';
        const tags   = (a.tags && a.tags.length) ? a.tags : ['(untagged)'];
        if (!tree[year])          tree[year] = {};
        if (!tree[year][domain])  tree[year][domain] = {};
        for (const tag of tags) {
          tree[year][domain][tag] = (tree[year][domain][tag] || 0) + 1;
        }
      }

      // Cap to top-N domains per year; roll the rest into "(other)"
      const TOP_DOMAINS = 20;

      const ids = [], labels = [], parents = [], values = [];

      for (const year of Object.keys(tree).sort()) {
        const domains = tree[year];

        // Rank domains by total tag-weighted article count for this year
        const ranked = Object.entries(domains)
          .map(([d, tags]) => [d, Object.values(tags).reduce((s, v) => s + v, 0)])
          .sort((a, b) => b[1] - a[1]);

        let yearTotal = 0;
        let otherTotal = 0;

        ranked.forEach(([domain, domTotal], i) => {
          if (i >= TOP_DOMAINS) { otherTotal += domTotal; return; }

          const domId = `${year}||${domain}`;
          let tagSum = 0;
          for (const [tag, count] of Object.entries(domains[domain])) {
            ids.push(`${domId}||${tag}`);
            labels.push(tag);
            parents.push(domId);
            values.push(count);
            tagSum += count;
          }
          ids.push(domId);
          labels.push(domain);
          parents.push(year);
          values.push(tagSum);
          yearTotal += tagSum;
        });

        if (otherTotal > 0) {
          ids.push(`${year}||(other)`);
          labels.push('(other)');
          parents.push(year);
          values.push(otherTotal);
          yearTotal += otherTotal;
        }

        ids.push(year);
        labels.push(year);
        parents.push('');
        values.push(yearTotal);
      }

      Plotly.newPlot('sunburst', [{
        type: 'sunburst',
        ids, labels, parents, values,
        branchvalues: 'total',
        maxdepth: 2,          // show year + domain rings on load; tags appear on drill-down
        insidetextorientation: 'radial',
        hovertemplate: '<b>%{label}</b><br>%{value} articles<extra></extra>',
        leaf: { opacity: 0.85 },
      }], {
        height: 580,
        margin: { t: 10, b: 10, l: 10, r: 10 },
        paper_bgcolor: '#fff',
      }, { responsive: true, displayModeBar: false });
    } catch (err) { chartError('sunburst', err); }
  }

  // ---------------------------------------------------------------------------
  // resizable column helper
  // ---------------------------------------------------------------------------
  function makeColumnsResizable(table) {
    table.querySelectorAll('thead th').forEach(th => {
      const handle = document.createElement('span');
      handle.className = 'col-resize-handle';
      th.appendChild(handle);

      // stop clicks on the handle from triggering column sort
      handle.addEventListener('click', e => e.stopPropagation());

      handle.addEventListener('mousedown', e => {
        e.preventDefault();
        e.stopPropagation();
        const startX = e.clientX;
        const startW = th.getBoundingClientRect().width;
        document.body.style.cursor = 'col-resize';

        const onMove = e => {
          th.style.width = Math.max(36, startW + e.clientX - startX) + 'px';
        };
        const onUp = () => {
          document.body.style.cursor = '';
          document.removeEventListener('mousemove', onMove);
          document.removeEventListener('mouseup', onUp);
        };
        document.addEventListener('mousemove', onMove);
        document.addEventListener('mouseup', onUp);
      });
    });
  }

  // ---------------------------------------------------------------------------
  // sortable / filterable article table
  // ---------------------------------------------------------------------------
  function renderTable(articles) {
    const tbody = document.querySelector('#articles-table tbody');
    let sortCol = 'addedDate';
    let sortDir = -1; // -1 = desc, 1 = asc
    let filterText = '';

    function row(a) {
      const tags = (a.tags || []).map(t => `<span class="tag-pill">${escHtml(t)}</span>`).join('');
      return `<tr>
        <td>${a.addedDate || ''}</td>
        <td>${a.readDate  || ''}</td>
        <td><a href="${escAttr(a.url)}" target="_blank" rel="noopener">${escHtml(a.title)}</a></td>
        <td>${escHtml(a.domain)}</td>
        <td>${tags}</td>
      </tr>`;
    }

    function escHtml(s) {
      return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    }
    function escAttr(s) {
      return String(s ?? '').replace(/"/g,'&quot;');
    }

    function render() {
      const lower = filterText.toLowerCase();
      let filtered = articles;
      if (lower) {
        filtered = articles.filter(a =>
          (a.title || '').toLowerCase().includes(lower) ||
          (a.domain || '').toLowerCase().includes(lower) ||
          (a.tags || []).some(t => t.toLowerCase().includes(lower))
        );
      }
      const sorted = [...filtered].sort((a, b) => {
        const av = a[sortCol] ?? '';
        const bv = b[sortCol] ?? '';
        if (sortCol === 'tags') {
          const as = (a.tags || []).join(',');
          const bs = (b.tags || []).join(',');
          return sortDir * as.localeCompare(bs);
        }
        return sortDir * av.localeCompare(bv);
      });
      tbody.innerHTML = sorted.map(row).join('');
      document.getElementById('table-count').textContent =
        lower ? `${sorted.length} of ${articles.length}` : `${articles.length} articles`;
    }

    // header sort
    document.querySelectorAll('#articles-table th').forEach(th => {
      th.addEventListener('click', () => {
        const col = th.dataset.col;
        if (sortCol === col) {
          sortDir *= -1;
        } else {
          sortCol = col;
          sortDir = 1;
        }
        document.querySelectorAll('#articles-table th').forEach(h => h.classList.remove('sort-asc','sort-desc'));
        th.classList.add(sortDir === 1 ? 'sort-asc' : 'sort-desc');
        render();
      });
    });

    document.getElementById('table-filter').addEventListener('input', e => {
      filterText = e.target.value;
      render();
    });

    makeColumnsResizable(document.getElementById('articles-table'));
    render();
  }

  // ---------------------------------------------------------------------------
  // main
  // ---------------------------------------------------------------------------
  (async () => {
    try {
      const data = await loadData();
      const { articles, heatmap, tag_series, domain_series } = data;

      const readCount = articles.filter(a => a.readDate).length;
      document.getElementById('summary-line').textContent =
        `${articles.length} articles — ${readCount} read — ${Object.keys(tag_series).length} tags`;

      // populate year dropdown (reverse-chron, default = current/most recent year)
      const heatmapYears = [...new Set(Object.keys(heatmap).map(d => d.slice(0,4)))]
        .sort().reverse();
      const sel = document.getElementById('heatmap-year');
      heatmapYears.forEach(y => {
        const opt = document.createElement('option');
        opt.value = y;
        opt.textContent = y;
        sel.appendChild(opt);
      });
      const defaultYear = parseInt(heatmapYears[0] || new Date().getFullYear());
      sel.value = String(defaultYear);
      sel.addEventListener('change', () => renderHeatmap(heatmap, parseInt(sel.value)));

      renderHeatmap(heatmap, defaultYear);
      renderSunburst(articles);
      renderTable(articles);
    } catch (err) {
      const line = document.getElementById('summary-line');
      line.textContent = `error loading data: ${err.message}`;
      line.style.color = '#c00';
      console.error(err);
    }
  })();
  </script>
</body>
</html>
"""


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
    html_path.write_text(HTML_TEMPLATE)
    print(f"  wrote {html_path}", file=sys.stderr)

    print("done.", file=sys.stderr)


if __name__ == "__main__":
    main()
