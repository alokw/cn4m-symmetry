#!/usr/bin/env python3
"""cn4m-symmetry: mirror a directory tree using hard links instead of copies.

Walks SOURCE_DIR on an interval, recreates its directory structure under
TARGET_DIR, and links every file into place. Hard links share the same inode
as the original, so the mirror costs no additional disk space.
"""

import fnmatch
import json
import logging
import os
import re
import signal
import sys
import time
from pathlib import Path

import webui

try:
    from dotenv import load_dotenv
except ImportError:  # dotenv is optional; plain env vars work fine
    def load_dotenv(*_args, **_kwargs):
        return False

log = logging.getLogger("symmetry")

TRUTHY = {"1", "true", "yes", "on"}
EXDEV = 18
STATE_FILENAME = ".symmetry-state.json"

# Buckets in the parent utility's assets.json holding quarantined assets.
QUARANTINE_KEYS = ("untracked_quar_assets", "tracked_quar_assets")


def env_bool(name, default):
    return os.getenv(name, str(default)).strip().lower() in TRUTHY


def env_int(name, default):
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("%s=%r is not an integer, using %d", name, raw, default)
        return default


def env_list(name):
    return [p.strip() for p in os.getenv(name, "").split(",") if p.strip()]


class Config:
    def __init__(self):
        self.source = Path(os.getenv("SOURCE_DIR", "/data/source"))
        self.target = Path(os.getenv("TARGET_DIR", "/data/target"))
        self.interval = max(1, env_int("SCAN_INTERVAL", 60))
        self.link_mode = os.getenv("LINK_MODE", "hard").strip().lower()
        self.prune = env_bool("PRUNE", True)
        self.include_hidden = env_bool("INCLUDE_HIDDEN", True)
        self.exclude = env_list("EXCLUDE_PATTERNS")
        self.quarantine_file = os.getenv("QUARANTINE_FILE", "assets.json").strip()
        self.stable_seconds = max(0, env_int("STABLE_SECONDS", 60))
        self.respect_deletions = env_bool("RESPECT_DELETIONS", True)
        self.state_file = os.getenv("STATE_FILE", "").strip()
        self.web_enabled = env_bool("WEB_ENABLED", True)
        self.web_host = os.getenv("WEB_HOST", "0.0.0.0").strip()
        self.web_port = env_int("WEB_PORT", 2647)
        self.web_allow_actions = env_bool("WEB_ALLOW_ACTIONS", True)
        self.web_max_files = max(0, env_int("WEB_MAX_FILES", 5000))
        self.run_once = env_bool("RUN_ONCE", False)
        self.dry_run = env_bool("DRY_RUN", False)
        self.log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()

    def validate(self):
        if self.link_mode not in ("hard", "symlink"):
            raise SystemExit("LINK_MODE must be 'hard' or 'symlink', got %r" % self.link_mode)
        if not self.source.is_dir():
            raise SystemExit(
                "SOURCE_DIR does not exist or is not a directory: %s%s"
                % (self.source, path_hint(self.source))
            )

        source = self.source.resolve()
        target = self.target.resolve()
        if source == target:
            raise SystemExit("SOURCE_DIR and TARGET_DIR must not be the same directory")
        if _is_within(target, source) or _is_within(source, target):
            raise SystemExit(
                "SOURCE_DIR (%s) and TARGET_DIR (%s) must not be nested inside each other"
                % (source, target)
            )
        self.source, self.target = source, target


def load_config_file():
    """Load the .env next to this script, or wherever ENV_FILE points.

    Resolved explicitly rather than left to load_dotenv()'s default search,
    which picks a file relative to the script while everything else in the
    process works from the current directory.
    """
    env_file = os.getenv("ENV_FILE") or str(Path(__file__).resolve().parent / ".env")
    load_dotenv(env_file)
    return env_file


def warn_duplicate_keys(path):
    """A key defined twice in .env silently takes its LAST value.

    Both this script and docker compose read the file that way, so a stale
    leftover line further down quietly overrides the value you just edited.
    """
    seen, dupes = set(), []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key = line.split("=", 1)[0].strip()
                (dupes.append(key) if key in seen else seen.add(key))
    except OSError:
        return
    for key in dict.fromkeys(dupes):
        log.warning(
            "%s defines %s more than once - the LAST definition wins, "
            "which may not be the one you edited", path, key,
        )


def path_hint(path):
    """Explain the two ways a configured path usually goes wrong."""
    text = str(path)
    if any(c in text for c in "\t\n\r\v\f\a\b"):
        # python-dotenv expands backslash escapes inside double-quoted values,
        # so TARGET_DIR="M:\test" arrives as 'M:<TAB>est'.
        return (
            "\n  Hint: that path contains a control character. A Windows path in "
            "double quotes has its backslash escapes expanded by .env parsing "
            '("M:\\test" becomes M:<TAB>est). Leave the value unquoted, or use '
            "forward slashes: M:/test"
        )
    if os.name == "posix" and re.match(r"^[A-Za-z]:[\\/]", text):
        return (
            "\n  Hint: that is a Windows path, but this process is running on Linux "
            "- almost certainly inside the container, which has no drive letters. "
            "Set SOURCE_DIR/TARGET_DIR to paths INSIDE the container (e.g. "
            "/data/test) and bind-mount the Windows folder onto /data in "
            "docker-compose.yml. To use Windows paths directly, run the script on "
            "the host instead: python symmetry.py"
        )
    return ""


def ensure_target(cfg):
    """Create TARGET_DIR, and any missing parents, if it isn't there yet."""
    if cfg.target.is_dir():
        return
    if os.path.lexists(cfg.target):
        raise SystemExit(
            "TARGET_DIR already exists but is not a directory: %s" % cfg.target
        )
    if cfg.dry_run:
        log.info("[dry-run] would create TARGET_DIR %s", cfg.target)
        return
    try:
        cfg.target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SystemExit(
            "cannot create TARGET_DIR %s: %s%s" % (cfg.target, exc, path_hint(cfg.target))
        )
    log.info("created TARGET_DIR %s", cfg.target)


def _is_within(child, parent):
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


class State:
    """Record of the links and directories we created under TARGET_DIR.

    Pruning is driven by this file rather than by inspecting the filesystem: a
    hard link whose source has been deleted is indistinguishable from an
    ordinary file, so without a record we could never safely clean it up.
    """

    def __init__(self, path):
        self.path = path
        self.files = set()
        self.dirs = set()
        self.pending = {}  # unstable files being watched, see StabilityTracker
        self.inodes = {}  # posix path -> (dev, ino) of each link we hold
        self.known_inodes = {}  # (dev, ino) -> posix path, for spotting renames
        # Paths we linked once and the user has since removed from the target.
        # Remembering them is what stops the next scan putting them back.
        self.deleted = set()
        self.deleted_dirs = set()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("ignoring unreadable state file %s: %s", self.path, exc)
            return
        self.files = {Path(p) for p in data.get("files", [])}
        self.dirs = {Path(p) for p in data.get("dirs", [])}
        pending = data.get("pending", {})
        if isinstance(pending, dict):
            self.pending = {
                key: tuple(value) for key, value in pending.items()
                if isinstance(value, list) and len(value) == 3
            }
        inodes = data.get("inodes", {})
        if isinstance(inodes, dict):
            self.inodes = {
                key: tuple(value) for key, value in inodes.items()
                if isinstance(value, list) and len(value) == 2
            }
            self.known_inodes = {v: k for k, v in self.inodes.items()}
        self.deleted = set(data.get("deleted", []) or [])
        self.deleted_dirs = set(data.get("deleted_dirs", []) or [])
        log.debug(
            "loaded state: %d files, %d dirs, %d pending, %d deleted",
            len(self.files), len(self.dirs), len(self.pending),
            len(self.deleted) + len(self.deleted_dirs),
        )

    def save(self, files, dirs, pending=None, inodes=None, deleted=None, deleted_dirs=None):
        self.files, self.dirs = set(files), set(dirs)
        self.pending = dict(pending or {})
        self.inodes = dict(inodes or {})
        self.known_inodes = {v: k for k, v in self.inodes.items()}
        self.deleted = set(deleted or ())
        self.deleted_dirs = set(deleted_dirs or ())
        payload = {
            "version": 1,
            "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "files": sorted(p.as_posix() for p in files),
            "dirs": sorted(p.as_posix() for p in dirs),
            "pending": {k: list(v) for k, v in sorted(self.pending.items())},
            "inodes": {k: list(v) for k, v in sorted(self.inodes.items())},
            "deleted": sorted(self.deleted),
            "deleted_dirs": sorted(self.deleted_dirs),
        }
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=1)
            os.replace(tmp, self.path)
        except OSError as exc:
            log.error("cannot write state file %s: %s", self.path, exc)


def load_quarantine(cfg):
    """Read the parent utility's assets.json, if it is there.

    Returns the set of (parent folder name, file name) pairs that cn4m has
    quarantined. Matching on the pair rather than the full path keeps working
    when the mirrored tree is deeper than one level. A missing or unreadable
    file simply means "nothing quarantined" - a broken assets.json must not
    stop the mirror from running.
    """
    if not cfg.quarantine_file:
        return set()
    path = cfg.target / cfg.quarantine_file
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return set()
    except (OSError, ValueError) as exc:
        log.warning("ignoring unreadable %s: %s", path, exc)
        return set()
    if not isinstance(data, dict):
        log.warning("ignoring %s: expected a JSON object at the top level", path)
        return set()

    quarantined = set()
    for key in QUARANTINE_KEYS:
        bucket = data.get(key) or {}
        entries = bucket.values() if isinstance(bucket, dict) else bucket
        try:
            entries = list(entries)
        except TypeError:
            log.warning("ignoring %s in %s: not a list or object", key, path)
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if name:
                quarantined.add((str(entry.get("parent") or ""), str(name)))

    log.debug("%s lists %d quarantined asset(s)", path, len(quarantined))
    return quarantined


def is_quarantined(quarantined, rel_path):
    """Match a mirrored path against the quarantine list by (parent, name)."""
    if not quarantined:
        return False
    return (rel_path.parent.name, rel_path.name) in quarantined


def quarantine_candidates(cfg, state, quarantined):
    """Paths that may need removing: what we know we linked, plus the direct
    parent/name each quarantine entry implies."""
    candidates = {p for p in state.files if is_quarantined(quarantined, p)}
    for parent, name in quarantined:
        candidates.add(Path(parent) / name if parent else Path(name))
    return candidates


def remove_quarantined(cfg, state, quarantined, stats):
    """Delete mirrored links for assets cn4m has quarantined.

    Runs regardless of PRUNE - a quarantined asset must not stay in the target.
    Only ever unlinks inside TARGET_DIR, so the source file is untouched.
    """
    for rel_path in sorted(quarantine_candidates(cfg, state, quarantined)):
        dst = cfg.target / rel_path
        if not os.path.lexists(dst):
            continue
        if not is_ours(cfg, state, rel_path, dst):
            log.warning(
                "quarantined asset %s is not one of our links, leaving it alone", dst
            )
            continue
        if cfg.dry_run:
            log.info("[dry-run] remove quarantined %s", dst)
            stats.quarantined += 1
            continue
        try:
            os.unlink(dst)
        except OSError as exc:
            stats.errors += 1
            log.error("cannot remove quarantined %s: %s", dst, exc)
        else:
            stats.quarantined += 1
            log.info("removed quarantined asset %s", dst)


class StabilityTracker:
    """Hold back files that are still being written.

    A file counts as stable once its size and mtime have both stayed identical
    for STABLE_SECONDS. Size is compared as well as mtime because sync clients
    - Dropbox among them - preserve a file's original timestamp while its data
    is still arriving, so an mtime age check alone would call a half-downloaded
    file finished. Comparing across scans costs nothing: the scan interval is
    already the waiting period.

    Observations live in the state file rather than in memory, so they survive
    both a restart and RUN_ONCE mode, where every scan is a new process.
    """

    def __init__(self, settle, pending=None):
        self.settle = settle
        # posix path -> (size, mtime, first seen at). Wall clock, not monotonic,
        # so the timestamps still mean something in the next process.
        self.seen = dict(pending or {})
        self.logged = set()
        self._touched = set()

    def is_stable(self, rel_path, src_stat, now):
        if self.settle <= 0:
            return True
        key = rel_path.as_posix()
        self._touched.add(key)
        previous = self.seen.get(key)
        if previous is None or (previous[0], previous[1]) != (
            src_stat.st_size, src_stat.st_mtime
        ):
            # New, or changed since last look: restart the clock.
            self.seen[key] = (src_stat.st_size, src_stat.st_mtime, now)
            return False
        return max(0.0, now - previous[2]) >= self.settle

    def first_defer(self, rel_path):
        """True the first time a path is held back, to keep logging quiet."""
        key = rel_path.as_posix()
        if key in self.logged:
            return False
        self.logged.add(key)
        return True

    def settled(self, rel_path):
        key = rel_path.as_posix()
        self.seen.pop(key, None)
        self.logged.discard(key)

    def sweep(self):
        """Drop anything not looked at this scan, so the map cannot grow."""
        for key in set(self.seen) - self._touched:
            self.seen.pop(key, None)
            self.logged.discard(key)
        self._touched.clear()

    def export(self):
        return self.seen


class Stats:
    __slots__ = (
        "linked", "relinked", "skipped", "pruned", "quarantined", "deferred",
        "left_deleted", "errors",
    )

    def __init__(self):
        self.linked = self.relinked = self.skipped = self.pruned = 0
        self.quarantined = self.deferred = self.left_deleted = self.errors = 0

    def __str__(self):
        text = (
            "linked=%d relinked=%d unchanged=%d pruned=%d errors=%d"
            % (self.linked, self.relinked, self.skipped, self.pruned, self.errors)
        )
        if self.quarantined:
            text += " quarantined=%d" % self.quarantined
        if self.deferred:
            text += " deferred=%d" % self.deferred
        if self.left_deleted:
            text += " left_deleted=%d" % self.left_deleted
        return text


def is_excluded(cfg, name, rel_path):
    if not cfg.include_hidden and name.startswith("."):
        return True
    posix = rel_path.as_posix()
    return any(fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(posix, pat) for pat in cfg.exclude)


def same_hard_link(src_stat, dst_path):
    """True if dst_path is already the same inode as the source file."""
    try:
        dst_stat = os.lstat(dst_path)
    except OSError:
        return False
    return dst_stat.st_dev == src_stat.st_dev and dst_stat.st_ino == src_stat.st_ino


def is_ours(cfg, state, rel_path, dst):
    """True if we may replace or delete dst.

    Either we recorded creating it, or it still carries the signature of a link
    we would have made (a symlink, or a file with more than one name on disk).
    """
    if rel_path in state.files:
        return True
    try:
        st = os.lstat(dst)
    except OSError:
        return False
    if os.path.islink(dst):
        return True
    return cfg.link_mode == "hard" and st.st_nlink > 1


def make_link(cfg, src, dst, stats):
    """Create the link. Returns True only if one now exists."""
    if cfg.dry_run:
        log.info("[dry-run] link %s -> %s", dst, src)
        stats.linked += 1
        return True
    try:
        if cfg.link_mode == "hard":
            os.link(src, dst)
        else:
            os.symlink(src, dst)
    except FileExistsError:
        stats.skipped += 1
        return True  # something is already there; a race, not a failure
    except OSError as exc:
        stats.errors += 1
        if getattr(exc, "errno", None) == EXDEV:
            log.error(
                "%s: cross-device link. SOURCE_DIR and TARGET_DIR are on different "
                "filesystems - set LINK_MODE=symlink, or mount both from the same volume.",
                src,
            )
        else:
            log.error("failed to link %s -> %s: %s", dst, src, exc)
        return False
    stats.linked += 1
    log.debug("linked %s -> %s", dst, src)
    return True


def replace_link(cfg, src, dst, stats):
    """Atomically point dst at the current src (link then rename over).

    Returns True only if the replacement actually landed.
    """
    if cfg.dry_run:
        log.info("[dry-run] replace %s -> %s", dst, src)
        stats.relinked += 1
        return True
    tmp = dst.with_name(dst.name + ".symmetry-tmp")
    try:
        if os.path.lexists(tmp):
            os.unlink(tmp)
        if cfg.link_mode == "hard":
            os.link(src, tmp)
        else:
            os.symlink(src, tmp)
        os.replace(tmp, dst)
    except OSError as exc:
        stats.errors += 1
        log.error("failed to refresh %s -> %s: %s", dst, src, exc)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False
    stats.relinked += 1
    log.debug("relinked %s -> %s", dst, src)
    return True


def find_miscased(dst_names, wanted):
    """The existing entry that differs from `wanted` only in capitalisation.

    On a case-insensitive filesystem os.path.lexists() happily matches the old
    spelling after a case-only rename, so the mirror would keep the stale name
    forever unless we compare against the real directory entries.
    """
    if dst_names is None or wanted in dst_names:
        return None
    folded = wanted.lower()
    for name in dst_names:
        if name.lower() == folded:
            return name
    return None


def sync_file(cfg, state, tracker, inodes, rel_path, src, dst, stats, dst_names=None):
    """Link src into dst. Returns what happened, as a short status string."""
    try:
        src_stat = os.stat(src)
    except OSError as exc:
        stats.errors += 1
        log.error("cannot stat %s: %s", src, exc)
        return "error"

    key = rel_path.as_posix()
    identity = (src_stat.st_dev, src_stat.st_ino)
    exists = os.path.lexists(dst)

    miscased = find_miscased(dst_names, rel_path.name) if exists else None
    if miscased is not None:
        stale = dst.with_name(miscased)
        stale_rel = rel_path.parent / miscased
        if not is_ours(cfg, state, stale_rel, stale):
            stats.errors += 1
            log.warning("refusing to re-case %s: it is not one of our links", stale)
            return "error"
        if cfg.dry_run:
            log.info("[dry-run] re-case %s -> %s", miscased, rel_path.name)
            return "unchanged"
        try:
            os.unlink(stale)
        except OSError as exc:
            stats.errors += 1
            log.error("cannot remove miscased %s: %s", stale, exc)
            return "error"
        log.info("re-casing %s -> %s", miscased, rel_path.name)
        exists = False  # fall through and create it under the right name
    if exists:
        # Already correctly linked: nothing to do, and nothing to watch.
        if cfg.link_mode == "hard":
            if same_hard_link(src_stat, dst):
                stats.skipped += 1
                tracker.settled(rel_path)
                inodes[key] = identity
                return "unchanged"
        elif os.path.islink(dst):
            try:
                if os.readlink(dst) == str(src):
                    stats.skipped += 1
                    tracker.settled(rel_path)
                    inodes[key] = identity
                    return "unchanged"
            except OSError:
                pass

    # A file we already mirror turning up under a new name is a rename, not new
    # data: we only ever linked it once it was complete, so there is nothing to
    # wait for. Without this a rename would drop out of the mirror until it had
    # served the full settle period all over again.
    renamed_from = state.known_inodes.get(identity) if cfg.link_mode == "hard" else None
    if renamed_from is not None and renamed_from != key:
        log.debug("%s is a rename of %s, linking without waiting", key, renamed_from)
    # From here we intend to write a link, so make sure the source has finished
    # arriving first. An existing link is left in place while we wait.
    elif not tracker.is_stable(rel_path, src_stat, time.time()):
        stats.deferred += 1
        message = "still changing, waiting for it to finish: %s (%d bytes)"
        if tracker.first_defer(rel_path):
            log.info(message, rel_path.as_posix(), src_stat.st_size)
        else:
            log.debug(message, rel_path.as_posix(), src_stat.st_size)
        return "deferred"

    if not exists:
        if not make_link(cfg, src, dst, stats):
            return "error"
        tracker.settled(rel_path)
        inodes[key] = identity
        return "linked"

    if not is_ours(cfg, state, rel_path, dst):
        stats.errors += 1
        log.warning("refusing to replace %s: it is a pre-existing file, not one of our links", dst)
        return "error"
    if not replace_link(cfg, src, dst, stats):
        return "error"
    tracker.settled(rel_path)
    inodes[key] = identity
    return "relinked"


def sync_tree(cfg, state, tracker, quarantined, stats, honor_deletions=False,
              inventory=None):
    """Mirror source into target; returns what we now own and what stays deleted.

    `inventory`, when given, is filled with one entry per source file describing
    what became of it - that is what the status page lists.
    """
    files, dirs, inodes = set(), set(), {}
    deleted, deleted_dirs = set(state.deleted), set(state.deleted_dirs)
    tracked = {p.as_posix() for p in state.files}

    def note(key, status, src=None, size=None):
        if inventory is None:
            return
        if size is None:
            try:
                size = os.stat(src).st_size if src is not None else 0
            except OSError:
                size = 0
        inventory.append({"path": key, "status": status, "size": size})

    for root, dirnames, filenames in os.walk(cfg.source, followlinks=False):
        root_path = Path(root)
        rel_root = root_path.relative_to(cfg.source)

        dirnames[:] = sorted(d for d in dirnames if not is_excluded(cfg, d, rel_root / d))

        dst_root = cfg.target / rel_root
        root_key = rel_root.as_posix()

        if dst_root.is_dir():
            if root_key in deleted_dirs:
                # Recreating the folder undoes the whole deletion, contents
                # included - otherwise it would sit there permanently empty.
                deleted_dirs.discard(root_key)
                prefix = root_key + "/"
                deleted.difference_update(
                    {k for k in deleted if k.startswith(prefix)}
                )
                log.info("folder %s is back in the target, managing it again", root_key)
        elif (
            honor_deletions
            and rel_root != Path(".")
            and (root_key in deleted_dirs or rel_root in state.dirs)
            and dst_root.parent.is_dir()
        ):
            # The folder was mirrored before and is gone now: the user removed
            # it. Leave it out, and do not walk into it looking for its files.
            if root_key not in deleted_dirs:
                deleted_dirs.add(root_key)
                deleted.update(k for k in tracked if k.startswith(root_key + "/"))
                log.info("folder %s was deleted from the target, leaving it out", root_key)
            stats.left_deleted += 1
            for entry in sorted(filenames):
                note((rel_root / entry).as_posix(), "deleted", root_path / entry)
            dirnames[:] = []
            continue

        if not dst_root.is_dir():
            if cfg.dry_run:
                log.info("[dry-run] mkdir %s", dst_root)
            else:
                try:
                    dst_root.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    stats.errors += 1
                    log.error("cannot create %s: %s", dst_root, exc)
                    dirnames[:] = []
                    continue

        # Real entries in the mirrored directory, for exact-case comparison.
        try:
            dst_names = set(os.listdir(dst_root))
        except OSError:
            dst_names = None

        if rel_root != Path("."):
            dirs.add(rel_root)

        for name in sorted(filenames):
            rel_path = rel_root / name
            if is_excluded(cfg, name, rel_path):
                note(rel_path.as_posix(), "excluded", root_path / name)
                continue
            if is_quarantined(quarantined, rel_path):
                log.debug("skipping quarantined asset: %s", rel_path.as_posix())
                note(rel_path.as_posix(), "quarantined", root_path / name)
                continue
            src = root_path / name
            if os.path.islink(src):
                log.debug("skipping symlink in source: %s", src)
                note(rel_path.as_posix(), "symlink", src)
                continue

            key = rel_path.as_posix()
            dst = cfg.target / rel_path
            if honor_deletions:
                present = os.path.lexists(dst)
                if key in deleted:
                    if not present:
                        stats.left_deleted += 1
                        note(key, "deleted", src)
                        continue
                    deleted.discard(key)  # put back by hand: manage it again
                    log.info("%s is back in the target, managing it again", key)
                elif key in tracked and not present:
                    # We linked this once and it is gone: the user deleted it.
                    deleted.add(key)
                    stats.left_deleted += 1
                    log.info("%s was deleted from the target, leaving it out", key)
                    note(key, "deleted", src)
                    continue

            outcome = sync_file(
                cfg, state, tracker, inodes, rel_path, src, dst, stats, dst_names,
            )
            note(key, outcome, src)
            deferred = outcome == "deferred"
            # Only claim a path once a link really exists for it. A link that
            # failed stays unclaimed so the next scan retries it rather than
            # mistaking the absence for a deletion.
            if key in inodes:
                files.add(rel_path)
            elif deferred and rel_path in state.files:
                files.add(rel_path)  # keep the older link from being pruned

    return files, dirs, inodes, deleted, deleted_dirs


def prune_tree(cfg, state, files, dirs, stats):
    """Remove links and empty directories in target that no longer exist in source."""
    for root, _dirnames, filenames in os.walk(cfg.target, topdown=False, followlinks=False):
        root_path = Path(root)
        rel_root = root_path.relative_to(cfg.target)

        for name in filenames:
            rel_path = rel_root / name
            if rel_path in files:
                continue
            # Control files that live in the target and are not ours to manage.
            if rel_root == Path(".") and (
                name.startswith(STATE_FILENAME)
                or (cfg.quarantine_file and name == cfg.quarantine_file)
            ):
                continue
            dst = root_path / name
            if not is_ours(cfg, state, rel_path, dst):
                log.debug("leaving %s in place: not one of our links", dst)
                continue
            if cfg.dry_run:
                log.info("[dry-run] remove %s", dst)
                stats.pruned += 1
                continue
            try:
                os.unlink(dst)
            except OSError as exc:
                stats.errors += 1
                log.error("cannot remove %s: %s", dst, exc)
            else:
                stats.pruned += 1
                log.debug("pruned %s", dst)

        if rel_root == Path(".") or rel_root in dirs:
            continue
        if rel_root not in state.dirs:
            continue  # a directory that was already there; leave it alone
        if cfg.dry_run:
            log.info("[dry-run] rmdir %s", root_path)
            continue
        try:
            root_path.rmdir()
        except OSError:
            pass  # not empty, or already gone - fine either way
        else:
            log.debug("pruned empty dir %s", root_path)


def clear_tombstones(state, paths):
    """Forget that these paths were deleted, so the next scan links them again.

    Only ever removes entries from the two remembered-deletion sets; it never
    touches the filesystem, so an unrecognised path is simply a no-op.
    """
    if "*" in paths:
        cleared = len(state.deleted) + len(state.deleted_dirs)
        state.deleted, state.deleted_dirs = set(), set()
        return cleared
    cleared = 0
    for path in paths:
        key = path.strip("/")
        prefix = key + "/"
        for bucket in (state.deleted, state.deleted_dirs):
            for entry in [e for e in bucket if e == key or e.startswith(prefix)]:
                bucket.discard(entry)
                cleared += 1
    return cleared


def target_is_empty(cfg):
    """True if TARGET_DIR holds nothing at all (or cannot be read)."""
    try:
        with os.scandir(cfg.target) as entries:
            for _entry in entries:
                return False
    except OSError:
        return True
    return True


def measure_target(cfg):
    """Split what is in TARGET_DIR into space that is shared and space that is not.

    A file with more than one name on disk (or a symlink we made) costs nothing
    beyond the source. Anything else is a real standalone file - typically
    something dropped into the target by hand - and does occupy disk.
    """
    shared_bytes = shared_files = alone_bytes = alone_files = 0
    for root, _dirnames, filenames in os.walk(cfg.target, followlinks=False):
        at_root = Path(root) == cfg.target
        for name in filenames:
            if at_root and (
                name.startswith(STATE_FILENAME)
                or (cfg.quarantine_file and name == cfg.quarantine_file)
            ):
                continue
            path = os.path.join(root, name)
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if os.path.islink(path):
                try:  # a symlink costs nothing; report the data it points at
                    shared_bytes += os.stat(path).st_size
                except OSError:
                    pass
                shared_files += 1
            elif st.st_nlink > 1:
                shared_bytes += st.st_size
                shared_files += 1
            else:
                alone_bytes += st.st_size
                alone_files += 1
    return {
        "shared_bytes": shared_bytes, "shared_files": shared_files,
        "standalone_bytes": alone_bytes, "standalone_files": alone_files,
    }


def run_pass(cfg, state, tracker, status=None):
    stats = Stats()
    started = time.monotonic()
    quarantined = load_quarantine(cfg)
    if status is not None:
        requested = status.take_restores()
        if requested:
            cleared = clear_tombstones(state, requested)
            log.info("force push requested: %d path(s) will be linked again", cleared)

    honor_deletions = cfg.respect_deletions
    if honor_deletions and state.files and target_is_empty(cfg):
        # Everything gone at once is a wiped or unavailable target, not someone
        # deleting files. Re-link instead of writing the whole mirror off.
        log.warning(
            "TARGET_DIR is empty but %d links were recorded - treating this as a "
            "reset and re-linking, rather than honouring it as a deletion",
            len(state.files),
        )
        honor_deletions = False

    inventory = [] if status is not None else None
    files, dirs, inodes, deleted, deleted_dirs = sync_tree(
        cfg, state, tracker, quarantined, stats, honor_deletions, inventory
    )
    tracker.sweep()
    if quarantined:
        remove_quarantined(cfg, state, quarantined, stats)
    if cfg.prune:
        prune_tree(cfg, state, files, dirs, stats)
    if cfg.dry_run:
        state.files, state.dirs = files, dirs
        state.deleted, state.deleted_dirs = deleted, deleted_dirs
    else:
        state.save(files, dirs, tracker.export(), inodes, deleted, deleted_dirs)
    elapsed = time.monotonic() - started
    log.info("scan complete in %.2fs: %s", elapsed, stats)

    if status is not None:
        status.record_scan({
            "finished": time.strftime("%Y-%m-%d %H:%M:%S"),
            "seconds": round(elapsed, 2),
            "linked": stats.linked, "relinked": stats.relinked,
            "unchanged": stats.skipped, "pruned": stats.pruned,
            "quarantined": stats.quarantined, "deferred": stats.deferred,
            "left_deleted": stats.left_deleted, "errors": stats.errors,
        })
        status.set_inventory(inventory, cfg.web_max_files)
        try:
            status.set_usage(measure_target(cfg))
        except OSError as exc:
            log.debug("could not measure target usage: %s", exc)
    return stats


def main():
    env_file = load_config_file()
    cfg = Config()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    warn_duplicate_keys(env_file)
    cfg.validate()

    ensure_target(cfg)

    state_path = Path(cfg.state_file) if cfg.state_file else cfg.target / STATE_FILENAME
    if cfg.state_file and not cfg.dry_run:
        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SystemExit("cannot create STATE_FILE folder %s: %s" % (state_path.parent, exc))
    state = State(state_path)
    state.load()
    tracker = StabilityTracker(cfg.stable_seconds, state.pending)

    log.info(
        "symmetry starting: %s -> %s (mode=%s interval=%ds prune=%s stable=%ds dry_run=%s)",
        cfg.source, cfg.target, cfg.link_mode, cfg.interval, cfg.prune,
        cfg.stable_seconds, cfg.dry_run,
    )

    status = None
    if cfg.web_enabled and not cfg.run_once:
        status = webui.Status(allow_actions=cfg.web_allow_actions)
        status.set_config({
            "SOURCE_DIR": str(cfg.source), "TARGET_DIR": str(cfg.target),
            "SCAN_INTERVAL": "%ds" % cfg.interval, "LINK_MODE": cfg.link_mode,
            "STABLE_SECONDS": "%ds" % cfg.stable_seconds, "PRUNE": str(cfg.prune),
            "INCLUDE_HIDDEN": str(cfg.include_hidden),
            "EXCLUDE_PATTERNS": ", ".join(cfg.exclude),
            "QUARANTINE_FILE": cfg.quarantine_file,
            "RESPECT_DELETIONS": str(cfg.respect_deletions),
            "SOURCE_FILE_LIST": "up to %d shown" % cfg.web_max_files,
            "DRY_RUN": str(cfg.dry_run),
        })
        if webui.start(status, cfg.web_host, cfg.web_port) is None:
            status = None

    stop = {"now": False}

    def handle_signal(signum, _frame):
        stop["now"] = True
        log.info("received signal %s, shutting down", signal.Signals(signum).name)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handle_signal)
        except (ValueError, AttributeError, OSError):
            pass

    while True:
        if status is not None:
            status.set_state(running=True)
        try:
            run_pass(cfg, state, tracker, status)
        except Exception:  # keep the daemon alive across unexpected failures
            log.exception("scan failed")
        finally:
            if status is not None:
                status.set_state(running=False)
        if cfg.run_once or stop["now"]:
            break
        deadline = time.monotonic() + cfg.interval
        while not stop["now"]:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # Still woken in slices so a signal is handled promptly, but a
            # force push from the status page starts the next scan at once.
            if status is not None and status.wake.wait(min(1.0, remaining)):
                status.wake.clear()
                log.info("rescanning now at the request of the status page")
                break
            if status is None:
                time.sleep(min(1.0, remaining))
        if stop["now"]:
            break

    log.info("symmetry stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
