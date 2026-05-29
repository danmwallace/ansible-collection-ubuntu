#!/usr/bin/env python3
"""Keep a danmwallace.* collection's git tags in sync with its releases.

For each collection version, the release is represented by an annotated
``v<version>`` tag on the commit that *introduced* that version in
``galaxy.yml`` (the bump commit). Galaxy derives a published version from the
tarball's ``galaxy.yml``, never from git, so tagging is pure repo hygiene that
is easy to forget — this makes it routine and safe to re-run.

Two classes of collection are handled:

* **Galaxy-published** (e.g. danmwallace.docker/linux/fedora/azure): a version
  is only tagged once it is confirmed live on the Galaxy v3 API, so a tag never
  claims a release that did not actually land.
* **GitHub-only** (e.g. danmwallace.private): never on Galaxy, so the git
  history *is* the release record — every released version is tag-eligible.

The script is read-only by default (reports the gap). Pass --apply to create
and push the missing tags. It is idempotent: an existing correct tag is left
alone; an existing tag that points at the wrong commit is reported, never moved.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

GALAXY_BASE = "https://galaxy.ansible.com"
GALAXY_VERSIONS = (
    "/api/v3/plugin/ansible/content/published/collections/index/{ns}/{name}/versions/"
)


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def run(args, cwd, check=True):
    """Run a command, returning stripped stdout. Raises on non-zero if check."""
    res = subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, check=False
    )
    if check and res.returncode != 0:
        raise RuntimeError(
            f"command failed ({res.returncode}): {' '.join(args)}\n{res.stderr.strip()}"
        )
    return res.stdout.strip(), res.returncode


def galaxy_field(text, field):
    """Pull a top-level scalar field from galaxy.yml without a YAML dep.

    galaxy.yml keeps namespace/name/version/repository as flat top-level
    scalars, so a line-anchored regex is enough and avoids importing PyYAML
    (which may not be present in whatever env the skill runs under)."""
    m = re.search(rf"^{field}:\s*(.+?)\s*$", text, re.MULTILINE)
    return m.group(1).strip().strip("\"'") if m else None


def version_at(commit, cwd):
    """The galaxy.yml version as of a given commit, or None if absent there."""
    out, code = run(["git", "show", f"{commit}:galaxy.yml"], cwd, check=False)
    return galaxy_field(out, "version") if code == 0 else None


def introducer_commits(cwd):
    """Map every galaxy.yml version to the commit that first introduced it.

    Walk the commits that touched galaxy.yml newest->oldest. Because semver only
    moves forward, each version occupies one contiguous block in that history;
    the *oldest* commit in a block is the bump that introduced the version.
    """
    out, _ = run(
        ["git", "log", "--follow", "--format=%H", "--", "galaxy.yml"], cwd
    )
    commits = [c for c in out.splitlines() if c]
    pairs = [(c, version_at(c, cwd)) for c in commits]
    pairs = [(c, v) for c, v in pairs if v]  # drop commits where it didn't exist

    introducers = {}
    for i, (commit, ver) in enumerate(pairs):
        nxt = pairs[i + 1][1] if i + 1 < len(pairs) else None
        if ver != nxt:  # boundary: this is the oldest commit at this version
            introducers.setdefault(ver, commit)
    return introducers


def galaxy_published_versions(ns, name):
    """Set of versions published on Galaxy, or None if none are — which is how
    we recognise a GitHub-only collection.

    The versions endpoint always returns HTTP 200 (even for a collection that
    was never published, where it just reports an empty list), so emptiness —
    not a 404 — is the "not on Galaxy" signal. Results are paginated, so follow
    `links.next` to be correct for collections with more than one page."""
    versions = set()
    path = GALAXY_VERSIONS.format(ns=ns, name=name)
    while path:
        with urllib.request.urlopen(GALAXY_BASE + path, timeout=30) as resp:
            data = json.load(resp)
        versions.update(item["version"] for item in data.get("data", []))
        path = (data.get("links") or {}).get("next")
    return versions or None


def changelog_section(cwd, version):
    """The Keep-a-Changelog body for one version, for use as a release note.
    Returns None if there's no CHANGELOG or no matching section."""
    path = Path(cwd) / "CHANGELOG.md"
    if not path.exists():
        return None
    lines = path.read_text().splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(rf"^##\s*\[{re.escape(version)}\]", line):
            start = i + 1
            break
    if start is None:
        return None
    body = []
    for line in lines[start:]:
        if line.startswith("## ["):
            break
        body.append(line)
    text = "\n".join(body).strip()
    return text or None


# --------------------------------------------------------------------------- #
# per-collection planning
# --------------------------------------------------------------------------- #
def tag_commit(tag, cwd):
    out, code = run(["git", "rev-list", "-n", "1", tag], cwd, check=False)
    return out if code == 0 else None


def plan_collection(cwd, only_current):
    """Return (label, fqcn, rows). Each row describes one version's tag state:
    {version, tag, commit, status, note, eligible, message}."""
    text = (Path(cwd) / "galaxy.yml").read_text()
    ns = galaxy_field(text, "namespace")
    name = galaxy_field(text, "name")
    current = galaxy_field(text, "version")
    fqcn = f"{ns}.{name}"

    published = galaxy_published_versions(ns, name)
    git_only = published is None

    introducers = introducer_commits(cwd)
    versions = [current] if only_current else sorted(
        introducers, key=lambda v: [int(x) for x in re.findall(r"\d+", v)]
    )

    rows = []
    for ver in versions:
        commit = introducers.get(ver)
        tag = f"v{ver}"
        row = {"version": ver, "tag": tag, "commit": commit, "eligible": False}

        if commit is None:
            row.update(status="error", note="no commit found for this version")
            rows.append(row)
            continue

        existing = tag_commit(tag, cwd)
        if existing:
            short = existing[:9]
            # A release tag is correct if it points at a commit that actually
            # carried this version. We deliberately don't insist it match the
            # bump commit: a tag legitimately marks release-time HEAD, which can
            # be any commit during that version's life and isn't recoverable
            # from history. Only a tag whose commit had a *different* version is
            # a real error worth flagging (and we still never move it).
            tagged_ver = version_at(existing, cwd)
            if tagged_ver == ver:
                row.update(status="in-sync", note=f"{tag} -> {short}")
            else:
                row.update(
                    status="MISMATCH",
                    note=f"{tag} -> {short} whose galaxy.yml is {tagged_ver or 'n/a'}, not {ver} — left untouched",
                )
            rows.append(row)
            continue

        # tag missing: decide eligibility
        if git_only:
            row.update(eligible=True, status="missing",
                       note="git-only collection (repo hygiene)")
        elif ver in published:
            row.update(eligible=True, status="missing",
                       note="confirmed on Galaxy")
        else:
            row.update(status="skip",
                       note="not published on Galaxy yet — refusing to tag")

        row["message"] = changelog_section(cwd, ver)
        rows.append(row)

    label = "GitHub-only" if git_only else "Galaxy-published"
    return label, fqcn, rows


def apply_tags(cwd, fqcn, rows, push, gh_release):
    for row in rows:
        if not row.get("eligible"):
            continue
        tag, commit, ver = row["tag"], row["commit"], row["version"]
        msg = row.get("message") or f"Release {tag} ({fqcn})"
        # annotated tag on the introducing commit
        run(["git", "tag", "-a", tag, commit, "-m", f"{fqcn} {tag}\n\n{msg}"], cwd)
        print(f"    created annotated {tag} -> {commit[:9]}")
        if push:
            run(["git", "push", "origin", tag], cwd)
            print(f"    pushed {tag} to origin")
        if gh_release:
            _, code = run(["gh", "release", "view", tag], cwd, check=False)
            if code == 0:
                print(f"    gh release {tag} already exists — skipping")
            else:
                args = ["gh", "release", "create", tag, "--title", f"{fqcn} {tag}"]
                if row.get("message"):
                    args += ["--notes", row["message"]]
                else:
                    args += ["--generate-notes"]
                run(args, cwd)
                print(f"    created GitHub release {tag}")


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def iter_collections(root):
    """Yield every collection dir (contains galaxy.yml) under a tree.

    Accepts the collection dir itself, an `ansible_collections/<ns>/<name>`
    tree, a single namespace dir (`.../<ns>`), or the workspace root — so the
    caller doesn't have to remember which level to point at. Collections sit one
    or two levels below the given path depending on what was passed."""
    root = Path(root)
    if (root / "galaxy.yml").exists():
        yield root
        return
    base = root / "ansible_collections" if (root / "ansible_collections").exists() else root
    found = set()
    for depth in ("*/galaxy.yml", "*/*/galaxy.yml"):
        for galaxy in base.glob(depth):
            found.add(galaxy.parent)
    for cdir in sorted(found):
        yield cdir


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", default=".",
                    help="a collection dir, or a tree to sweep (default: cwd)")
    ap.add_argument("--sweep", action="store_true",
                    help="treat PATH as a tree and process every collection under it")
    ap.add_argument("--all-versions", action="store_true",
                    help="check the full version history, not just the current version")
    ap.add_argument("--apply", action="store_true",
                    help="actually create the missing tags (default: report only)")
    ap.add_argument("--no-push", action="store_true",
                    help="with --apply, create tags locally but don't push")
    ap.add_argument("--github-release", action="store_true",
                    help="with --apply, also create a GitHub release per new tag (needs gh)")
    args = ap.parse_args()

    collections = list(iter_collections(args.path)) if args.sweep \
        else [Path(args.path)]

    if not collections:
        print(f"No collections (galaxy.yml) found under {args.path}")
        return 1

    any_missing = any_mismatch = False
    for cwd in collections:
        if not (cwd / "galaxy.yml").exists():
            print(f"!! {cwd}: no galaxy.yml — skipping")
            continue
        label, fqcn, rows = plan_collection(cwd, only_current=not args.all_versions)
        print(f"\n== {fqcn}  [{label}]  ({cwd})")
        for row in rows:
            marker = {"in-sync": "  ok ", "missing": " ++ ", "MISMATCH": " !! ",
                      "skip": " -- ", "error": " ?? "}.get(row["status"], "    ")
            print(f"  {marker}{row['tag']:<10} {row['status']:<8} {row['note']}")
            any_missing |= row.get("eligible", False)
            any_mismatch |= row["status"] == "MISMATCH"
        if args.apply:
            apply_tags(cwd, fqcn, rows, push=not args.no_push,
                       gh_release=args.github_release)

    if not args.apply and any_missing:
        print("\nRun again with --apply to create the missing tags.")
    if any_mismatch:
        print("\nWARNING: one or more tags point at the wrong commit (see !!).")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
