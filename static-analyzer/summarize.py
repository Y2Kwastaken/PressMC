#!/usr/bin/env python3
"""Summarize static-analyzer output against the plugin sample, attributing each reference to who makes it.

Every deprecated-API reference comes from some class inside a plugin jar. That class is classified as:

  plugin-code       the plugin's own code
  bundled-library   a third-party library shaded into the jar (e.g. a relocated XSeries, PacketEvents, ACF)
  version-specific  a module for a specific (usually old) Minecraft version, e.g. an `nms.v1_12_R1` package, which
                    typically only runs on servers of that version

The classification is a package-name heuristic (see LIBRARY_NAMES / LIBRARY_SEGMENTS / VERSION_SEGMENT): it can't see
whether a branch is actually reachable on a modern server, so treat the split as an estimate.

Inputs: usage-by-class.csv (static-analyzer output), the sample manifest.csv and deprecated-data.csv.
Outputs (into --out):
  usage-attributed.csv   one row per deprecated entry: plugins using it, split by category and by sample bucket
  plugins-attributed.csv one row per plugin with any usage: its buckets and how many entries it reaches per category
and a Markdown summary on stdout.
"""

from __future__ import annotations

import argparse
import collections
import csv
import re
from pathlib import Path

CATEGORIES = ("plugin-code", "bundled-library", "version-specific")
BUCKETS = ("popular", "legacy", "random-tail")

# Substrings of a fully qualified class name that identify a known shaded library.
LIBRARY_NAMES = ("xseries", "cryptomorin", "xmaterial", "packetevents", "inventorygui", "signgui", "org.mineacademy.fo",
                 "tr7zw", "nbtapi", "triumphteam", "inventoryframework", "fastboard", "co.aikar.commands",
                 "com.saicone.rtag", "de.slikey.effectlib")
# Package segments that conventionally hold relocated dependencies.
LIBRARY_SEGMENTS = {"lib", "libs", "libraries", "shaded", "shade", "dependencies", "deps", "thirdparty", "external",
                    "acf"}
# Package segments for per-Minecraft-version modules.
VERSION_SEGMENT = re.compile(r"^(v\d+_\d+.*|nms|legacy|multiversion|mc1_\d+.*)$", re.I)


# XSeries (github.com/CryptoMorin/XSeries) is usually relocated under the plugin's own package; its distinctive class
# names catch copies relocated without "xseries" in the package (e.g. oneblock.utils.lib.XBlock).
XSERIES_CLASSES = {"XMaterial", "XBlock", "XBase", "XEntityType", "XPotion", "XEnchantment", "XSound", "XItemStack",
                   "XTag", "XBiome", "XAttribute", "XParticle", "XSkull", "XReflection", "XRegistry", "XItemFlag",
                   "XPatternType", "XGameMode", "XWorldBorder"}


def is_xseries(class_name: str) -> bool:
    lower = class_name.lower()
    simple = class_name.rsplit(".", 1)[-1].split("$")[0]
    return "xseries" in lower or "cryptomorin" in lower or simple in XSERIES_CLASSES


# Known libraries, matched against the lowercased class name, in order; relocated copies keep these fragments.
LIBRARY_PATTERNS = [
    ("PacketEvents", ("packetevents", ".libs.pe.")),
    ("AxAPI", ("axapi",)),
    ("ACF (Aikar's command framework)", ("co.aikar.commands", ".acf.", "aikar")),
    ("BookUtil", ("bookutil",)),
    ("MineAcademy Foundation", ("org.mineacademy.fo",)),
    ("InventoryGui", ("inventorygui",)),
    ("SignGUI", ("signgui",)),
    ("rtag", ("saicone.rtag",)),
    ("NBT-API", ("tr7zw", "nbtapi")),
    ("EffectLib", ("de.slikey.effectlib",)),
    ("ScreamingSandals SGUI", ("screamingsandals.bedwars.lib.sgui",)),
    ("PhoenixPlugins common lib", ("phoenixcrates.lib.common",)),
]


def library_name(class_name: str) -> str | None:
    """The library a class belongs to, or None for the plugin's own code (including its own version modules).

    Bundled code under a lib/libs/shaded package that matches no known library is reported by its package.
    """
    if is_xseries(class_name):
        return "XSeries"
    lower = class_name.lower()
    simple = class_name.rsplit(".", 1)[-1]
    if simple.startswith("BukkitCommandCompletions"):
        return "ACF (Aikar's command framework)"
    if simple.startswith("BookUtil"):
        return "BookUtil"
    for name, fragments in LIBRARY_PATTERNS:
        if any(f in lower for f in fragments):
            return name
    if category(class_name) == "bundled-library":
        return "Other bundled code: " + class_name.rsplit(".", 1)[0]
    return None


def category(class_name: str) -> str:
    if is_xseries(class_name):
        return "bundled-library"
    lower = class_name.lower()
    segments = lower.split(".")[:-1]
    if any(VERSION_SEGMENT.match(s) for s in segments):
        return "version-specific"
    if any(name in lower for name in LIBRARY_NAMES) or any(s in LIBRARY_SEGMENTS for s in segments):
        return "bundled-library"
    return "plugin-code"


def key(row: dict) -> tuple[str, str, str]:
    return row["type"], row["path"], row["owner"]


def describe(k: tuple[str, str, str]) -> str:
    kind, path, owner = k
    if kind == "TYPE":
        return f"`{path.rsplit('.', 1)[-1]}` (type)"
    simple_owner = owner.rsplit(".", 1)[-1] if "." in owner else owner
    # strip packages from parameter types for readability
    path = re.sub(r"\b(?:[a-z_][\w]*\.)+([A-Z][\w]*)", r"\1", path)
    return f"`{simple_owner}#{path}`" if kind != "CONSTRUCTOR" else f"`{path}` (constructor)"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--usage", type=Path, default=Path("report/data/usage-by-class.csv"))
    parser.add_argument("--manifest", type=Path, default=Path("static-plugin-fetch/sample/manifest.csv"))
    parser.add_argument("--deprecated", type=Path, default=Path("report/data/deprecated-data.csv"))
    parser.add_argument("--out", type=Path, default=Path("report/data"))
    args = parser.parse_args()

    manifest = {r["file"]: r for r in csv.DictReader(args.manifest.open())}
    buckets_of = {f: set(r["buckets"].split("+")) for f, r in manifest.items()}
    bucket_sizes = {b: sum(b in bs for bs in buckets_of.values()) for b in BUCKETS}

    entries = []
    with args.deprecated.open() as f:
        lines = [line for line in f if not line.startswith("#") and line.strip()]
    for row in csv.DictReader(lines):
        entries.append(key(row))

    # entry -> category -> set of plugins; plugin -> category -> set of entries
    by_entry: dict = collections.defaultdict(lambda: collections.defaultdict(set))
    uses: collections.Counter = collections.Counter()
    by_plugin: dict = collections.defaultdict(lambda: collections.defaultdict(set))
    # (plugin, entry) -> {"xseries", "other"}: where that plugin's references to that entry come from
    xs_sources: dict = collections.defaultdict(set)
    xs_uses = 0
    total_uses = 0
    # per library: plugins whose copy references an entry, entries referenced, reference sites
    OWN = "<own code>"
    lib_sources: dict = collections.defaultdict(set)  # plugin -> libraries (or OWN) its references come from
    lib_plugins: dict = collections.defaultdict(set)
    lib_entries: dict = collections.defaultdict(set)
    lib_uses: collections.Counter = collections.Counter()
    for row in csv.DictReader(args.usage.open()):
        cat = category(row["class"])
        k = key(row)
        by_entry[k][cat].add(row["plugin"])
        by_plugin[row["plugin"]][cat].add(k)
        uses[k] += int(row["uses"])
        lib = library_name(row["class"])
        lib_sources[row["plugin"]].add(lib or OWN)
        if lib:
            lib_plugins[lib].add(row["plugin"])
            lib_entries[lib].add(k)
            lib_uses[lib] += int(row["uses"])
        xs = is_xseries(row["class"])
        xs_sources[(row["plugin"], k)].add("xseries" if xs else "other")
        total_uses += int(row["uses"])
        xs_uses += int(row["uses"]) if xs else 0

    def via_xseries(k) -> set:
        return {p for (p, e), src in xs_sources.items() if e == k and "xseries" in src}

    def only_xseries(k) -> set:
        return {p for (p, e), src in xs_sources.items() if e == k and src == {"xseries"}}

    args.out.mkdir(parents=True, exist_ok=True)
    with (args.out / "usage-attributed.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["type", "path", "owner", "uses", "plugins_any", "plugins_plugin_code", "plugins_only_library",
                    "plugins_only_version_specific", "plugins_via_xseries", "plugins_only_xseries",
                    *[f"plugin_code_{b}" for b in BUCKETS]])
        for k in entries:
            cats = by_entry.get(k, {})
            anyp = set().union(*cats.values()) if cats else set()
            code = cats.get("plugin-code", set())
            only_lib = {p for p in cats.get("bundled-library", set()) if p not in code}
            only_ver = {p for p in cats.get("version-specific", set()) if p not in code and p not in only_lib}
            w.writerow([*k, uses[k], len(anyp), len(code), len(only_lib), len(only_ver),
                        len(via_xseries(k)), len(only_xseries(k)),
                        *[sum(b in buckets_of.get(p, ()) for p in code) for b in BUCKETS]])

    with (args.out / "plugins-attributed.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["plugin", "source", "buckets", "downloads", "published", *[f"entries_{c}" for c in CATEGORIES],
                    "entries_via_xseries", "only_xseries"])
        for plugin, cats in sorted(by_plugin.items()):
            m = manifest.get(plugin, {})
            sources = [src for (p, _), src in xs_sources.items() if p == plugin]
            w.writerow([plugin, m.get("source", ""), m.get("buckets", ""), m.get("downloads", ""),
                        m.get("published", "")[:10], *[len(cats.get(c, ())) for c in CATEGORIES],
                        sum("xseries" in src for src in sources), all(src == {"xseries"} for src in sources)])

    with (args.out / "libraries-attributed.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["library", "plugins", "plugins_only_this_library", "entries", "uses", *[f"plugins_{b}" for b in BUCKETS]])
        for lib in sorted(lib_plugins, key=lambda lib: (-len(lib_plugins[lib]), lib)):
            only = sum(1 for p in lib_plugins[lib] if lib_sources[p] == {lib})
            w.writerow([lib, len(lib_plugins[lib]), only, len(lib_entries[lib]), lib_uses[lib],
                        *[sum(b in buckets_of.get(p, ()) for p in lib_plugins[lib]) for b in BUCKETS]])

    # ---------------------------------------------------------------- Markdown summary
    total = len(manifest)
    any_use = set(by_plugin)
    code_use = {p for p, cats in by_plugin.items() if cats.get("plugin-code")}

    def share(plugins: set, bucket: str | None) -> str:
        n = sum(1 for p in plugins if bucket is None or bucket in buckets_of.get(p, ()))
        size = total if bucket is None else bucket_sizes[bucket]
        return f"{n} ({100 * n / size:.1f}%)"

    print("### Plugins referencing any of the deprecated entries\n")
    print("| Bucket | Plugins | Any reference | From the plugin's own code |")
    print("|---|---:|---:|---:|")
    for b in (*BUCKETS, None):
        size = total if b is None else bucket_sizes[b]
        print(f"| {b or '**All**'} | {size} | {share(any_use, b)} | {share(code_use, b)} |")

    used = [k for k in entries if by_entry.get(k)]
    code_used = [k for k in entries if by_entry.get(k, {}).get("plugin-code")]
    print(f"\n{len(used)} of {len(entries)} entries are referenced by at least one plugin; "
          f"{len(code_used)} are referenced from a plugin's own code.\n")

    print("### Referenced entries\n")
    print("| Entry | Plugins (any) | Plugin code | Only via bundled library | Only via version-specific module "
          "| Plugin code: popular / legacy / random tail |")
    print("|---|---:|---:|---:|---:|---:|")
    for k in sorted(used, key=lambda k: (-len(by_entry[k].get("plugin-code", ())),
                                          -len(set().union(*by_entry[k].values())))):
        cats = by_entry[k]
        code = cats.get("plugin-code", set())
        only_lib = {p for p in cats.get("bundled-library", set()) if p not in code}
        only_ver = {p for p in cats.get("version-specific", set()) if p not in code and p not in only_lib}
        per_bucket = " / ".join(str(sum(b in buckets_of.get(p, ()) for p in code)) for b in BUCKETS)
        print(f"| {describe(k)} | {len(set().union(*cats.values()))} | {len(code)} | {len(only_lib)} "
              f"| {len(only_ver)} | {per_bucket} |")

    plugin_sources = collections.defaultdict(set)
    for (p, _), src in xs_sources.items():
        plugin_sources[p] |= src
    xs_any = {p for p, src in plugin_sources.items() if "xseries" in src}
    xs_only = {p for p, src in plugin_sources.items() if src == {"xseries"}}
    print("\n### XSeries\n")
    print(f"- Plugins referencing any entry: {len(any_use)}")
    print(f"- ...of which bundle XSeries that references one: {len(xs_any)} ({100 * len(xs_any) / len(any_use):.0f}%)")
    print(f"- ...of which reference the entries **only** through XSeries: {len(xs_only)} "
          f"({100 * len(xs_only) / len(any_use):.0f}%)")
    print(f"- Reference sites inside XSeries: {xs_uses} of {total_uses} ({100 * xs_uses / total_uses:.0f}%)\n")
    print("| Entry | Plugins (any) | Via XSeries | Only via XSeries |")
    print("|---|---:|---:|---:|")
    for k in sorted([k for k in used if via_xseries(k)], key=lambda k: -len(only_xseries(k))):
        print(f"| {describe(k)} | {len(set().union(*by_entry[k].values()))} | {len(via_xseries(k))} "
              f"| {len(only_xseries(k))} |")

    unused = [k for k in entries if not by_entry.get(k)]
    fields = [k for k in unused if k[0] == "FIELD"]
    print(f"\n{len(unused)} entries are not referenced by any plugin in the sample"
          f" ({len(fields)} of them are compile-time constant fields, which static analysis can't detect).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
