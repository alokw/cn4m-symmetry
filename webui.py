"""A small read-only status page for cn4m-symmetry.

Runs on stdlib http.server in a daemon thread beside the scan loop, so it adds
no dependencies and cannot hold up a scan. Everything it shows is a snapshot
the scanner hands it after each pass - the server never touches the filesystem.
"""

import json
import logging
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("symmetry")

UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def human_bytes(size):
    value = float(size or 0)
    for unit in UNITS:
        if value < 1024 or unit == UNITS[-1]:
            return "%.0f %s" % (value, unit) if unit == "B" else "%.2f %s" % (value, unit)
        value /= 1024
    return "%.2f %s" % (value, UNITS[-1])


class Status:
    """Thread-safe snapshot of what the scanner is doing."""

    # Problem states first, so a truncated list never hides the interesting rows.
    ORDER = {"error": 0, "deleted": 1, "deferred": 2, "quarantined": 3,
             "excluded": 4, "symlink": 5, "linked": 6, "relinked": 6, "unchanged": 7}

    def __init__(self, history=60, allow_actions=True):
        self._lock = threading.Lock()
        self._config = {}
        self._usage = {}
        self._scans = deque(maxlen=history)
        self._state = {"running": False, "last_scan": None, "next_scan": None}
        self._files = []
        self._counts = {}
        self._truncated = 0
        self.allow_actions = allow_actions
        self._restores = set()
        self.wake = threading.Event()

    def set_inventory(self, entries, cap):
        """Store the per-file result of a scan, most interesting rows first."""
        entries = entries or []
        counts = {}
        for entry in entries:
            counts[entry["status"]] = counts.get(entry["status"], 0) + 1
        ordered = sorted(entries, key=lambda e: (self.ORDER.get(e["status"], 9), e["path"]))
        with self._lock:
            self._counts = counts
            self._truncated = max(0, len(ordered) - cap) if cap else 0
            self._files = ordered[:cap] if cap else ordered

    def request_restore(self, paths):
        """Queue paths for a force push. Returns how many were accepted."""
        if not self.allow_actions:
            return 0
        wanted = {str(p) for p in paths if str(p).strip()}
        if not wanted:
            return 0
        with self._lock:
            self._restores |= wanted
        self.wake.set()
        return len(wanted)

    def take_restores(self):
        with self._lock:
            pending, self._restores = self._restores, set()
        return pending

    def set_config(self, config):
        with self._lock:
            self._config = dict(config)

    def set_usage(self, usage):
        with self._lock:
            self._usage = dict(usage)

    def set_state(self, **fields):
        with self._lock:
            self._state.update(fields)

    def record_scan(self, entry):
        with self._lock:
            self._scans.appendleft(dict(entry))
            self._state["last_scan"] = entry.get("finished")

    def snapshot(self):
        with self._lock:
            usage = dict(self._usage)
            shared = usage.get("shared_bytes", 0)
            standalone = usage.get("standalone_bytes", 0)
            usage.update(
                total_bytes=shared + standalone,
                shared_human=human_bytes(shared),
                standalone_human=human_bytes(standalone),
                total_human=human_bytes(shared + standalone),
            )
            return {
                "config": dict(self._config),
                "usage": usage,
                "state": dict(self._state),
                "scans": list(self._scans),
                "files": list(self._files),
                "counts": dict(self._counts),
                "truncated": self._truncated,
                "allow_actions": self.allow_actions,
            }


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>cn4m-symmetry</title>
<style>
  :root {
    --bg:#f6f7f9; --card:#fff; --ink:#1c2024; --muted:#6b7280; --line:#e4e7eb;
    --accent:#2f6f4f; --warn:#b45309; --err:#b42318; --shared:#3f8f6b; --alone:#c9a227;
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#14161a; --card:#1b1e23; --ink:#e8eaed; --muted:#9aa1ab; --line:#2b2f36;
            --accent:#5fbf8f; --shared:#4fae83; --alone:#d9b53c; }
  }
  * { box-sizing:border-box }
  body { margin:0; background:var(--bg); color:var(--ink);
         font:14px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif }
  .wrap { max-width:1000px; margin:0 auto; padding:24px 20px 48px }
  header { display:flex; align-items:baseline; gap:12px; flex-wrap:wrap; margin-bottom:20px }
  h1 { font-size:19px; margin:0; font-weight:650; letter-spacing:-.01em }
  .pill { font-size:12px; padding:2px 9px; border-radius:999px; background:var(--accent);
          color:#fff; font-weight:600 }
  .pill.idle { background:var(--muted) }
  .sub { color:var(--muted); font-size:13px }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px;
          padding:16px 18px; margin-bottom:16px }
  .card h2 { font-size:12px; text-transform:uppercase; letter-spacing:.06em;
             color:var(--muted); margin:0 0 12px; font-weight:600 }
  .figures { display:flex; gap:28px; flex-wrap:wrap; margin-bottom:14px }
  .fig .n { font-size:23px; font-weight:650; letter-spacing:-.02em }
  .fig .l { color:var(--muted); font-size:12px; margin-top:1px }
  .fig .n.shared { color:var(--shared) } .fig .n.alone { color:var(--alone) }
  .bar { height:9px; border-radius:5px; overflow:hidden; display:flex; background:var(--line) }
  .bar i { display:block; height:100% }
  .bar .s { background:var(--shared) } .bar .a { background:var(--alone) }
  .legend { display:flex; gap:16px; margin-top:9px; font-size:12px; color:var(--muted);
            flex-wrap:wrap }
  .legend b { font-weight:500; color:var(--ink) }
  .dot { display:inline-block; width:8px; height:8px; border-radius:2px; margin-right:5px }
  .note { margin-top:12px; font-size:12.5px; color:var(--muted); line-height:1.5 }
  table { width:100%; border-collapse:collapse; font-size:13px }
  th { text-align:left; font-weight:600; color:var(--muted); font-size:11.5px;
       text-transform:uppercase; letter-spacing:.04em; padding:0 10px 7px 0 }
  td { padding:5px 10px 5px 0; border-top:1px solid var(--line); vertical-align:top }
  td.num { text-align:right; padding-right:14px; font-variant-numeric:tabular-nums }
  .mono { font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-size:12.5px }
  .k { color:var(--muted); white-space:nowrap; padding-right:18px }
  .zero { color:var(--muted) }
  .hot { color:var(--accent); font-weight:600 }
  .bad { color:var(--err); font-weight:600 }
  .wait { color:var(--warn); font-weight:600 }
  .scroll { overflow-x:auto; max-height:460px; overflow-y:auto }
  .toolbar { display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin-bottom:12px }
  input[type=search] { flex:1 1 220px; min-width:180px; padding:6px 10px; font:inherit;
    font-size:13px; color:var(--ink); background:var(--bg); border:1px solid var(--line);
    border-radius:7px }
  input[type=search]:focus { outline:2px solid var(--accent); outline-offset:-1px }
  .btn { font:inherit; font-size:12px; font-weight:600; padding:5px 11px; cursor:pointer;
    color:#fff; background:var(--accent); border:0; border-radius:7px }
  .btn:hover { filter:brightness(1.08) }
  .btn:disabled { opacity:.55; cursor:default; filter:none }
  .btn.small { padding:3px 9px; font-size:11.5px }
  .chips { display:flex; gap:6px; flex-wrap:wrap }
  .chip { font-size:11.5px; padding:2px 8px; border-radius:999px; border:1px solid var(--line);
    color:var(--muted); background:var(--bg); cursor:pointer; user-select:none }
  .chip.on { background:var(--accent); color:#fff; border-color:transparent }
  .tag { font-size:11px; font-weight:600; padding:2px 7px; border-radius:5px;
    background:var(--line); color:var(--muted); white-space:nowrap }
  .tag.deleted { background:#b4231822; color:var(--err) }
  .tag.deferred { background:#b4530922; color:var(--warn) }
  .tag.quarantined { background:#7c3aed22; color:#7c3aed }
  .tag.linked, .tag.unchanged, .tag.relinked { background:#3f8f6b22; color:var(--shared) }
  .tag.error { background:#b4231822; color:var(--err) }
  td.act { text-align:right; white-space:nowrap }
  th.act { width:1% }
  footer { color:var(--muted); font-size:12px; text-align:center; margin-top:26px }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>cn4m-symmetry</h1>
    <span class="pill" id="pill">starting</span>
    <span class="sub" id="paths"></span>
  </header>

  <div class="card">
    <h2>Disk usage in the target</h2>
    <div class="figures">
      <div class="fig"><div class="n shared" id="shared">-</div>
        <div class="l">shared with source &mdash; costs nothing</div></div>
      <div class="fig"><div class="n alone" id="alone">-</div>
        <div class="l">standalone files &mdash; really on disk</div></div>
      <div class="fig"><div class="n" id="apparent">-</div>
        <div class="l">what Explorer would report</div></div>
    </div>
    <div class="bar"><i class="s" id="barS"></i><i class="a" id="barA"></i></div>
    <div class="legend">
      <span><i class="dot" style="background:var(--shared)"></i>hard-linked
        <b id="nShared">0</b> files</span>
      <span><i class="dot" style="background:var(--alone)"></i>standalone
        <b id="nAlone">0</b> files</span>
    </div>
    <div class="note" id="saved"></div>
  </div>

  <div class="card">
    <h2>Configuration</h2>
    <div class="scroll"><table id="cfg"></table></div>
  </div>

  <div class="card">
    <h2>Source files</h2>
    <div class="toolbar">
      <input id="filter" type="search" placeholder="Filter by path&hellip;" autocomplete="off">
      <span id="chips" class="chips"></span>
      <button id="pushAll" class="btn" hidden>Force push all deleted</button>
    </div>
    <div class="scroll"><table>
      <thead><tr><th>Path</th><th class="num">Size</th><th>State</th>
        <th class="act"></th></tr></thead>
      <tbody id="files"></tbody>
    </table></div>
    <div class="note" id="filesNote"></div>
  </div>

  <div class="card">
    <h2>Recent scans</h2>
    <div class="scroll"><table>
      <thead><tr><th>Finished</th><th class="num">Linked</th><th class="num">Relinked</th>
        <th class="num">Unchanged</th><th class="num">Pruned</th><th class="num">Quarantined</th>
        <th class="num">Deferred</th><th class="num">Kept&nbsp;deleted</th><th class="num">Errors</th><th class="num">Took</th></tr></thead>
      <tbody id="scans"></tbody>
    </table></div>
    <div class="note" id="empty" hidden>No scans recorded yet.</div>
  </div>

  <footer>read-only view &middot; refreshes every 5s</footer>
</div>
<script>
const $ = id => document.getElementById(id);
const cell = (v, cls) => `<td class="num ${v ? (cls || 'hot') : 'zero'}">${v}</td>`;

function render(d) {
  const c = d.config, u = d.usage, s = d.state;
  $('pill').textContent = s.running ? 'scanning' : 'idle';
  $('pill').className = 'pill' + (s.running ? '' : ' idle');
  $('paths').textContent = c.source + '  \\u2192  ' + c.target;

  $('shared').textContent = u.shared_human || '-';
  $('alone').textContent = u.standalone_human || '-';
  $('apparent').textContent = u.total_human || '-';
  $('nShared').textContent = u.shared_files || 0;
  $('nAlone').textContent = u.standalone_files || 0;
  const tot = u.total_bytes || 1;
  $('barS').style.width = (100 * (u.shared_bytes || 0) / tot) + '%';
  $('barA').style.width = (100 * (u.standalone_bytes || 0) / tot) + '%';
  $('saved').textContent = u.shared_bytes
    ? `The mirror holds ${u.shared_human} of hard-linked content that occupies no extra `
      + `space \\u2014 the same bytes as the source, under a second name. Only the `
      + `${u.standalone_human} of standalone files is additional disk usage.`
    : 'Nothing mirrored yet.';

  $('cfg').innerHTML = Object.entries(c).map(([k, v]) =>
    `<tr><td class="k">${k}</td><td class="mono">${v === '' ? '<span class="zero">(none)</span>' : v}</td></tr>`
  ).join('');

  $('scans').innerHTML = d.scans.map(r => `<tr>
    <td class="mono">${r.finished}</td>
    ${cell(r.linked)}${cell(r.relinked)}
    <td class="num zero">${r.unchanged}</td>
    ${cell(r.pruned)}${cell(r.quarantined)}${cell(r.deferred, 'wait')}
    ${cell(r.left_deleted)}${cell(r.errors, 'bad')}
    <td class="num zero">${r.seconds}s</td></tr>`).join('');
  $('empty').hidden = d.scans.length > 0;
}

let LAST = null, FILTER = '', ONLY = '';

const esc = s => String(s).replace(/[&<>"]/g, c =>
  ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));

function size(n) {
  const u = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  let v = n || 0, i = 0;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return (i ? v.toFixed(1) : v) + ' ' + u[i];
}

const LABEL = {
  deleted: 'deleted in target', deferred: 'still arriving', quarantined: 'quarantined',
  excluded: 'excluded', symlink: 'symlink, skipped', error: 'error',
  linked: 'linked', relinked: 'relinked', unchanged: 'mirrored'
};

function renderFiles(d) {
  const rows = d.files.filter(f =>
    (!ONLY || f.status === ONLY) &&
    (!FILTER || f.path.toLowerCase().includes(FILTER)));

  $('chips').innerHTML = Object.entries(d.counts)
    .sort((a, b) => b[1] - a[1])
    .map(([k, n]) => `<span class="chip ${ONLY === k ? 'on' : ''}" data-only="${k}">`
      + `${esc(LABEL[k] || k)} ${n}</span>`).join('');

  const canPush = d.allow_actions;
  $('files').innerHTML = rows.map(f => {
    const st = f.status === 'relinked' || f.status === 'linked' ? 'unchanged' : f.status;
    const btn = (f.status === 'deleted' && canPush)
      ? `<button class="btn small" data-push="${esc(f.path)}">Force push</button>` : '';
    return `<tr><td class="mono">${esc(f.path)}</td><td class="num zero">${size(f.size)}</td>`
      + `<td><span class="tag ${st}">${esc(LABEL[f.status] || f.status)}</span></td>`
      + `<td class="act">${btn}</td></tr>`;
  }).join('');

  const deleted = d.counts.deleted || 0;
  $('pushAll').hidden = !(deleted && canPush);
  $('pushAll').textContent = `Force push all ${deleted} deleted`;
  $('filesNote').textContent = d.files.length
    ? `Showing ${rows.length} of ${d.files.length} files`
      + (d.truncated ? ` (${d.truncated} more not listed)` : '')
      + (canPush ? '' : ' \\u00b7 force push disabled by WEB_ALLOW_ACTIONS')
    : 'No files seen yet.';
}

async function push(body, el) {
  if (el) { el.disabled = true; el.textContent = 'queued\\u2026'; }
  try {
    await fetch('api/restore', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });
    setTimeout(tick, 900);
  } catch (e) { if (el) { el.disabled = false; el.textContent = 'Force push'; } }
}

document.addEventListener('click', e => {
  const chip = e.target.closest('[data-only]');
  if (chip) { ONLY = ONLY === chip.dataset.only ? '' : chip.dataset.only;
              if (LAST) renderFiles(LAST); return; }
  const one = e.target.closest('[data-push]');
  if (one) { push({paths: [one.dataset.push]}, one); return; }
  if (e.target.id === 'pushAll') push({all: true}, e.target);
});

$('filter').addEventListener('input', e => {
  FILTER = e.target.value.trim().toLowerCase();
  if (LAST) renderFiles(LAST);
});

async function tick() {
  try {
    const d = await (await fetch('api/status', {cache: 'no-store'})).json();
    LAST = d; render(d); renderFiles(d);
  }
  catch (e) { $('pill').textContent = 'unreachable'; $('pill').className = 'pill idle'; }
}
tick(); setInterval(tick, 5000);
</script>
</body>
</html>
"""


def _make_handler(status):
    class Handler(BaseHTTPRequestHandler):
        server_version = "cn4m-symmetry"

        def _send(self, code, body, content_type):
            payload = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass  # browser navigated away mid-response

        def do_POST(self):
            if self.path.split("?", 1)[0].rstrip("/") != "/api/restore":
                self._send(404, '{"error":"not found"}', "application/json")
                return
            if not status.allow_actions:
                self._send(
                    403, '{"error":"actions are disabled (WEB_ALLOW_ACTIONS=false)"}',
                    "application/json",
                )
                return
            try:
                length = min(int(self.headers.get("Content-Length") or 0), 1 << 20)
                body = json.loads(self.rfile.read(length) or "{}")
                paths = ["*"] if body.get("all") else list(body.get("paths") or [])
            except (ValueError, TypeError):
                self._send(400, '{"error":"expected JSON"}', "application/json")
                return
            accepted = status.request_restore(paths)
            log.info("status page requested a force push of %d path(s)", accepted)
            self._send(200, json.dumps({"ok": True, "queued": accepted}), "application/json")

        def do_GET(self):
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path == "/":
                self._send(200, PAGE, "text/html; charset=utf-8")
            elif path == "/api/status":
                self._send(
                    200, json.dumps(status.snapshot()), "application/json; charset=utf-8"
                )
            else:
                self._send(404, "not found\n", "text/plain; charset=utf-8")

        def log_message(self, fmt, *args):
            log.debug("web: " + fmt, *args)

    return Handler


def start(status, host, port):
    """Start the status server in a daemon thread. Returns it, or None."""
    try:
        server = ThreadingHTTPServer((host, port), _make_handler(status))
    except OSError as exc:
        log.error("web interface disabled, cannot bind %s:%s: %s", host, port, exc)
        return None
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="webui", daemon=True)
    thread.start()
    log.info("web interface on http://%s:%d/", host, port)
    return server
