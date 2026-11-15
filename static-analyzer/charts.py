#!/usr/bin/env python3
"""Render the report's charts as standalone SVG files (standard library only).

Reads the outputs of static-analyzer + summarize.py and the sample manifest, writes into --out:

  plugins-by-bucket.svg       share of each sample bucket referencing deprecated API, split by who makes the reference
  entries-attribution.svg     the referenced deprecated entries, plugins per entry split the same way
  xseries-share.svg           per entry XSeries touches: plugins reaching it only through bundled XSeries vs the rest
  xseries-plugins.svg         every sampled plugin bundling XSeries: downloads, version, entries (needs xseries_scan.py)
  libraries.svg               bundled libraries whose code references the entries, plugins per library
  xseries-dependence.svg      the plugins bundling XSeries split by how they depend on it (needs xseries_scan.py)
  unused-deprecations.svg     every deprecated entry no sampled plugin references, by package (inlined constants excluded)
  sample-jar-age.svg          jar age distribution per bucket
  sample-minecraft-version.svg newest Minecraft version each jar is marked for, per bucket

Charts follow the light/dark color scheme of whatever displays them, and every mark carries a <title> tooltip.
Colors: categorical slots 1-3 and an ordinal blue ramp, validated for CVD separation and contrast in both modes.
"""

from __future__ import annotations

import argparse
import csv
import html
import re
from pathlib import Path

FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'
BUCKETS = [("popular", "Popular"), ("legacy", "Legacy"), ("random-tail", "Random tail")]

# who makes the reference: categorical slots 1-3 (light / dark steps)
ATTRIBUTION = [
    ("code", "Plugin's own code", "#2a78d6", "#3987e5"),
    ("library", "Only via a bundled library", "#eb6834", "#d95926"),
    ("version", "Only via a version-specific module", "#1baf7a", "#199e70"),
]
# ordinal ramp, newest/lightest first (light mode) - dark mode runs the other way so "older" stays the stronger mark
ORDINAL_LIGHT = ["#86b6ef", "#3987e5", "#1c5cab", "#0d366b"]
ORDINAL_DARK = ["#184f95", "#256abf", "#5598e7", "#9ec5f4"]

STYLE = """
  .surface { fill: #fcfcfb; }
  .title { fill: #0b0b0b; font-size: 15px; font-weight: 600; }
  .subtitle, .legend, .label { fill: #52514e; font-size: 12px; }
  .tick { fill: #898781; font-size: 11px; font-variant-numeric: tabular-nums; }
  .value { fill: #0b0b0b; font-size: 11px; font-variant-numeric: tabular-nums; }
  .grid { stroke: #e1e0d9; stroke-width: 1; }
  .axis { stroke: #c3c2b7; stroke-width: 1; }
  .ink-on-fill-light { fill: #ffffff; font-size: 11px; }
  .ink-on-fill-dark { fill: #0b0b0b; font-size: 11px; }
  %(light)s
  @media (prefers-color-scheme: dark) {
    .surface { fill: #1a1a19; }
    .title { fill: #f0efec; }
    .subtitle, .legend, .label { fill: #c3c2b7; }
    .value { fill: #f0efec; }
    .grid { stroke: #2c2c2a; }
    .axis { stroke: #383835; }
    %(dark)s
  }
"""


def esc(text: str) -> str:
    return html.escape(str(text), quote=True)


def text_width(text: str, size: float = 12) -> float:
    """Rough width of system-ui text, good enough to lay out labels without clipping."""
    return sum(0.33 if c in "il.,:;|!'()[]" else 0.62 if c.isupper() or c in "mwMW#" else 0.54 for c in text) * size


class Svg:
    def __init__(self, width: int, height: int, title: str, fills: list[tuple[str, str, str]]):
        """fills: (class name, light hex, dark hex) for every series/step color used."""
        self.width, self.height = width, height
        self.parts: list[str] = []
        light = "\n  ".join(f".{c} {{ fill: {lt}; }}" for c, lt, _ in fills)
        dark = "\n    ".join(f".{c} {{ fill: {dk}; }}" for c, _, dk in fills)
        self.head = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
                     f'viewBox="0 0 {width} {height}" role="img" aria-label="{esc(title)}" font-family=\'{FONT}\'>\n'
                     f"<title>{esc(title)}</title>\n<style>{STYLE % {'light': light, 'dark': dark}}</style>\n"
                     f'<rect class="surface" width="{width}" height="{height}" rx="8"/>\n')

    def add(self, fragment: str) -> None:
        self.parts.append(fragment)

    def text(self, x, y, s, cls, anchor="start", baseline="middle"):
        self.add(f'<text x="{x:.1f}" y="{y:.1f}" class="{cls}" text-anchor="{anchor}" '
                 f'dominant-baseline="{baseline}">{esc(s)}</text>')

    def write(self, path: Path) -> None:
        path.write_text(self.head + "\n".join(self.parts) + "\n</svg>\n")


def bar_segment(x: float, y: float, w: float, h: float, cls: str, tooltip: str, round_end: bool) -> str:
    """A horizontal bar piece; the data end (right) is rounded 4px when it ends the bar, the baseline stays square."""
    w = max(w, 1.0)
    r = min(4.0, w, h / 2) if round_end else 0
    if r:
        d = (f"M{x:.1f},{y:.1f} H{x + w - r:.1f} Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} "
             f"V{y + h - r:.1f} Q{x + w:.1f},{y + h:.1f} {x + w - r:.1f},{y + h:.1f} H{x:.1f} Z")
    else:
        d = f"M{x:.1f},{y:.1f} H{x + w:.1f} V{y + h:.1f} H{x:.1f} Z"
    return f'<path d="{d}" class="{cls}"><title>{esc(tooltip)}</title></path>'


def legend(svg: Svg, x: float, y: float, items: list[tuple[str, str]]) -> None:
    for cls, name in items:
        svg.add(f'<rect x="{x:.1f}" y="{y - 5:.1f}" width="10" height="10" rx="2" class="{cls}"/>')
        svg.text(x + 15, y, name, "legend")
        x += 15 + text_width(name) + 22


def stacked_rows(svg: Svg, rows, x0: float, top: float, plot_w: float, row_h: float, bar_h: float,
                 max_value: float, ticks: list[float], tick_fmt, tip_fmt, inside_labels=False, label_ink=None):
    """rows: (label, [(class, value, tooltip), ...], end_label). Draws grid, bars, row labels and tip labels."""
    scale = plot_w / max_value
    bottom = top + row_h * len(rows)
    for t in ticks:
        x = x0 + t * scale
        svg.add(f'<line x1="{x:.1f}" y1="{top - 4:.1f}" x2="{x:.1f}" y2="{bottom:.1f}" class="grid"/>')
        svg.text(x, bottom + 14, tick_fmt(t), "tick", anchor="middle")
    svg.add(f'<line x1="{x0:.1f}" y1="{top - 4:.1f}" x2="{x0:.1f}" y2="{bottom:.1f}" class="axis"/>')
    for i, (label, segments, end_label) in enumerate(rows):
        y = top + i * row_h + (row_h - bar_h) / 2
        svg.text(x0 - 10, y + bar_h / 2, label, "label", anchor="end")
        x = x0
        nonzero = [s for s in segments if s[1] > 0]
        for j, (cls, value, tip) in enumerate(nonzero):
            w = value * scale
            gap = 2 if j > 0 else 0  # surface gap between touching segments
            svg.add(bar_segment(x + gap, y, w - gap, bar_h, cls, tip, round_end=(j == len(nonzero) - 1)))
            if inside_labels:
                pct = tip_fmt(value)
                if text_width(pct, 11) + 10 < w - gap:
                    svg.text(x + gap + 5, y + bar_h / 2, pct, label_ink(cls))
            x += w
        if end_label:
            svg.text(x + 6, y + bar_h / 2, end_label, "value")


# ------------------------------------------------------------------------------------------------------------- charts


def chart_plugins_by_bucket(manifest, plugins, out: Path) -> None:
    sizes = {b: sum(b in r["buckets"].split("+") for r in manifest.values()) for b, _ in BUCKETS}
    counts = {b: {"code": 0, "library": 0, "version": 0} for b, _ in BUCKETS}
    for p in plugins:
        kind = ("code" if int(p["entries_plugin-code"]) else
                "library" if int(p["entries_bundled-library"]) else "version")
        for b, _ in BUCKETS:
            if b in p["buckets"].split("+"):
                counts[b][kind] += 1
    rows = []
    for b, name in BUCKETS:
        n = sizes[b]
        segs = [(cls, 100 * counts[b][cls] / n, f"{name}: {counts[b][cls]} of {n} plugins ({100 * counts[b][cls] / n:.1f}%) "
                 f"- {label.lower()}") for cls, label, *_ in ATTRIBUTION]
        total = sum(counts[b].values())
        rows.append((f"{name} (n={n})", segs, f"{100 * total / n:.1f}%  ({total})"))

    title = "Plugins referencing the 136 oldest deprecated APIs"
    svg = Svg(760, 250, title, [(c, lt, dk) for c, _, lt, dk in ATTRIBUTION])
    svg.text(24, 30, title, "title")
    svg.text(24, 50, "Share of each sample bucket; a plugin counts once, under its most direct kind of reference",
             "subtitle")
    legend(svg, 24, 76, [(c, n) for c, n, *_ in ATTRIBUTION])
    stacked_rows(svg, rows, x0=170, top=100, plot_w=470, row_h=40, bar_h=20, max_value=20,
                 ticks=[0, 5, 10, 15, 20], tick_fmt=lambda t: f"{t:.0f}%", tip_fmt=None)
    svg.write(out / "plugins-by-bucket.svg")


def short_entry(row) -> str:
    kind, path, owner = row["type"], row["path"], row["owner"]
    path = re.sub(r"\b(?:[a-z_][\w]*\.)+([A-Z][\w]*)", r"\1", path)
    if kind == "TYPE":
        return f"{path.rsplit('.', 1)[-1]} (type)"
    if kind == "CONSTRUCTOR":
        return f"new {path}"
    return f"{owner.rsplit('.', 1)[-1]}#{path}"


def chart_entries(usage, out: Path) -> None:
    used = [r for r in usage if int(r["plugins_any"]) > 0]
    used.sort(key=lambda r: (-int(r["plugins_plugin_code"]), -int(r["plugins_any"])))
    rows = []
    for r in used:
        name = short_entry(r)
        values = {"code": int(r["plugins_plugin_code"]), "library": int(r["plugins_only_library"]),
                  "version": int(r["plugins_only_version_specific"])}
        segs = [(cls, values[cls], f"{name}: {values[cls]} plugins - {label.lower()}") for cls, label, *_ in ATTRIBUTION]
        rows.append((name, segs, str(int(r["plugins_any"]))))
    label_w = max(text_width(r[0]) for r in rows) + 34
    row_h, top = 24, 100
    max_value = 60
    width = int(label_w + 470 + 70)
    height = int(top + row_h * len(rows) + 40)
    title = f"The {len(rows)} deprecated entries referenced by any of 958 plugins"
    svg = Svg(width, height, title, [(c, lt, dk) for c, _, lt, dk in ATTRIBUTION])
    svg.text(24, 30, title, "title")
    svg.text(24, 50, "Plugins per entry, split by who makes the reference; the other 107 entries are referenced by none",
             "subtitle")
    legend(svg, 24, 76, [(c, n) for c, n, *_ in ATTRIBUTION])
    stacked_rows(svg, rows, x0=label_w, top=top, plot_w=470, row_h=row_h, bar_h=12, max_value=max_value,
                 ticks=[0, 10, 20, 30, 40, 50, 60], tick_fmt=lambda t: f"{t:.0f}", tip_fmt=None)
    svg.write(out / "entries-attribution.svg")


def chart_xseries(usage, plugins, path: Path) -> None:
    """Emphasis chart: for each entry XSeries touches, the plugins reaching it only through XSeries vs everyone else."""
    rows_data = [r for r in usage if int(r["plugins_via_xseries"]) > 0]
    rows_data.sort(key=lambda r: (-int(r["plugins_only_xseries"]), -int(r["plugins_any"])))
    rows = []
    for r in rows_data:
        name = short_entry(r)
        only, total = int(r["plugins_only_xseries"]), int(r["plugins_any"])
        segs = [("xseries", only, f"{name}: {only} of {total} plugins reference it only through bundled XSeries"),
                ("other", total - only, f"{name}: {total - only} of {total} plugins reference it some other way")]
        rows.append((name, segs, f"{only} of {total}"))
    any_plugins = len(plugins)
    only_plugins = sum(p["only_xseries"] == "True" for p in plugins)
    label_w = max(text_width(r[0]) for r in rows) + 34
    row_h, top = 26, 100
    width = int(label_w + 470 + 90)
    height = int(top + row_h * len(rows) + 40)
    title = "How much of the deprecated-API usage comes from bundled XSeries"
    fills = [("xseries", "#eb6834", "#d95926"), ("other", "#c3c2b7", "#52514e")]
    svg = Svg(width, height, title, fills)
    svg.text(24, 30, title, "title")
    svg.text(24, 50, f"{only_plugins} of the {any_plugins} plugins referencing any entry do so only through XSeries; "
                     f"entries XSeries touches are shown", "subtitle")
    legend(svg, 24, 76, [("xseries", "Plugins referencing it only through XSeries"),
                         ("other", "All other plugins referencing it")])
    stacked_rows(svg, rows, x0=label_w, top=top, plot_w=470, row_h=row_h, bar_h=12, max_value=60,
                 ticks=[0, 10, 20, 30, 40, 50, 60], tick_fmt=lambda t: f"{t:.0f}", tip_fmt=None)
    svg.write(path)


def chart_distribution(manifest, classify, bands: list[str], title: str, subtitle: str, path: Path) -> None:
    classes = [f"step{i}" for i in range(len(bands))]
    fills = [(c, ORDINAL_LIGHT[i], ORDINAL_DARK[i]) for i, c in enumerate(classes)]
    # white text on the two strongest steps, near-black on the two lightest (per mode)
    ink_light = {c: ("ink-on-fill-dark" if i == 0 else "ink-on-fill-light") for i, c in enumerate(classes)}
    rows = []
    for b, name in BUCKETS:
        rs = [r for r in manifest.values() if b in r["buckets"].split("+")]
        n = len(rs)
        counts = [sum(classify(r) == band for r in rs) for band in bands]
        segs = [(classes[i], 100 * counts[i] / n, f"{name}: {counts[i]} of {n} jars ({100 * counts[i] / n:.0f}%) - {bands[i]}")
                for i in range(len(bands))]
        rows.append((f"{name} (n={n})", segs, ""))
    svg = Svg(760, 250, title, fills)
    # inside labels must read in both modes: use a style that flips with the step's mode-specific lightness
    svg.add("<style>@media (prefers-color-scheme: dark) { .flip-ink { fill: #0b0b0b; } .flip-ink-0 { fill: #ffffff; } }"
            "</style>")
    svg.text(24, 30, title, "title")
    svg.text(24, 50, subtitle, "subtitle")
    legend(svg, 24, 76, list(zip(classes, bands)))

    def label_ink(cls: str) -> str:
        i = classes.index(cls)
        # light mode: step0 is light -> dark ink; dark mode: the ramp is reversed, so the last steps are light
        if i == 0:
            return "ink-on-fill-dark flip-ink-0"
        if i >= len(bands) - 2:
            return "ink-on-fill-light flip-ink"
        return "ink-on-fill-light"

    stacked_rows(svg, rows, x0=170, top=100, plot_w=520, row_h=40, bar_h=24, max_value=100,
                 ticks=[0, 25, 50, 75, 100], tick_fmt=lambda t: f"{t:.0f}%", tip_fmt=lambda v: f"{v:.0f}%",
                 inside_labels=True, label_ink=label_ink)
    svg.write(path)


def age_band(r) -> str:
    years = int(r["jar_age_days"]) / 365.25
    return "< 1 year" if years < 1 else "1–2 years" if years < 2 else "2–3 years" if years < 3 else "3+ years"


def mc_band(r) -> str:
    v = r["newest_game_version"]
    parts = v.split(".")
    if parts[0] == "26":
        return "26.x"
    if parts[0] == "1" and len(parts) > 1 and parts[1].isdigit():
        minor = int(parts[1])
        return "1.21.x" if minor == 21 else "1.20.x" if minor == 20 else "1.19.x or older"
    return "1.19.x or older"  # one jar lists no parseable version; grouped with the oldest band


def chart_libraries(libraries: list[dict], own_code_plugins: int, path: Path) -> None:
    """Emphasis chart: each bundled library whose copy references a deprecated entry, plugins split into those reaching
    the API only through it vs those that also reach it some other way."""
    named = [r for r in libraries if not r["library"].startswith("Other bundled code")]
    named.sort(key=lambda r: (-int(r["plugins"]), -int(r["plugins_only_this_library"]), r["library"]))
    rows = []
    for r in named:
        total, only, entries = int(r["plugins"]), int(r["plugins_only_this_library"]), int(r["entries"])
        name = r["library"]
        segs = [("only", only, f"{name}: for {only} of {total} plugins, this library is their only source of "
                               f"deprecated references"),
                ("other", total - only, f"{name}: {total - only} of {total} plugins also have deprecated references "
                                        f"outside this library (their own code or another library)")]
        rows.append((name, segs, f"{total} {'plugin' if total == 1 else 'plugins'} · "
                                 f"{entries} {'entry' if entries == 1 else 'entries'}"))
    label_w = max(text_width(r[0]) for r in rows) + 34
    row_h, top = 28, 122
    plot_w, max_value = 420, 25
    width = int(label_w + plot_w + 150)
    height = int(top + row_h * len(rows) + 90)
    title = "Bundled libraries whose code references the deprecated entries"
    fills = [("only", "#eb6834", "#d95926"), ("other", "#c3c2b7", "#52514e")]
    svg = Svg(width, height, title, fills)
    svg.text(24, 30, title, "title")
    svg.text(24, 50, "Bar length: plugins bundling a copy of the library that references at least one entry", "subtitle")
    # one legend item per line: the labels are long enough that a single row would crowd the chart
    for i, (cls, label) in enumerate([
            ("only", "This library is the plugin's only source of deprecated references"),
            ("other", "The plugin also has deprecated references outside this library (its own code or another library)")]):
        y = 76 + i * 18
        svg.add(f'<rect x="24" y="{y - 5}" width="10" height="10" rx="2" class="{cls}"/>')
        svg.text(39, y, label, "legend")
    stacked_rows(svg, rows, x0=label_w, top=top, plot_w=plot_w, row_h=row_h, bar_h=14, max_value=max_value,
                 ticks=[0, 5, 10, 15, 20, 25], tick_fmt=lambda t: f"{t:.0f}", tip_fmt=None)
    svg.text(24, top + row_h * len(rows) + 40,
             "Libraries are relocated under each plugin's package and matched back to their library by name.", "tick")
    svg.text(24, top + row_h * len(rows) + 56,
             "Entries: how many of the 136 deprecated entries the library's code references.", "tick")
    svg.text(24, top + row_h * len(rows) + 72,
             f"For comparison, {own_code_plugins} plugins reference the entries from their own code.", "tick")
    svg.write(path)


# How a plugin bundling XSeries relates to the deprecated entries; shared by both XSeries plugin charts.
XSERIES_KINDS = [
    ("only", "Reaches the deprecated API only through XSeries", "#eb6834", "#d95926"),
    ("also", "Through XSeries and other code (its own or another library)", "#6250d6", "#9085e9"),
    ("none", "Bundles XSeries, but its copy references none of the entries", "#898781", "#898781"),
]


def xseries_kind(r: dict) -> str:
    if r["xseries_references_deprecated"] != "True":
        return "none"
    return "only" if r["only_via_xseries"] == "True" else "also"


def chart_xseries_summary(rows: list[dict], sample_size: int, path: Path) -> None:
    """Tall segmented bar: the plugins bundling XSeries, split by how they depend on it (one whole, three parts)."""
    total = len(rows)
    counts = {cls: sum(xseries_kind(r) == cls for r in rows) for cls, *_ in XSERIES_KINDS}
    width, x0, bar_w, bar_h, bar_y = 760, 24, 712, 28, 92
    title = f"How the {total} sampled plugins that bundle XSeries depend on it"
    svg = Svg(width, 200, title, [(c, lt, dk) for c, _, lt, dk in XSERIES_KINDS])
    svg.text(24, 30, title, "title")
    svg.text(24, 50, f"{total} of {sample_size} plugins in the sample ({100 * total / sample_size:.1f}%) bundle XSeries; "
                     f"the same split as the plugin-by-plugin chart", "subtitle")
    gap = 2
    x = x0
    parts = [(cls, counts[cls], label) for cls, label, *_ in XSERIES_KINDS if counts[cls] > 0]
    available = bar_w - gap * (len(parts) - 1)
    for cls, n, label in parts:
        w = available * n / total
        share = f"{100 * n / total:.0f}%"
        tip = f"{label}: {n} of {total} plugins ({share})"
        svg.add(f'<rect x="{x:.1f}" y="{bar_y}" width="{w:.1f}" height="{bar_h}" rx="4" class="{cls}">'
                f'<title>{esc(tip)}</title></rect>')
        text = f"{n} plugins · {share}"
        if text_width(text, 12) + 16 < w:
            svg.add(f'<text x="{x + 8:.1f}" y="{bar_y + bar_h / 2:.1f}" class="ink-on-fill-light" '
                    f'style="font-size: 12px; font-weight: 600" dominant-baseline="middle">{esc(text)}</text>')
        x += w + gap
    # legend: names only, in block order, one per line
    for i, (cls, _, label) in enumerate(parts):
        y = bar_y + bar_h + 26 + i * 18
        svg.add(f'<rect x="24" y="{y - 5}" width="10" height="10" rx="2" class="{cls}"/>')
        svg.text(39, y, label, "legend")
    svg.write(path)


def chart_xseries_plugins(rows: list[dict], sample_size: int, path: Path) -> None:
    """Table-style dot plot: every sampled plugin bundling XSeries, downloads on a log axis, colored by how it reaches
    the deprecated API, with the details as text columns."""
    import math

    kinds = XSERIES_KINDS
    kind = xseries_kind

    rows = sorted(rows, key=lambda r: -int(r["downloads"] or 0))
    name_w = max(text_width(r["name"]) for r in rows) + 16
    x_names = 24
    x0 = x_names + name_w + 12          # plot start
    plot_w = 380
    log_min, log_max = 0, 7              # 1 .. 10M downloads
    cols = [("Downloads", 78, "end"), ("Source", 74, "start"), ("Bucket", 112, "start"),
            ("XSeries", 70, "start"), ("Entries", 64, "end")]
    col_x = []
    x = x0 + plot_w + 24
    for title, w, anchor in cols:
        col_x.append((x + w if anchor == "end" else x, anchor))
        x += w + 14
    width = int(x + 10)
    row_h, top = 24, 156
    height = int(top + row_h * len(rows) + 46)

    title = f"The {len(rows)} sampled plugins that bundle XSeries"
    svg = Svg(width, height, title, [(c, lt, dk) for c, _, lt, dk in kinds])
    svg.add("<style>.ring { stroke: #fcfcfb; stroke-width: 2; } .colhead { fill: #898781; font-size: 11px; }"
            "@media (prefers-color-scheme: dark) { .ring { stroke: #1a1a19; } }</style>")
    svg.text(24, 30, title, "title")
    svg.text(24, 50, f"{len(rows)} of {sample_size} plugins in the sample; downloads as reported by each source "
                     f"(Modrinth and Hangar counts aren't directly comparable)", "subtitle")
    # legend with round swatches, one per line so the long labels stay readable
    for i, (cls, label, *_) in enumerate(kinds):
        y = 74 + i * 18
        svg.add(f'<circle cx="29" cy="{y}" r="5" class="{cls}"/>')
        svg.text(40, y, label, "legend")

    head_y = top - 12
    svg.text(x_names, head_y, "Plugin", "colhead")
    svg.text(x0, head_y, "Downloads (log scale)", "colhead")
    for (label, _, _), (cx, anchor) in zip(cols, col_x):
        svg.text(cx, head_y, label if label != "Entries" else "Entries*", "colhead", anchor=anchor)

    def xpos(downloads: int) -> float:
        value = math.log10(max(downloads, 1))
        return x0 + (value - log_min) / (log_max - log_min) * plot_w

    bottom = top + row_h * len(rows)
    for t in range(log_min, log_max + 1):
        gx = x0 + (t - log_min) / (log_max - log_min) * plot_w
        svg.add(f'<line x1="{gx:.1f}" y1="{top:.1f}" x2="{gx:.1f}" y2="{bottom:.1f}" class="grid"/>')
        label = ["1", "10", "100", "1k", "10k", "100k", "1M", "10M"][t]
        svg.text(gx, bottom + 14, label, "tick", anchor="middle")

    for i, r in enumerate(rows):
        cy = top + i * row_h + row_h / 2
        downloads = int(r["downloads"] or 0)
        k = kind(r)
        svg.text(x_names, cy, r["name"], "label")
        svg.add(f'<line x1="{x0:.1f}" y1="{cy:.1f}" x2="{xpos(downloads):.1f}" y2="{cy:.1f}" class="grid"/>')
        entries = int(r["entries_via_xseries"] or 0)
        tip = (f"{r['name']}: {downloads:,} downloads on {r['source'].capitalize()}; "
               + {"only": f"its only deprecated references are in XSeries ({entries} entries)",
                  "also": f"XSeries references {entries} entries, and other code references some too",
                  "none": "its XSeries copy references none of the entries"}[k])
        svg.add(f'<circle cx="{xpos(downloads):.1f}" cy="{cy:.1f}" r="5" class="{k} ring"><title>{esc(tip)}</title>'
                f'</circle>')
        values = [f"{downloads:,}", r["source"].capitalize(), r["buckets"].replace("+", ", "),
                  r["xseries_version"] or "unknown", str(entries) if entries else "–"]
        for value, (cx, anchor) in zip(values, col_x):
            svg.text(cx, cy, value, "value" if value not in ("unknown", "–") else "tick", anchor=anchor)

    svg.text(24, bottom + 34, "* Entries: how many of the 136 deprecated entries the plugin's XSeries copy references. "
                              "Version \"unknown\": relocation removed XSeries' Maven metadata.", "tick")
    svg.write(path)


def split_owner(qualified: str) -> tuple[str, str]:
    """'org.bukkit.event.player.AsyncPlayerPreLoginEvent.Result' -> ('org.bukkit.event.player',
    'AsyncPlayerPreLoginEvent.Result'): the package ends at the first segment starting with an uppercase letter."""
    parts = qualified.split(".")
    i = next((i for i, p in enumerate(parts) if p[:1].isupper()), len(parts) - 1)
    return ".".join(parts[:i]), ".".join(parts[i:])


def chart_unused(usage: list[dict], deprecation_dates: list[str], path: Path) -> None:
    """Index graphic: every deprecated entry no sampled plugin references, grouped by package, entries in columns.

    Constant fields are left out: javac inlines them, so a static scan can't see their use.
    deprecation_dates: one date per deprecated-data.csv row, in the same order as usage-attributed.csv.
    """
    magic_values = "2013-08-19"
    entries = []
    for i, r in enumerate(usage):
        if int(r["plugins_any"]) > 0 or r["type"] == "FIELD":
            continue
        params = re.sub(r"\b(?:[a-z_][\w]*\.)+([A-Z][\w]*)", r"\1", r["path"])
        if r["type"] == "TYPE":
            package, simple = split_owner(r["path"])
            label = f"{simple} (whole class)"
        else:
            package, simple = split_owner(r["owner"])
            label = f"new {params}" if r["type"] == "CONSTRUCTOR" else f"{simple}#{params}"
            if r["type"] == "CONSTRUCTOR" and "." in simple:  # nested class constructor: keep the outer name
                label = f"new {simple}{params[params.index('('):]}"
        date = deprecation_dates[i] if i < len(deprecation_dates) else ""
        entries.append((package, label, date, f"{r['type'].lower()} {r['owner'] if r['owner'] != 'null' else ''}"
                                              f"#{r['path']}".replace(" #", " ")))

    groups: dict[str, list] = {}
    for e in sorted(entries, key=lambda e: (e[0], e[1].lower())):
        groups.setdefault(e[0], []).append(e)
    order = sorted(groups, key=lambda p: (-len(groups[p]), p))

    max_columns, row_h = 3, 18
    inner_w = 3 * 352
    width = 24 * 2 + inner_w

    def entry_width(e) -> float:
        _, label, date, _ = e
        return text_width(label, 12) + (text_width(date, 11) + 8 if date and date != magic_values else 0) + 24

    title = f"The {len(entries)} deprecated APIs no sampled plugin references"
    y = 100
    layout = []
    for package in order:
        items = groups[package]
        # as many columns as the longest entry (plus its date) allows, so nothing runs into the next column
        columns = max(1, min(max_columns, int(inner_w // max(entry_width(e) for e in items))))
        col_w = inner_w / columns
        rows_n = -(-len(items) // columns)
        layout.append((package, items, y, rows_n, col_w))
        y += 28 + rows_n * row_h + 14
    height = y + 30
    svg = Svg(width, height, title, [])
    svg.add("<style>.section { fill: #0b0b0b; font-size: 13px; font-weight: 600; } .count { fill: #898781; font-size: 12px; }"
            ".entry { fill: #0b0b0b; font-size: 12px; } .date { fill: #898781; font-size: 11px; }"
            ".rule { stroke: #e1e0d9; stroke-width: 1; }"
            "@media (prefers-color-scheme: dark) { .section, .entry { fill: #f0efec; } .rule { stroke: #2c2c2a; } }</style>")
    svg.text(24, 30, title, "title")
    svg.text(24, 50, "Of the 136 oldest deprecated entries, none of the 958 plugins in the sample reference these. "
                     "14 inlined constants are left out,", "subtitle")
    svg.text(24, 68, "since static analysis can't see their use.", "subtitle")
    for package, items, top, rows_n, col_w in layout:
        svg.add(f'<line x1="24" y1="{top - 6}" x2="{width - 24}" y2="{top - 6}" class="rule"/>')
        svg.text(24, top + 8, package, "section")
        svg.text(24 + text_width(package, 13) + 10, top + 8,
                 f"{len(items)} {'entry' if len(items) == 1 else 'entries'}", "count")
        for j, (_, label, date, tip) in enumerate(items):
            col, row = divmod(j, rows_n)  # fill column by column, so the list reads top to bottom
            x = 24 + col * col_w
            ey = top + 28 + row * row_h
            svg.add(f'<text x="{x:.1f}" y="{ey:.1f}" class="entry" dominant-baseline="middle"><title>{esc(tip)}, '
                    f'deprecated {esc(date)}</title>{esc(label)}</text>')
            if date and date != magic_values:
                svg.text(x + text_width(label, 12) + 8, ey, date, "date")
    svg.text(24, height - 14, f"Undated entries were deprecated on {magic_values} in Bukkit's \"Deprecate magic values\" "
                              f"commit; dated entries are older.", "tick")
    svg.write(path)


def deprecation_dates(oldest_md: Path) -> list[str]:
    """Deprecation date per row of oldest-deprecations.md, which lists the entries in deprecated-data.csv's order."""
    dates = []
    for line in oldest_md.read_text().splitlines():
        cells = [c.strip() for c in line.split("|")]
        if len(cells) > 3 and cells[1].isdigit():
            dates.append(cells[2])
    return dates


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", type=Path, default=Path("report/data"))
    parser.add_argument("--manifest", type=Path, default=Path("static-plugin-fetch/sample/manifest.csv"))
    parser.add_argument("--out", type=Path, default=Path("report/figures"))
    args = parser.parse_args()

    manifest = {r["file"]: r for r in csv.DictReader(args.manifest.open())}
    plugins = list(csv.DictReader((args.data / "plugins-attributed.csv").open()))
    usage = list(csv.DictReader((args.data / "usage-attributed.csv").open()))
    args.out.mkdir(parents=True, exist_ok=True)

    chart_plugins_by_bucket(manifest, plugins, args.out)
    chart_entries(usage, args.out)
    chart_xseries(usage, plugins, args.out / "xseries-share.svg")
    chart_libraries(list(csv.DictReader((args.data / "libraries-attributed.csv").open())),
                    sum(int(p["entries_plugin-code"]) > 0 for p in plugins), args.out / "libraries.svg")
    xseries_list = args.data / "xseries-plugins.csv"
    if xseries_list.exists():  # written by xseries_scan.py
        xseries_rows = list(csv.DictReader(xseries_list.open()))
        chart_xseries_plugins(xseries_rows, len(manifest), args.out / "xseries-plugins.svg")
        chart_xseries_summary(xseries_rows, len(manifest), args.out / "xseries-dependence.svg")
    oldest_md = args.data.parent / "oldest-deprecations.md"
    chart_unused(usage, deprecation_dates(oldest_md) if oldest_md.exists() else [], args.out / "unused-deprecations.svg")
    chart_distribution(manifest, age_band, ["< 1 year", "1–2 years", "2–3 years", "3+ years"],
                       "Age of the sampled jars", "Time since each jar was published, at the time of the sample run",
                       args.out / "sample-jar-age.svg")
    chart_distribution(manifest, mc_band, ["26.x", "1.21.x", "1.20.x", "1.19.x or older"],
                       "Newest Minecraft version each sampled jar is marked for",
                       "As listed on Modrinth/Hangar; jars were not required to support the latest version",
                       args.out / "sample-minecraft-version.svg")
    for f in sorted(args.out.glob("*.svg")):
        print(f)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
