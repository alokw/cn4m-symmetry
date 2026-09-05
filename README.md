# cn4m-symmetry

A small utility that scans a folder on an interval and mirrors its entire
directory structure into another folder using **hard links** instead of copies.
The mirror looks and behaves like a full copy of the tree, but consumes no
additional disk space — every linked file is the same inode on disk.

## How it works

Every `SCAN_INTERVAL` seconds it:

1. Walks `SOURCE_DIR` recursively.
2. Recreates each subdirectory under `TARGET_DIR` (real directories — only
   files get linked).
3. Hard links each file into the matching spot in `TARGET_DIR`.
4. Skips anything already correctly linked (same inode), re-links files whose
   source was replaced, and prunes links whose source is gone.

## Quick start

```sh
cp .env.example .env
$EDITOR .env          # set HOST_DATA_DIR, SOURCE_DIR, TARGET_DIR
docker compose up -d
docker compose logs -f
```

Or without Docker:

```sh
pip install -r requirements.txt
python symmetry.py
```

## Configuration

All settings come from the `.env` file (or plain environment variables).

| Variable | Default | Description |
| --- | --- | --- |
| `SOURCE_DIR` | `/data/source` | Folder to scan. |
| `TARGET_DIR` | `/data/target` | Folder that receives the mirrored tree. |
| `SCAN_INTERVAL` | `60` | Seconds between scans. |
| `LINK_MODE` | `hard` | `hard` or `symlink`. |
| `PRUNE` | `true` | Delete links whose source file is gone. |
| `INCLUDE_HIDDEN` | `true` | Include dotfiles and dot-directories. |
| `EXCLUDE_PATTERNS` | *(empty)* | Comma-separated globs to skip, e.g. `*.tmp,.git,@eaDir`. |
| `RESPECT_DELETIONS` | `true` | Never re-create a file or folder you deleted from the target. |
| `STATE_FILE` | *(empty)* | Where to keep the bookkeeping; empty means `TARGET_DIR/.symmetry-state.json`. |
| `WEB_ENABLED` | `true` | Serve the read-only status page. |
| `WEB_PORT` | `2647` | Port for the status page. |
| `WEB_ALLOW_ACTIONS` | `true` | Allow the force-push button; `false` keeps the page read-only. |
| `WEB_MAX_FILES` | `5000` | Most files listed on the status page. |
| `STABLE_SECONDS` | `60` | Skip files still being written; link only after size and mtime hold steady this long. `0` disables. |
| `QUARANTINE_FILE` | `assets.json` | Parent-utility manifest in `TARGET_DIR` listing assets to keep out of the mirror. Empty disables. |
| `DRY_RUN` | `false` | Log actions without touching the filesystem. |
| `RUN_ONCE` | `false` | Do one scan and exit. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `HOST_DATA_DIR` | `/mnt/pool` | Host path mounted at `/data` (compose only). |

## Mapping your folders

The source and target can be any two separate folders — they don't need to be
related or adjacent. Mount the **closest directory that contains both**, then
point `SOURCE_DIR` and `TARGET_DIR` at subfolders inside it:

```yaml
# host:  /mnt/pool/media/tv  ->  /mnt/pool/apps/jellyfin/library
volumes:
  - /mnt/pool:/data       # one mount, the common ancestor of both
```
```ini
SOURCE_DIR=/data/media/tv
TARGET_DIR=/data/apps/jellyfin/library
```

What does *not* work, even though both folders sit on the same disk:

```yaml
volumes:
  - /mnt/pool/media/tv:/source        # two separate mounts
  - /mnt/pool/apps/jellyfin:/target   # -> every link fails with EXDEV
```

The kernel compares the *mount*, not the device — `do_linkat()` returns
`EXDEV` whenever the two paths arrive via different mounts, so a hard link can
never cross from one bind mount to another. Verified: two bind mounts of the
same filesystem (`device=171` on both sides) still refuse to link.

| Your situation | What to do |
| --- | --- |
| Both folders on the same disk | Mount their common ancestor; `LINK_MODE=hard`. |
| Folders on different disks | `LINK_MODE=symlink` — hard links cannot span filesystems. |
| Not running in Docker | Same-filesystem rule still applies; mounts are not a concern. |

## Hard links: the rules that bite

- **Same filesystem only.** A hard link is a second name for an existing inode,
  so source and target must be on the same filesystem. Cross-device attempts
  fail with `EXDEV`; the log says so explicitly. Use `LINK_MODE=symlink` if the
  two folders are on different disks.
- **Same *mount point*, in Docker.** Linux refuses hard links across separate
  bind mounts even when they sit on the same disk. That's why
  `docker-compose.yml` mounts a single parent directory at `/data` and points
  both `SOURCE_DIR` and `TARGET_DIR` at subfolders inside it. Two separate
  `-v` mounts will not work.
- **Editing one edits both.** Both paths are the same file. Deleting one path
  is safe (the data survives until the last link is gone), but writing through
  either path changes the content both sides see.
- **Directories are never hard linked** — they're recreated as real (empty)
  directories, which is what makes this a virtual mirror rather than a bind
  mount.
- **Symlinks in the source are skipped** to avoid duplicating link chains.

## Deleting things from the target

With `RESPECT_DELETIONS=true` (the default) the mirror is a **one-time push per
file**, not a continuous re-sync. Delete a file or a whole folder from
`TARGET_DIR` and it stays deleted: the path is recorded and no later scan will
put it back. The source is never touched, so the original is always still there.

This works because the state file already records every path we created. A
missing target file is therefore not ambiguous — either we linked it before, in
which case you removed it, or we never did, in which case it is simply new.

- **Put something back by hand** and it is managed again from the next scan.
  Recreating a deleted folder restores its contents too, rather than leaving an
  empty folder with no way to refill it.
- **A link that failed** is not mistaken for a deletion. Only paths we
  successfully linked are eligible, so a failed link is retried next scan.
- **To re-push everything**, delete `.symmetry-state.json` from the target.
- **Wiping the whole target** is read as a reset rather than a mass deletion:
  the scan logs a warning and re-links, instead of writing the mirror off
  permanently.

Deletions are counted as `left_deleted` in the scan summary and on the status
page. Set `RESPECT_DELETIONS=false` to go back to re-pushing whatever is
missing.

## Status page

With `WEB_ENABLED=true` (the default) the container serves a small read-only
page at **http://localhost:2647/**, showing:

- **Disk usage split in two** — how much of the target is shared with the source
  and therefore costs nothing, versus how much is genuinely standalone. This is
  the number Explorer gets wrong: it sums both and reports the total, because it
  counts hard-linked blocks once per name.
- **The active configuration**, as the running process resolved it — useful for
  confirming the container sees the paths you think it does.
- **A log of recent scans**, each with a timestamp, duration, and counts for
  linked / relinked / unchanged / pruned / quarantined / deferred / errors.
- **Every file in the source**, with what became of it: mirrored, still
  arriving, quarantined, excluded, errored, or **deleted in target** for the
  ones being deliberately left out. Filter by path, or click a state to show
  only those.

### Force push

Files flagged *deleted in target* carry a **Force push** button, with a
**Force push all deleted** alongside it. Pressing one forgets that deletion and
wakes the scanner immediately, so the file is linked again within a second
rather than at the next interval.

This is the one thing on the page that changes anything, and its blast radius is
deliberately tiny: it only removes entries from the remembered-deletions list. It
cannot delete, overwrite, or reach outside `SOURCE_DIR`, and an unknown path is a
no-op — so there is nothing for a malformed or hostile request to damage. Even
so, it is an unauthenticated write, so set `WEB_ALLOW_ACTIONS=false` if the port
is reachable by anyone you would not hand the button to.

It refreshes every five seconds and is backed by `/api/status`, which returns
the same data as JSON if you want to script against it.

The page is served by the standard library in a daemon thread — no framework,
no extra dependencies, and a slow request can never hold up a scan. It is
read-only: nothing on it can change the configuration or touch a file. It also
has **no authentication**, so only publish the port on a network you trust.
Set `WEB_ENABLED=false` to turn it off, or change `WEB_PORT` to move it.

The usage figures come from one `lstat` per target file after each scan, so
they cost about the same as the pruning walk.

## Files that are still arriving

A file is only linked once its **size and mtime have both held steady for
`STABLE_SECONDS`**, compared across scans. Anything still growing — a download
in progress, a large copy — is left alone and reported as `deferred` in the scan
summary, then linked on the first scan after it settles.

Size is compared as well as mtime because sync clients, Dropbox among them,
preserve a file's *original* timestamp while its data is still arriving. An
mtime age check alone would call a half-downloaded file finished; a size check
catches it.

**Renames are exempt.** A renamed file is recognised by its inode as content
already mirrored — and we only ever link a file once it is complete — so it is
re-linked under the new name in the *same* scan that prunes the old one. Without
this a rename would drop out of the mirror and serve the whole settle period
again, leaving the file missing from the target for a scan or two. The same
applies to renamed directories, and to a rename that only changes
capitalisation: on a case-insensitive filesystem the mirror is re-cased to match
the source rather than keeping the old spelling.

Two consequences worth knowing:

- **A brand-new file waits one scan.** With nothing to compare against on first
  sight, the earliest it can link is the following scan. Lower `SCAN_INTERVAL`
  to shorten that, or set `STABLE_SECONDS=0` to link immediately.
- **An existing link is kept while a replacement settles.** If a mirrored file
  is replaced by one still being written, the old link stays in place (and is
  not pruned) until the new version is stable, then gets swapped. The target
  never contains a half-written file, and never briefly loses the good one.

Observations are recorded in the state file, so waiting survives a restart and
works in `RUN_ONCE` mode, where each scan is a separate process. Files that are
already correctly linked are never re-examined, so a settled mirror does no
extra work.

If your writers use temporary names (`.part`, `.crdownload`, `.tmp`), adding
them to `EXCLUDE_PATTERNS` skips them outright and complements this check.

## Quarantined assets

If the parent utility (cn4m) drops an `assets.json` alongside the state file in
`TARGET_DIR`, every asset it lists under `untracked_quar_assets` or
`tracked_quar_assets` is treated as quarantined:

- it is never linked into the mirror, and
- if an earlier scan already linked it, that link is removed.

Removal ignores `PRUNE` — a quarantined asset should not remain in the target
either way — and only ever unlinks inside `TARGET_DIR`, so **the source file is
never touched**. Clear an asset out of those buckets and the next scan links it
back in.

Entries are matched on the `parent` and `name` fields rather than the `folder`
path, since `folder` is expressed in the parent utility's own namespace
(`/cn4m_assets/repo/1100`). Matching the pair also keeps working if the mirrored
tree is deeper than one level. A missing, malformed, or unreadable `assets.json`
means "nothing quarantined": the file is skipped with a warning and mirroring
carries on, rather than a broken manifest stalling the sync. `assets.json`
itself is never pruned from the target.

## Safety

The tool keeps a record of everything it created at
`TARGET_DIR/.symmetry-state.json`, and `PRUNE` only deletes paths listed there
(or paths that still carry a link's signature — a symlink, or a file with more
than one name on disk). This matters because once a source file is deleted, the
hard link left behind in the target is indistinguishable from an ordinary file;
without the record there'd be no safe way to tell the leftovers apart from your
own data. Anything unrecognised is left where it is.

Delete the state file and the tool simply forgets it owned those links — it will
re-link what's still in the source and leave the strays alone.

It also refuses to start if `SOURCE_DIR` and `TARGET_DIR` are the same folder or
nested inside one another. Use `DRY_RUN=true` for a first run to see exactly
what it would do.

## Verified behaviour

Tested in the container against a Linux filesystem: hard links share the source
inode (`stat` shows one inode, link count 2), repeat scans are no-ops, deleted
sources are pruned, a replaced source file is re-linked to its new inode,
symlinks in the source are skipped, pre-existing files in the target are never
touched, and `SIGTERM` from `docker stop` exits cleanly mid-interval.

Running the script directly on Windows works for `LINK_MODE=hard` on NTFS;
`LINK_MODE=symlink` needs Developer Mode or an elevated shell, which is a
Windows privilege rule rather than a limitation of the tool.
