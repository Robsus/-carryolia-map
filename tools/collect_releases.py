#!/usr/bin/env python3
"""
Gathers, for every planned region, its newest release (tag
"<region>-YYYY-MM-DD-r<run>"): downloads its summary and outline into
<dist>/<region>/ for make_manifest.py, writes status/regions.md, and deletes
releases of a region beyond the newest --keep.

Needs the GitHub CLI (gh) with a token that may manage releases.

Usage:
  python collect_releases.py --repo OWNER/REPO --regions regions.json --dist dist [--keep 2] [--prune]
"""

import argparse
import json
import os
import re
import subprocess
from datetime import datetime, timezone

TAG = re.compile(r"^(?P<region>.+)-(?P<date>\d{4}-\d{2}-\d{2})-r(?P<run>\d+)$")


def gh(*args, capture=True):
    return subprocess.run(["gh", *args], check=True, text=True,
                          stdout=subprocess.PIPE if capture else None).stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--regions", required=True, help="regions.json from plan_regions.py")
    ap.add_argument("--dist", default="dist")
    ap.add_argument("--keep", type=int, default=2)
    ap.add_argument("--prune", action="store_true")
    ap.add_argument("--status", default="status/regions.md")
    args = ap.parse_args()

    with open(args.regions) as f:
        planned = {r["id"]: r for r in json.load(f)}

    lines = gh("api", f"repos/{args.repo}/releases", "--paginate", "--jq", ".[] | [.tag_name, .id] | @tsv")
    by_region = {}
    for line in lines.splitlines():
        tag, _ = line.split("\t")
        m = TAG.match(tag)
        if not m or m["region"] not in planned:
            continue
        by_region.setdefault(m["region"], []).append((m["date"], int(m["run"]), tag))

    os.makedirs(args.dist, exist_ok=True)
    rows = []
    for region in sorted(planned):
        releases = sorted(by_region.get(region, []), reverse=True)
        if not releases:
            rows.append((region, "—", "—", "—", "—", planned[region]["day"]))
            continue
        tag = releases[0][2]
        folder = os.path.join(args.dist, region)
        os.makedirs(folder, exist_ok=True)
        gh("release", "download", tag, "--repo", args.repo, "--dir", folder, "--clobber",
           "--pattern", f"{region}.summary.json", "--pattern", f"{region}.poly", capture=False)
        with open(os.path.join(folder, "tag"), "w") as f:
            f.write(tag + "\n")
        with open(os.path.join(folder, f"{region}.summary.json")) as f:
            s = json.load(f)
        rows.append((region, tag, s.get("osm_timestamp", "")[:10], f"{s['archive_bytes'] / 1e6:,.0f}",
                     len(s["parts"]), planned[region]["day"]))
        if args.prune:
            for _, _, old in releases[args.keep:]:
                print(f"Deleting old release {old}")
                gh("release", "delete", old, "--repo", args.repo, "--yes", "--cleanup-tag", capture=False)

    os.makedirs(os.path.dirname(args.status), exist_ok=True)
    built = [r for r in rows if r[1] != "—"]
    with open(args.status, "w") as f:
        f.write("# Map data status\n\n")
        f.write(f"Updated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}: "
                f"{len(built)} of {len(rows)} regions built. Written by the \"Build map data\" workflow; "
                "every region is rebuilt once a month on its own day.\n\n")
        f.write("| Region | Release | OSM data of | Archive (MB) | Parts | Monthly day |\n")
        f.write("| --- | --- | --- | ---: | ---: | ---: |\n")
        for r in rows:
            f.write("| " + " | ".join(str(x) for x in r) + " |\n")
    print(f"{len(built)} of {len(rows)} regions have a release")


if __name__ == "__main__":
    main()
