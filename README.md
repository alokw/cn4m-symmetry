# cn4m-symmetry

A small utility that scans a folder on an interval and mirrors its entire
directory structure into another folder using **hard links** instead of copies.
The mirror looks and behaves like a full copy of the tree, but consumes no
additional disk space — every linked file is the same inode on disk.

## How it works

Every `SCAN_INTERVAL` seconds it walks `SOURCE_DIR` and makes `TARGET_DIR` match
it, using links rather than copies:

1. Recreates each subdirectory as a real (empty) directory — only files are
   linked.
2. Hard links each file into the matching place, skipping anything already
   correctly linked.
3. Holds back files that are still being written, and re-links a file whose
   source was replaced.
4. Recognises a renamed file by its inode and moves the link with it, in the
   same scan.
5. Prunes links whose source is gone, leaves alone anything it did not create,
   and never puts back something you deleted from the target.
6. Keeps out assets cn4m has quarantined, reports what it did on a small status
   page, and tells cn4m when the mirror gains files.

The source directory is only ever read. Every write happens inside
`TARGET_DIR`.


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
| `STATUS_URL` | `http://localhost:2640/suite/status` | cn4m endpoint to notify when new links are made; empty disables. |
| `STATUS_APP` | `symmetry` | `app` field sent with each update. |
| `STATUS_LEVEL` | `working` | `level` field sent with each update. |
| `STATUS_TIMEOUT` | `5` | Seconds to wait on the status endpoint. |
| `WEB_ENABLED` | `true` | Serve the read-only status page. |
| `WEB_PORT` | `2647` | Port for the status page. |
| `WEB_ALLOW_ACTIONS` | `true` | Allow the force-push button; `false` keeps the page read-only. |
| `WEB_MAX_FILES` | `5000` | Most files listed on the status page. |
| `WEBHOOK_TOKEN` | *(empty)* | If set, `/api/rescan` and `/api/restore` require it. Empty means open. |
| `WEBHOOK_DEBOUNCE` | `1` | Seconds to gather a burst of webhook calls into one scan. |
| `STABLE_SECONDS` | `60` | Skip files still being written; link only after size and mtime hold steady this long. `0` disables. |
| `QUARANTINE_FILE` | `assets.json` | Parent-utility manifest in `TARGET_DIR` listing assets to keep out of the mirror. Empty disables. |
| `DRY_RUN` | `false` | Log actions without touching the filesystem. |
| `RUN_ONCE` | `false` | Do one scan and exit. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `WEB_HOST` | `0.0.0.0` | Interface the status page binds to; `127.0.0.1` keeps it local. |
| `ENV_FILE` | *(next to the script)* | Path to the `.env` to read. |
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


## Webhook: scan now

`/api/rescan` starts a scan immediately instead of waiting for the next
interval. It is meant for a file watcher to call the moment it knows a file has
finished arriving:

```sh
curl -X POST http://localhost:2647/api/rescan -d path=1100/new_asset.mov
```

**Pass the path and it links on sight.** This is the important part: a file the
watcher just reported is, by definition, on its first sight to the scanner, so
the normal `STABLE_SECONDS` check would hold it for a full settle window and the
"immediate" scan would link nothing. Naming the path tells the scanner the
watcher has already confirmed the file is complete, so it skips that wait for
those paths only. Everything else in the same scan is treated as usual.

| Call | Effect |
| --- | --- |
| `-d path=a.mov -d path=b.mov` | Scan now; `a.mov` and `b.mov` link on sight. |
| `-d path=1100` | Scan now; everything under `1100/` links on sight. |
| no body | Scan now; new files still wait out `STABLE_SECONDS`. |
| `-d trust_all=1` | Scan now; every file links on sight, this scan only. |

Accepts JSON (`{"paths": [...]}`), form data, or a query string, over POST or
GET — whichever the sender can manage. Relative to `SOURCE_DIR`, forward
slashes.

Calls **coalesce**: `WEBHOOK_DEBOUNCE` (1s) gathers a burst of callbacks into a
single scan with all their paths vouched for, and a call that arrives mid-scan
queues exactly one more. A rescan never undoes a deletion you made in the
target — that is what force push is for.

Set `WEBHOOK_TOKEN` and callers must present it as an `X-Webhook-Token` header,
`Authorization: Bearer`, or `?token=`; the force-push endpoint is then guarded
by the same token. The status page itself stays open. Each scan on the status
page shows what triggered it — `interval`, `webhook`, `force push`, or
`startup` — so you can confirm the watcher is getting through.

## Telling cn4m what happened

Set `STATUS_URL` and every scan that links something posts a one-line update:

```
POST http://<cn4m-host>:2640/suite/status
app=symmetry&message=Discovered+and+linked+5+new+files&level=working
```

Only scans that actually gained links send anything — a quiet scan stays quiet,
so the endpoint sees traffic when there is news rather than once a minute
forever. If files were also re-linked, the message says so:
`Discovered and linked 1 new file, refreshed 1`.

### localhost means the container

`STATUS_URL` defaults to `http://localhost:2640/suite/status`, which is right
when symmetry runs directly on the same machine as cn4m. **Inside Docker,
`localhost` is the container itself**, not the machine running Docker, so that
default cannot reach a cn4m on the host. Use whichever applies:

| Where cn4m runs | `STATUS_URL` |
| --- | --- |
| On the Docker host | `http://host.docker.internal:2640/suite/status` |
| As another container | `http://<its service name>:2640/suite/status` |
| Same machine, no Docker | `http://localhost:2640/suite/status` |

`docker-compose.yml` maps `host.docker.internal` to the host gateway, so the
first form works on Linux as well as Docker Desktop. If a localhost URL fails
from inside a container, the log says all this rather than just reporting a
refused connection.

### When cn4m is not there

A failing endpoint is left alone rather than retried every scan: after a failure
updates pause for 60 seconds, then 2, 4, 8 minutes and so on up to 30, and the
first success resets it. So an unset, wrong, or temporarily down cn4m costs one
attempt and a single log line, not a broken request every minute. An HTTP error
such as a 404 backs off the same way a refused connection does.

Updates go out on a background thread and swallow their errors, so a cn4m that
is slow, down, or not there at all cannot delay or interrupt mirroring. A
failure is logged once at `WARNING`, then at `DEBUG` until it recovers. Setting
`STATUS_URL` empty disables the whole thing. In-flight updates get a moment to
finish on shutdown, which is what makes the update from a `RUN_ONCE` scan — and
from the last scan before `docker stop` — actually arrive.


## Safety

The tool keeps a record of everything it created at
`TARGET_DIR/.symmetry-state.json` — each path, and the inode it linked there —
and it will only ever delete or replace a file that **matches that record
exactly**: the path it wrote down, still pointing at the inode it wrote down.
Nothing is inferred from the filesystem. That rule is what makes the target
safe to work in by hand:

- **Rename one of our links** and the new name is yours: it is not in the
  record, so it is never pruned. The old name is treated as a deletion and not
  pushed back (force push it from the status page if you want both).
- **Make your own hard link** to one of our files, or **copy** a file in, and it
  is left alone — even though a hard link is byte-for-byte indistinguishable
  from one of ours.
- **Replace one of our links with your own file** at the same path and the
  inode no longer matches, so it is yours from then on.

An earlier version guessed that any file with more than one name on disk was
ours, which is exactly wrong for a link you made by renaming one of ours; it
deleted such a file, and the guess is gone.

Delete the state file and the tool forgets it owned anything: it re-links what
is still in the source, reclaiming those as it goes, and leaves every stray
where it is.

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

## Code map

Three files, no framework, and `python-dotenv` is the only dependency — it is
optional at that, with a no-op fallback if it is missing.

| File | What lives there |
| --- | --- |
| `symmetry.py` | Config, state, and the scan itself. |
| `webui.py` | The status page: snapshot object, HTML, and the server. |
| `notify.py` | Pushing status lines to cn4m. |

### Pieces worth lifting into another project

Each of these is written to stand on its own — plain stdlib, no imports from the
rest of the tool unless noted.

| Where | What it does | To reuse |
| --- | --- | --- |
| `notify.py` (whole file) | Fire-and-forget HTTP status pushes with exponential backoff, a one-line-per-outage log, and a shutdown drain so the last update is not lost. | Copy the file. Only `link_message()` at the bottom knows about mirroring. |
| `webui.py:28` `Status` | Lock-guarded snapshot a worker publishes and a web thread reads. | Copy; drop the fields you do not need. |
| `webui.py:436` `start()` | Stdlib HTTP server on a daemon thread, JSON endpoint plus a polling page. | Copy with `_make_handler` above it. |
| `symmetry.py:38-55` `env_bool` / `env_int` / `env_list` | Environment parsing that warns instead of crashing on bad input. | Copy the three functions. |
| `symmetry.py:108` `load_config_file` | Resolves `.env` explicitly, avoiding `load_dotenv()`'s surprising script-relative search. | Copy. |
| `symmetry.py:120` `warn_duplicate_keys` | Warns when a `.env` defines a key twice, since the last one silently wins. | Copy. |
| `symmetry.py:196` `State` | JSON state file written atomically via temp-file rename, tolerating a corrupt or missing file. | Copy; replace the fields. |
| `symmetry.py:363` `StabilityTracker` | Decides when a file has stopped being written, by size **and** mtime across polls rather than mtime age. | Copy; it only needs a stat and a timestamp. |
| `symmetry.py:480-540` `make_link` / `replace_link` | Link creation, and replacement made atomic by linking to a temp name then renaming over. | Copy; both report success so a failure is never mistaken for done. |
| `symmetry.py:852` `measure_target` | Splits a tree into bytes shared with another tree versus bytes genuinely its own, using link counts. | Copy; useful anywhere hard links make `du` lie. |
| `symmetry.py:541` `find_miscased` | Spots an entry differing only in capitalisation, which `os.path.lexists()` cannot on a case-insensitive filesystem. | Copy. |

### Ideas rather than code

Three things here are more approach than snippet, and are the parts that took
the most iteration to get right:

- **Never trust mtime alone to mean "finished writing".** Sync clients preserve
  a file's original timestamp while its data is still arriving, so a
  half-downloaded file can look years old. Compare size too.
- **A record of what you created is what makes cleanup safe.** A hard link is
  indistinguishable from an ordinary file, so without a written record there is
  no way to tell your own leftovers from someone else's data — and no way to
  tell a deletion from something never created.
- **Only claim an action once it has actually happened.** Recording an intent
  before confirming the result is what turns one transient failure into
  permanent state, which is why the link helpers return a success flag.
