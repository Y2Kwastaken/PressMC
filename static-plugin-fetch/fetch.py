#!/usr/bin/env python3
"""Fetch a sample of plugin jars from Modrinth and Hangar for static analysis.

For each project (in order of downloads) its newest Paper/Bukkit jar is taken, preferring release channels, whatever
Minecraft version it is marked for. How old the jar is and the newest Minecraft version it claims to support are
recorded in the manifest, so the analysis can be split by age or compatibility afterwards. Passing --game-version only
adds a marked_compatible column; it never filters.

Three buckets are filled per source:

  popular      the most downloaded projects
  legacy       projects whose newest jar is old (published before a cutoff), i.e. plugins that haven't been updated
               in a while; also chosen by downloads
  random-tail  projects drawn uniformly at random (seeded, reproducible) from everything ranked below the last
               popular pick, so results can speak for the whole ecosystem and not only its most popular plugins

A project can be in both popular and legacy.

Jars are streamed: each one is downloaded as soon as it is selected and its manifest row is flushed straight away,
so an interrupted run keeps what it fetched and a re-run skips jars already present and verified. Projects found on both sources are only taken once (by normalized name, and by
jar SHA-256 after download).

Output (default: ./sample):
  sample/jars/*.jar      the plugin jars, a flat folder that static-analyzer can read directly
  sample/manifest.csv    one row per jar: source, project, downloads, version, publish date, buckets, hashes

Only uses the Python standard library. Downloaded jars belong to their authors: use them for analysis only and do not
redistribute them (sample/ is git-ignored).
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import datetime as dt
import hashlib
import json
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterator

MODRINTH_API = "https://api.modrinth.com/v2"
HANGAR_API = "https://hangar.papermc.io/api/v1"

# Modrinth loaders that run on a Paper server. Folia-only and proxy (velocity, bungeecord, waterfall) jars are excluded.
MODRINTH_LOADERS = ["paper", "spigot", "bukkit", "purpur"]
RELEASE_CHANNELS = {"release", "stable"}

DEFAULT_USER_AGENT = "static-plugin-fetch/0.1"


# --------------------------------------------------------------------------------------------------------------------
# HTTP


class Http:
    """Small JSON/download client with a User-Agent, retries, and rate-limit handling."""

    def __init__(self, user_agent: str, min_interval: float = 0.0):
        self.user_agent = user_agent
        self.min_interval = min_interval
        self._last_request = 0.0

    def _open(self, url: str, timeout: float = 60.0):
        for attempt in range(6):
            wait = self.min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            request = urllib.request.Request(url, headers={"User-Agent": self.user_agent, "Accept": "application/json"})
            try:
                response = urllib.request.urlopen(request, timeout=timeout)
            except urllib.error.HTTPError as e:
                if e.code == 429 or e.code >= 500:
                    delay = _retry_delay(e.headers, attempt)
                    log(f"  HTTP {e.code} for {url}, retrying in {delay:.0f}s")
                    time.sleep(delay)
                    continue
                raise
            except (urllib.error.URLError, TimeoutError) as e:
                delay = 2 ** attempt
                log(f"  {e} for {url}, retrying in {delay}s")
                time.sleep(delay)
                continue
            # Modrinth: slow down before hitting the per-minute limit instead of after
            remaining = response.headers.get("X-Ratelimit-Remaining")
            reset = response.headers.get("X-Ratelimit-Reset")
            if remaining is not None and reset is not None and int(remaining) <= 2:
                time.sleep(int(reset) + 1)
            return response
        raise RuntimeError(f"giving up on {url}")

    def json(self, url: str, params: dict | None = None):
        if params:
            url = url + "?" + urllib.parse.urlencode(params)
        with self._open(url) as response:
            return json.load(response)

    def download(self, url: str, target: Path) -> dict[str, str]:
        """Downloads to target (via a .part file) and returns the file's sha1/sha256/sha512 hex digests."""
        digests = {name: hashlib.new(name) for name in ("sha1", "sha256", "sha512")}
        part = target.with_name(target.name + ".part")
        with self._open(url, timeout=300) as response, part.open("wb") as out:
            while chunk := response.read(1 << 16):
                out.write(chunk)
                for digest in digests.values():
                    digest.update(chunk)
        part.replace(target)
        return {name: digest.hexdigest() for name, digest in digests.items()}


def _retry_delay(headers, attempt: int) -> float:
    for header in ("Retry-After", "X-Ratelimit-Reset"):
        value = headers.get(header) if headers else None
        if value and value.isdigit():
            return int(value) + 1
    return min(2 ** attempt, 60)


def file_digests(path: Path) -> dict[str, str]:
    digests = {name: hashlib.new(name) for name in ("sha1", "sha256", "sha512")}
    with path.open("rb") as f:
        while chunk := f.read(1 << 16):
            for digest in digests.values():
                digest.update(chunk)
    return {name: digest.hexdigest() for name, digest in digests.items()}


# --------------------------------------------------------------------------------------------------------------------
# Data


@dataclasses.dataclass
class Candidate:
    """A project from a source's search, in descending download order."""
    source: str
    project_id: str
    slug: str
    name: str
    downloads: int
    page_url: str
    rank: int = 0  # 1-based position in the source's download ordering


@dataclasses.dataclass
class Release:
    """The jar chosen for a project: its newest version, preferring release channels."""
    version: str
    channel: str
    published: dt.datetime
    file_name: str
    download_url: str
    hash_algorithm: str  # which of the source-provided hashes to verify against; empty if unverifiable
    hash_value: str
    game_versions: list[str] = dataclasses.field(default_factory=list)  # Minecraft versions it is marked for

    @property
    def newest_game_version(self) -> str:
        return newest_version(self.game_versions)


@dataclasses.dataclass
class Selected:
    candidate: Candidate
    release: Release
    buckets: list[str]
    jar_name: str = ""
    sha256: str = ""


def parse_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def _version_key(version: str) -> tuple[int, ...] | None:
    """'1.21.11' -> (1, 21, 11), '26.3' -> (26, 3); snapshots such as '25w14a' -> None."""
    parts = version.split(".")
    return tuple(int(p) for p in parts) if all(p.isdigit() for p in parts) else None


def newest_version(versions: list[str]) -> str:
    keyed = [(key, v) for v in versions if (key := _version_key(v)) is not None]
    return max(keyed)[1] if keyed else ""


def normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def safe_file_part(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._+-]", "_", value).strip("._") or "x"


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------------------------------------------------
# Sources


class Modrinth:
    name = "modrinth"

    def __init__(self, http: Http):
        self.http = http

    FACETS = json.dumps([
        ["project_type:plugin"],
        [f"categories:{loader}" for loader in MODRINTH_LOADERS],
    ])

    def _search(self, offset: int, limit: int) -> dict:
        return self.http.json(f"{MODRINTH_API}/search", {
            "facets": self.FACETS,
            "index": "downloads",
            "limit": limit,
            "offset": offset,
        })

    def _candidate(self, hit: dict, rank: int) -> Candidate:
        return Candidate(self.name, hit["project_id"], hit["slug"], hit["title"], int(hit["downloads"]),
                         f"https://modrinth.com/plugin/{hit['slug']}", rank=rank)

    def candidates(self, limit: int) -> Iterator[Candidate]:
        offset = 0
        while offset < limit:
            page = self._search(offset, min(100, limit - offset))
            hits = page.get("hits", [])
            for hit in hits:
                offset += 1
                yield self._candidate(hit, offset)
            if not hits or offset >= page.get("total_hits", 0):
                return

    def total(self) -> int:
        return int(self._search(0, 1).get("total_hits", 0))

    def candidate_at(self, rank: int) -> Candidate | None:
        """The project at a 1-based position in download order."""
        hits = self._search(rank - 1, 1).get("hits", [])
        return self._candidate(hits[0], rank) if hits else None

    def release(self, candidate: Candidate) -> Release | None:
        versions = self.http.json(f"{MODRINTH_API}/project/{candidate.project_id}/version", {
            "loaders": json.dumps(MODRINTH_LOADERS),
        })
        releases = []
        for version in versions:
            jars = [f for f in version.get("files", []) if f["filename"].endswith(".jar")]
            if not jars:
                continue
            file = next((f for f in jars if f.get("primary")), jars[0])
            releases.append(Release(version["version_number"], version.get("version_type", ""),
                                    parse_time(version["date_published"]), file["filename"], file["url"],
                                    "sha512", file["hashes"]["sha512"], list(version.get("game_versions", []))))
        return choose_release(releases)


class Hangar:
    name = "hangar"
    page_size = 25  # Hangar's maximum

    def __init__(self, http: Http, allow_external: bool = False):
        self.http = http
        self.allow_external = allow_external

    def _projects(self, offset: int, limit: int) -> dict:
        return self.http.json(f"{HANGAR_API}/projects", {
            "limit": limit,
            "offset": offset,
            "sort": "-downloads",
            "platform": "PAPER",
        })

    def _candidate(self, project: dict, rank: int) -> Candidate:
        namespace = project["namespace"]
        return Candidate(self.name, str(project["id"]), namespace["slug"], project["name"],
                         int(project["stats"]["downloads"]),
                         f"https://hangar.papermc.io/{namespace['owner']}/{namespace['slug']}", rank=rank)

    def candidates(self, limit: int) -> Iterator[Candidate]:
        offset = 0
        while offset < limit:
            page = self._projects(offset, min(self.page_size, limit - offset))
            results = page.get("result", [])
            for project in results:
                offset += 1
                yield self._candidate(project, offset)
            if not results or offset >= page.get("pagination", {}).get("count", 0):
                return

    def total(self) -> int:
        return int(self._projects(0, 1).get("pagination", {}).get("count", 0))

    def candidate_at(self, rank: int) -> Candidate | None:
        """The project at a 1-based position in download order."""
        results = self._projects(rank - 1, 1).get("result", [])
        return self._candidate(results[0], rank) if results else None

    def release(self, candidate: Candidate) -> Release | None:
        releases: list[Release] = []
        for page_index in range(4):  # newest first; look further back only until a usable build turns up
            page = self.http.json(f"{HANGAR_API}/projects/{urllib.parse.quote(candidate.slug)}/versions", {
                "limit": self.page_size,
                "offset": page_index * self.page_size,
                "platform": "PAPER",
            })
            for version in page.get("result", []):
                download = (version.get("downloads") or {}).get("PAPER")
                if not download:
                    continue
                channel = version.get("channel", {}).get("name", "")
                published = parse_time(version["createdAt"])
                game_versions = list((version.get("platformDependencies") or {}).get("PAPER", []))
                if download.get("downloadUrl") and download.get("fileInfo"):
                    info = download["fileInfo"]
                    release = Release(version["name"], channel, published, info["name"], download["downloadUrl"],
                                      "sha256", info["sha256Hash"], game_versions)
                elif self.allow_external and _is_direct_jar(download.get("externalUrl")):
                    # hosted elsewhere (e.g. GitHub releases): no hash to verify against
                    url = download["externalUrl"]
                    file_name = urllib.parse.unquote(urllib.parse.urlparse(url).path.rsplit("/", 1)[-1])
                    release = Release(version["name"], channel, published, file_name, url, "", "", game_versions)
                else:
                    continue
                releases.append(release)
            if any(r.channel.lower() in RELEASE_CHANNELS for r in releases) or len(page.get("result", [])) < self.page_size:
                break
        return choose_release(releases)


def choose_release(releases: list[Release]) -> Release | None:
    """The jar that best represents a project: release channels first, then the build targeting the newest Minecraft
    version, then the most recently published. Projects often upload one build per Minecraft version on the same day,
    so "most recent upload" alone can pick an old-version backport."""
    if not releases:
        return None
    stable = [r for r in releases if r.channel.lower() in RELEASE_CHANNELS] or releases
    return max(stable, key=lambda r: (_version_key(r.newest_game_version) or (), r.published))


def _is_direct_jar(url: str | None) -> bool:
    """An external link straight to a jar file. Links to Modrinth pages are left to the Modrinth source."""
    if not url:
        return False
    parsed = urllib.parse.urlparse(url)
    return parsed.scheme == "https" and parsed.path.lower().endswith(".jar") and "modrinth.com" not in parsed.netloc


# --------------------------------------------------------------------------------------------------------------------
# Selection
#
# Streaming: each jar is downloaded as soon as it is selected and its manifest row is written and flushed straight
# away, so an interrupted run keeps everything fetched so far and a re-run skips jars that are already present and
# verified. Quotas count jars actually kept, so a failed download or byte-identical duplicate doesn't use up a slot.


@dataclasses.dataclass
class Quota:
    popular: int
    legacy: int
    random_tail: int


class Collector:
    """Downloads each selected jar right away, drops byte-identical duplicates, and appends it to the manifest."""

    def __init__(self, http: Http | None, jar_dir: Path, manifest: "ManifestWriter | None"):
        self.http = http  # None for a dry run
        self.jar_dir = jar_dir
        self.manifest = manifest
        self.taken_names: set[str] = set()
        self.seen_hashes: dict[str, str] = {}
        self.kept: list[Selected] = []

    def is_taken(self, candidate: Candidate) -> bool:
        return normalize_name(candidate.name) in self.taken_names

    def accept(self, item: Selected) -> bool:
        c, r = item.candidate, item.release
        item.jar_name = f"{c.source}-{safe_file_part(c.slug)}-{safe_file_part(r.version)}.jar"
        if self.http is not None and not self._fetch(item):
            return False
        self.taken_names.add(normalize_name(c.name))
        self.kept.append(item)
        if self.manifest:
            self.manifest.write(item)
        log(f"  [{'+'.join(item.buckets):14}] #{c.rank:<6} {c.name} {r.version} "
            f"({r.published.date()}, MC {r.newest_game_version or '?'}, {c.downloads:,} downloads)")
        return True

    def _fetch(self, item: Selected) -> bool:
        r = item.release
        target = self.jar_dir / item.jar_name
        verified = bool(r.hash_algorithm)
        digests = file_digests(target) if target.exists() else None
        up_to_date = digests is not None and (not verified or digests[r.hash_algorithm] == r.hash_value.lower())
        if not up_to_date:
            try:
                digests = self.http.download(r.download_url, target)
            except Exception as e:
                log(f"  failed {item.jar_name}: {e}")
                return False
            if verified and digests[r.hash_algorithm] != r.hash_value.lower():
                log(f"  {r.hash_algorithm} mismatch for {item.jar_name}, discarding")
                target.unlink(missing_ok=True)
                return False
            if not _looks_like_jar(target):
                log(f"  {item.jar_name} is not a jar (got a web page?), discarding")
                target.unlink(missing_ok=True)
                return False
        item.sha256 = digests["sha256"]
        if item.sha256 in self.seen_hashes:
            log(f"  {item.jar_name} is identical to {self.seen_hashes[item.sha256]}, dropping the duplicate")
            target.unlink(missing_ok=True)
            return False
        self.seen_hashes[item.sha256] = item.jar_name
        return True


def _release_or_none(source, candidate: Candidate, skipped: dict[str, int]) -> Release | None:
    try:
        release = source.release(candidate)
    except Exception as e:  # one broken project shouldn't stop the run
        log(f"  {source.name}/{candidate.slug}: {e}")
        skipped["error"] += 1
        return None
    if release is None:
        skipped["no downloadable jar"] += 1
    return release


def select_ranked(source, collector: Collector, quota: Quota, scan: int, legacy_before: dt.datetime) -> int:
    """Walks a source's projects by downloads and fills the popular and legacy buckets.

    Returns the rank of the last popular pick: everything below it is the tail.
    """
    popular_count = legacy_count = 0
    last_popular_rank = 0
    skipped = {"no downloadable jar": 0, "duplicate": 0, "error": 0, "download failed": 0}
    for candidate in source.candidates(scan):
        if popular_count >= quota.popular and legacy_count >= quota.legacy:
            break
        if collector.is_taken(candidate):
            skipped["duplicate"] += 1
            continue
        wants_popular = popular_count < quota.popular
        if not wants_popular and legacy_count >= quota.legacy:
            continue
        release = _release_or_none(source, candidate, skipped)
        if release is None:
            continue
        buckets = []
        if wants_popular:
            buckets.append("popular")
        if release.published < legacy_before and legacy_count < quota.legacy:
            buckets.append("legacy")
        if not buckets:
            continue
        if not collector.accept(Selected(candidate, release, buckets)):
            skipped["download failed"] += 1
            continue
        popular_count += "popular" in buckets
        legacy_count += "legacy" in buckets
        if "popular" in buckets:
            last_popular_rank = candidate.rank
    log(f"{source.name}: {popular_count} popular, {legacy_count} legacy; skipped {skipped}")
    if legacy_count < quota.legacy:
        log(f"{source.name}: only found {legacy_count}/{quota.legacy} legacy jars in the top {scan} projects "
            f"(raise --scan)")
    return last_popular_rank


def select_random_tail(source, collector: Collector, count: int, tail_start: int, rng: random.Random) -> None:
    """Fills the random-tail bucket: projects drawn uniformly at random from the ranks below tail_start."""
    if count <= 0:
        return
    total = source.total()
    ranks = list(range(tail_start + 1, total + 1))
    rng.shuffle(ranks)
    log(f"{source.name}: drawing {count} random projects from ranks {tail_start + 1}-{total}")
    taken = 0
    skipped = {"no downloadable jar": 0, "duplicate": 0, "error": 0, "download failed": 0}
    for rank in ranks:
        if taken >= count:
            break
        try:
            candidate = source.candidate_at(rank)
        except Exception as e:
            log(f"  {source.name} rank {rank}: {e}")
            skipped["error"] += 1
            continue
        if candidate is None:
            continue
        if collector.is_taken(candidate):
            skipped["duplicate"] += 1
            continue
        release = _release_or_none(source, candidate, skipped)
        if release is None:
            continue
        if collector.accept(Selected(candidate, release, ["random-tail"])):
            taken += 1
        else:
            skipped["download failed"] += 1
    log(f"{source.name}: {taken} random-tail; skipped {skipped}")


def _looks_like_jar(path: Path) -> bool:
    with path.open("rb") as f:
        return f.read(4) == b"PK\x03\x04"


MANIFEST_COLUMNS = ["file", "source", "project_id", "slug", "name", "project_url", "downloads", "download_rank",
                    "buckets", "version", "channel", "published", "jar_age_days", "newest_game_version",
                    "marked_compatible", "game_versions", "sha256", "hash_verified", "download_url"]


class ManifestWriter:
    """Writes manifest.csv one row at a time, flushing after each, so it is complete up to any interruption.

    marked_compatible is whether the jar lists game_version, or empty when no --game-version was given.
    """

    def __init__(self, path: Path, game_version: str | None, now: dt.datetime):
        self.file = path.open("w", newline="")
        self.writer = csv.writer(self.file)
        self.writer.writerow(MANIFEST_COLUMNS)
        self.file.flush()
        self.game_version = game_version
        self.now = now

    def write(self, item: Selected) -> None:
        c, r = item.candidate, item.release
        self.writer.writerow([item.jar_name, c.source, c.project_id, c.slug, c.name, c.page_url, c.downloads, c.rank,
                              "+".join(item.buckets), r.version, r.channel, r.published.isoformat(),
                              (self.now - r.published).days, r.newest_game_version,
                              "" if self.game_version is None else self.game_version in r.game_versions,
                              " ".join(r.game_versions), item.sha256, bool(r.hash_algorithm), r.download_url])
        self.file.flush()

    def close(self) -> None:
        self.file.close()


# --------------------------------------------------------------------------------------------------------------------


def per_source(value: str) -> dict[str, int]:
    """'100' applies to every source; 'modrinth=400,hangar=300' sets each one (unlisted sources get 0)."""
    value = value.strip()
    if value.isdigit():
        return {"*": int(value)}
    result = {}
    for part in value.split(","):
        name, _, number = part.partition("=")
        if not number.strip().isdigit():
            raise argparse.ArgumentTypeError(f"expected N or source=N,... but got {value!r}")
        result[name.strip()] = int(number)
    return result


def quota_for(values: dict[str, int], source: str) -> int:
    return values.get(source, values.get("*", 0))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--game-version",
                        help="optional, e.g. 26.3: adds a marked_compatible column saying whether each jar lists this "
                             "Minecraft version (does not filter)")
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parent / "sample",
                        help="output folder (default: static-plugin-fetch/sample)")
    parser.add_argument("--popular", type=per_source, default="100",
                        help="popular jars: N for every source, or e.g. modrinth=400,hangar=300 (default 100)")
    parser.add_argument("--legacy", type=per_source, default="50",
                        help="legacy jars: N or per source like --popular (default 50)")
    parser.add_argument("--random-tail", type=per_source, default="25",
                        help="jars drawn at random from the projects ranked below the popular bucket: N or per "
                             "source like --popular (default 25)")
    parser.add_argument("--seed", type=int, default=1, help="random seed for the random tail (default 1)")
    age = parser.add_mutually_exclusive_group()
    age.add_argument("--legacy-before", type=dt.date.fromisoformat,
                     help="a jar is legacy if published before this date (YYYY-MM-DD)")
    age.add_argument("--legacy-years", type=float, default=2.0,
                     help="a jar is legacy if published at least this many years ago (default 2)")
    parser.add_argument("--scan", type=int, default=1500,
                        help="most projects to look at per source for popular/legacy, in download order "
                             "(default 1500)")
    parser.add_argument("--sources", default="modrinth,hangar", help="comma-separated: modrinth,hangar")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT,
                        help="User-Agent sent to the APIs; Modrinth asks for one that identifies you, e.g. "
                             "'static-plugin-fetch/0.1 (you@example.com)'")
    parser.add_argument("--allow-external", action="store_true",
                        help="Hangar: also download versions hosted elsewhere when the link points straight at a "
                             ".jar (e.g. GitHub releases); these can't be hash-verified")
    parser.add_argument("--dry-run", action="store_true", help="select and list jars without downloading them")
    args = parser.parse_args()

    now = dt.datetime.now(dt.timezone.utc)
    if args.legacy_before:
        legacy_before = dt.datetime.combine(args.legacy_before, dt.time(), dt.timezone.utc)
    else:
        legacy_before = now - dt.timedelta(days=round(args.legacy_years * 365.25))
    log(f"legacy = newest jar published before {legacy_before.date()}; random seed {args.seed}")

    sources = []
    for name in [s.strip() for s in args.sources.split(",") if s.strip()]:
        if name == "modrinth":
            sources.append(Modrinth(Http(args.user_agent)))
        elif name == "hangar":
            sources.append(Hangar(Http(args.user_agent, min_interval=0.2), args.allow_external))
        else:
            parser.error(f"unknown source {name!r}")

    manifest = None
    if not args.dry_run:
        (args.out / "jars").mkdir(parents=True, exist_ok=True)
        manifest = ManifestWriter(args.out / "manifest.csv", args.game_version, now)
    collector = Collector(None if args.dry_run else Http(args.user_agent), args.out / "jars", manifest)
    rng = random.Random(args.seed)
    try:
        for source in sources:
            quota = Quota(quota_for(args.popular, source.name), quota_for(args.legacy, source.name),
                          quota_for(args.random_tail, source.name))
            log(f"\n{source.name}: {quota.popular} popular, {quota.legacy} legacy, {quota.random_tail} random-tail "
                f"(scanning up to {args.scan} projects)")
            tail_start = select_ranked(source, collector, quota, args.scan, legacy_before)
            select_random_tail(source, collector, quota.random_tail, tail_start, rng)
    finally:
        if manifest:
            manifest.close()

    counts = {bucket: sum(bucket in item.buckets for item in collector.kept)
              for bucket in ("popular", "legacy", "random-tail")}
    if args.dry_run:
        log(f"\ndry run: {len(collector.kept)} jars selected {counts}, nothing downloaded")
    else:
        log(f"\n{len(collector.kept)} jars {counts} in {args.out / 'jars'}; manifest: {args.out / 'manifest.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
