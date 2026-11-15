#!/usr/bin/env python3
"""List every sampled plugin that bundles XSeries, and whether its copy references the deprecated entries.

For each jar in the sample this looks for XSeries classes (same detection as summarize.py: an `xseries`/`cryptomorin`
package or XSeries' distinctive class names), and records:

  relocation      the package XSeries was relocated to inside the plugin
  xseries_version the bundled XSeries version, when the jar still carries XSeries' Maven pom.properties; otherwise empty
  xseries_classes how many XSeries classes the jar contains
  plus, from plugins-attributed.csv, whether that copy references any of the deprecated entries

Writes report/data/xseries-plugins.csv and prints a Markdown table.
"""

from __future__ import annotations

import argparse
import collections
import csv
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from summarize import is_xseries  # noqa: E402  (same detection rule as the attribution)


def scan(jar: Path) -> dict | None:
    try:
        z = zipfile.ZipFile(jar)
    except zipfile.BadZipFile:
        return None
    with z:
        names = z.namelist()
        classes = [n[:-6].replace("/", ".") for n in names
                   if n.endswith(".class") and not n.startswith("META-INF/versions/")]
        xs = [c for c in classes if is_xseries(c)]
        if not xs:
            return None
        packages = collections.Counter(c.rsplit(".", 1)[0] for c in xs)
        anchor = next((c for c in xs if c.rsplit(".", 1)[-1] == "XMaterial"), None)
        relocation = anchor.rsplit(".", 1)[0] if anchor else packages.most_common(1)[0][0]
        version = ""
        for n in names:
            if n.lower().endswith("cryptomorin/xseries/pom.properties"):
                for line in z.read(n).decode(errors="replace").splitlines():
                    if line.startswith("version="):
                        version = line.split("=", 1)[1].strip()
        return {"relocation": relocation, "xseries_version": version, "xseries_classes": len(xs)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--jars", type=Path, default=Path("static-plugin-fetch/sample/jars"))
    parser.add_argument("--manifest", type=Path, default=Path("static-plugin-fetch/sample/manifest.csv"))
    parser.add_argument("--plugins", type=Path, default=Path("report/data/plugins-attributed.csv"))
    parser.add_argument("--out", type=Path, default=Path("report/data/xseries-plugins.csv"))
    args = parser.parse_args()

    manifest = {r["file"]: r for r in csv.DictReader(args.manifest.open())}
    attributed = {r["plugin"]: r for r in csv.DictReader(args.plugins.open())}

    rows = []
    for jar in sorted(args.jars.glob("*.jar")):
        found = scan(jar)
        if not found:
            continue
        m = manifest.get(jar.name, {})
        a = attributed.get(jar.name, {})
        via = int(a.get("entries_via_xseries") or 0)
        rows.append({
            "plugin": jar.name,
            "name": m.get("name", ""),
            "source": m.get("source", ""),
            "buckets": m.get("buckets", ""),
            "downloads": m.get("downloads", ""),
            "published": m.get("published", "")[:10],
            **found,
            "xseries_references_deprecated": via > 0,
            "entries_via_xseries": via,
            "only_via_xseries": a.get("only_xseries") == "True" and via > 0,
        })
    rows.sort(key=lambda r: -int(r["downloads"] or 0))

    with args.out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["plugin"])
        w.writeheader()
        w.writerows(rows)

    total = len(manifest)
    referencing = sum(r["xseries_references_deprecated"] for r in rows)
    print(f"{len(rows)} of {total} sampled plugins bundle XSeries; in {referencing} of them the bundled copy references "
          f"at least one deprecated entry.\n")
    print("| Plugin | Source | Bucket | Downloads | XSeries version | Copy references deprecated API | "
          "Only via XSeries |")
    print("|---|---|---|---:|---|---|---|")
    for r in rows:
        refs = f"yes ({r['entries_via_xseries']} entries)" if r["xseries_references_deprecated"] else "no"
        print(f"| {r['name']} | {r['source'].capitalize()} | {r['buckets'].replace('+', ', ')} "
              f"| {int(r['downloads'] or 0):,} | {r['xseries_version'] or 'unknown'} | {refs} "
              f"| {'yes' if r['only_via_xseries'] else 'no'} |")
    print(f"\nWrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
