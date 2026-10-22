#!/usr/bin/env python3
"""The builder's CRE automation GitHub App: create it once, or find and check the one they have.

  github_app.py [--alias <amazon-alias>] [--key-dir <dir>] [--wait [<seconds>]]
                [--code <code-or-callback-address>] [--port <n>] [--dry-run]

Every builder has their own App, named `aws-cre-automation-<alias>` (alias: your Amazon alias,
default `whoami`), used by projen's workflows in each of their GitHub repos (dependency upgrades,
self-mutation, auto-merge: gotcha 3). Its private key lives at <key-dir>/<slug>.pem (default
~/.cre, mode 600), never in a repo.

  - The App exists: checks it is yours (owner = your gh login), that the key at <key-dir>/<slug>.pem
    is one of its keys (GitHub accepts a JWT it signed: GET /app), and that it has Contents, Pull
    requests and Workflows write. Prints APP_SLUG, APP_ID, APP_KEY_FILE, APP_INSTALL_URL.
    No key file: exit 10 with how to make one (a browser step on the App's settings page).
  - It doesn't exist: creates it with GitHub's App manifest flow, a browser click by GitHub's design
    (there is no API for it). A small local server (127.0.0.1, --port or a random port) is started
    in the background and this command returns at once: APP_CREATE_URL=<local page> and
    NEEDS=APP_CREATE, exit 13. That page posts the manifest to GitHub; the builder checks the name
    and clicks "Create GitHub App"; GitHub redirects back to the local server with a one-time code,
    which it exchanges for the App's ID and key. Re-run (the same command) to pick up the result;
    --wait N waits for it; without --wait it waits up to 600 s when the page opened in a browser on
    this machine (someone is there to click), and returns at once when it didn't (the URL has to
    reach them first; --wait with no number is 90 s). The manifest: public
    (installable on organizations; nobody can use it without the key), no webhook, repository
    permissions Contents, Pull requests and Workflows (write), nothing else.
    Remote shell (SSH): the browser runs elsewhere, so the callback address can't be reached
    directly. Either forward the port (printed: ssh -L <port>:127.0.0.1:<port> <host>), or after
    clicking Create copy the address the browser fails to open and re-run with --code '<address>'.

Installing the App on a repo is a separate browser step (repo_github.py prepare handles it).
Exit codes: 0 ready | 10 refused (no key, someone else's App, bad key) | 11 gh not logged in |
13 NEEDS=APP_CREATE / APP_PERMISSIONS (a browser step) | 20 failed.
"""
import getpass
import html
import json
import os
import re
import secrets
import socketserver
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import crelib as c  # noqa: E402

NAME = "github-app"


# ---- the manifest flow's local server (run detached: github_app.py _serve ...) -------------------

def exchange(code, key, api):
    """Exchange a manifest code for the App; writes the key (mode 600). Returns {id, slug, owner}."""
    req = urllib.request.Request(f"{api}/app-manifests/{urllib.parse.quote(code)}/conversions", method="POST",
                                 data=b"", headers={"Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            app = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            msg = json.load(e).get("message", "")
        except Exception:
            msg = ""
        raise RuntimeError(f"GitHub answered {e.code} to the code exchange: {msg or e.reason}"
                           + (" (a code is single-use and expires after an hour)" if e.code == 404 else ""))
    except Exception as e:
        raise RuntimeError(f"exchanging the code failed: {e}")
    if not isinstance(app, dict) or not all(k in app for k in ("id", "slug", "pem")):
        raise RuntimeError(f"GitHub's code exchange returned no App ({str(app)[:200]})")
    fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(app["pem"])
    os.chmod(key, 0o600)
    return {"id": app["id"], "slug": app["slug"], "owner": (app.get("owner") or {}).get("login", "")}


def write_state(path, d):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(d, f)
    os.replace(tmp, path)


def serve(slug, login, key, api, web, pending, port, lifetime):
    """The local page that posts the manifest, and the callback. State in <pending>:
    {pid, port, url, state, expires, status: waiting|done|error|expired, id, slug, owner, error}."""
    state = secrets.token_urlsafe(16)
    st = {"pid": os.getpid(), "state": state, "status": "waiting", "expires": int(time.time()) + int(lifetime)}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def reply(self, body):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body.encode())

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            if u.path == "/":
                manifest = {
                    "name": slug,
                    "url": f"{web}/{login}",
                    "redirect_url": f"http://127.0.0.1:{st['port']}/callback",
                    "public": True,
                    "hook_attributes": {"url": f"{web}/{login}", "active": False},
                    "default_permissions": {"contents": "write", "pull_requests": "write", "workflows": "write"},
                    "default_events": [],
                }
                self.reply(f"""<!doctype html><title>Create {html.escape(slug)}</title>
<form id="f" method="post" action="{web}/settings/apps/new?state={state}">
<input type="hidden" name="manifest" value="{html.escape(json.dumps(manifest))}">
<p>Opening GitHub to create the App <b>{html.escape(slug)}</b>...</p><button>Continue</button></form>
<script>document.getElementById('f').submit()</script>""")
            elif u.path == "/callback":
                if q.get("state", [""])[0] != state or "code" not in q:
                    self.reply("<p>Unexpected callback (state mismatch). Close this tab and re-run setup.</p>")
                    return
                try:
                    app = exchange(q["code"][0], key, api)
                except RuntimeError as e:
                    st.update(status="error", error=str(e))
                    self.reply("<p>Creating the App failed; re-run setup to see why.</p>")
                else:
                    st.update(status="done", **app)
                    self.reply(f"<p>Created <b>{html.escape(app['slug'])}</b>. You can close this tab.</p>")
                write_state(pending, st)
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self.send_response(404)
                self.end_headers()

    try:
        srv = socketserver.TCPServer(("127.0.0.1", int(port)), H)
    except OSError as e:
        write_state(pending, {**st, "status": "error", "error": f"can't listen on 127.0.0.1:{port}: {e}"})
        return 1
    with srv:
        st["port"] = srv.server_address[1]
        st["url"] = f"http://127.0.0.1:{st['port']}/"

        def expire():
            st.update(status="expired")
            write_state(pending, st)
            srv.shutdown()

        # Started before serving, so the server always stops (no browser call can block it).
        timer = threading.Timer(int(lifetime), expire)
        timer.daemon = True
        timer.start()
        write_state(pending, st)
        srv.serve_forever()
        timer.cancel()
    return 0


# ---- the step ------------------------------------------------------------------------------------

def parse(step, argv):
    p = c.Parser(step, "github_app.py")
    p.add_argument("--alias", default="")
    p.add_argument("--key-dir", dest="key_dir", default=os.environ.get("CRE_KEY_DIR") or os.path.expanduser("~/.cre"))
    p.add_argument("--wait", nargs="?", type=int, const=90, default=None)
    p.add_argument("--code", default="")
    p.add_argument("--port", type=int, default=0)
    c.common_flags(p)
    a = p.parse_args(argv)
    step.yes, step.dry_run = a.yes, a.dry_run
    if not 0 <= a.port < 65536 or (a.wait is not None and a.wait < 0):
        step.die(c.REFUSED, "--port must be a port number and --wait a number of seconds.")
    a.alias = a.alias or os.environ.get("CRE_ALIAS") or getpass.getuser()
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,14}", a.alias):
        step.die(c.REFUSED, f"Alias '{a.alias}' doesn't look like an Amazon alias (lowercase, up to 15 characters). Pass --alias <your alias>.")
    a.slug = f"aws-cre-automation-{a.alias}"
    a.key = os.path.join(a.key_dir, f"{a.slug}.pem")
    a.pending = os.path.join(a.key_dir, f"{a.slug}.pending.json")
    return a


def lookup(slug):
    """(id, owner) of the App from its public record, or ("", "") when there's no such App. gh prints
    an error body on stdout on a 404, so only a successful call with a numeric id counts."""
    r = c.gh("api", f"apps/{slug}")
    if r.returncode != 0:
        return "", ""
    try:
        d = json.loads(r.stdout)
    except ValueError:
        return "", ""
    app_id = str(d.get("id", ""))
    return (app_id, (d.get("owner") or {}).get("login", "")) if app_id.isdigit() else ("", "")


def read_pending(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def verify(step, a, login, app_id, owner):
    """The App exists: it must be yours, the key must be one of its keys, its permissions enough."""
    web = c.github_web()
    if owner and owner != login:
        step.die(c.REFUSED, f"GitHub App {a.slug} belongs to {owner}, not to you ({login}). Is '{a.alias}' your alias? Pass --alias "
                 f"<your alias>; if it is, the name was taken: ask {owner} to rename or delete it.")
    if not os.path.isfile(a.key) or os.path.getsize(a.key) == 0:
        step.die(c.REFUSED, f"GitHub App {a.slug} (ID {app_id}) exists, but its private key isn't at {a.key}. Make one: "
                 f"{web}/settings/apps/{a.slug} → Private keys → Generate a private key (your browser downloads "
                 f"{a.slug}.<date>.private-key.pem), then:\n"
                 f"   mkdir -p {a.key_dir} && mv \"$(ls -t ~/Downloads/{a.slug}.*.private-key.pem | head -1)\" {a.key} && chmod 600 {a.key}\n"
                 "   and re-run.")
    body = c.App(step, a.slug, app_id, a.key).check_key()
    try:
        os.chmod(a.key, 0o600)
    except OSError:
        pass
    step.ok(f"GitHub App {a.slug} (ID {app_id}) is yours, and the key at {a.key} is one of its keys.")
    missing = c.missing_permissions(body.get("permissions"))
    if missing:
        step.warn(f"{a.slug} lacks write access to: {' '.join(missing)}. projen's workflows need Contents, Pull requests and Workflows: "
                  f"Read and write. Set them at {web}/settings/apps/{a.slug}/permissions → Save changes; each installation then accepts "
                  "them on its page (repo_github.py prepare checks).")
        step.need("APP_PERMISSIONS")
    remove(a.pending)
    step.out("APP_SLUG", a.slug)
    step.out("APP_ID", app_id)
    step.out("APP_KEY_FILE", a.key)
    step.out("APP_INSTALL_URL", f"{web}/apps/{a.slug}/installations/new")


def created(step, a, login, result):
    """A finished manifest flow (the background server, or --code): check the name, then verify."""
    if result.get("slug") != a.slug:
        step.die(c.FAILED, f"GitHub created the App as '{result.get('slug')}', not '{a.slug}' (was the name edited?). Rename it to "
                 f"{a.slug} at {c.github_web()}/settings/apps/{result.get('slug')} → Display name, then re-run.")
    step.ok(f"Created GitHub App {a.slug} (ID {result['id']}); its private key is at {a.key} (readable only by you).")
    verify(step, a, login, str(result["id"]), result.get("owner", ""))


def settle(step, a, login, pending, expired_fails=False):
    """Act on a finished server state; returns True when it ended the step. A page that expired
    before anyone clicked is only an error while waiting for it; a later run starts a new one."""
    status = pending.get("status")
    if status == "done":
        created(step, a, login, pending)
        return True
    if status == "error" or (status == "expired" and expired_fails):
        remove(a.pending)
        why = pending.get("error") or "the local page timed out before GitHub called back"
        step.die(c.FAILED, f"Creating the GitHub App failed: {why}. Re-run to start again; if GitHub said the name is taken, pass --alias.")
    return False


def start_server(step, a, login):
    remove(a.pending)
    lifetime = os.environ.get("CRE_GITHUB_APP_TIMEOUT", "1800")
    # Detached, with no inherited stdout/stderr, so a caller capturing this command's output (an
    # agent's shell tool) isn't held open by it. It stops after the callback or the lifetime.
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "_serve", a.slug, login, a.key, c.github_api(), c.github_web(),
                      a.pending, str(a.port), lifetime],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(50):
        if os.path.exists(a.pending):
            break
        time.sleep(0.1)
    pending = read_pending(a.pending)
    if pending.get("status") != "waiting":
        remove(a.pending)
        step.die(c.FAILED, f"Could not start the local page for the App manifest flow: {pending.get('error') or 'it did not start'}. "
                 "Pass --port <a free port>, or re-run.")
    return pending


def main(step, argv):
    a = parse(step, argv)
    c.require_tools(step, "gh", "openssl", "curl")
    c.require_gh(step)
    r = c.gh("api", "user", "--jq", ".login")
    login = r.stdout.strip()
    if r.returncode != 0 or not login:
        step.die(c.FAILED, "Could not read your GitHub login (gh api user).")

    # A result the background server left from an earlier run.
    if settle(step, a, login, read_pending(a.pending)):
        return
    app_id, owner = lookup(a.slug)
    if app_id:
        verify(step, a, login, app_id, owner)
        return

    # ---- not created yet: the manifest flow ----
    if step.dry_run:
        step.plan(f"(dry run) create GitHub App {a.slug} owned by {login} (a browser step); save its key to {a.key}")
        return
    os.makedirs(a.key_dir, exist_ok=True)
    os.chmod(a.key_dir, 0o700)
    if a.code:
        code = a.code
        if "code=" in code:
            code = urllib.parse.parse_qs(urllib.parse.urlparse(code).query).get("code", [""])[0]
        if not code:
            step.die(c.REFUSED, "--code: no code in that address.")
        try:
            result = exchange(code, a.key, c.github_api())
        except RuntimeError as e:
            step.die(c.FAILED, f"Creating the App from that code failed: {e}")
        remove(a.pending)
        created(step, a, login, result)
        return

    pending = read_pending(a.pending)
    started = False
    if not (pending.get("status") == "waiting" and alive(pending.get("pid"))):
        pending = start_server(step, a, login)
        started = True
    url, port = pending["url"], pending["port"]
    step.out("APP_CREATE_URL", url)
    browser = started and c.open_url(url)
    opened = " (opened in your browser)" if browser else ""
    if browser:
        step.out("APP_CREATE_OPENED", "1")
    minutes = int(os.environ.get("CRE_GITHUB_APP_TIMEOUT", "1800")) // 60
    step.warn(f"Create your GitHub App {a.slug} (owner: {login}): open {url}{opened}, check the name, click 'Create GitHub App'. "
              f"The local page waits up to {minutes} min.")
    if os.environ.get("SSH_CONNECTION"):
        step.info(f"This is an SSH session: that address is on this machine. In a terminal on your laptop: ssh -L {port}:127.0.0.1:{port} "
                  f"<this host>   then open {url} there.")
    step.info(f"If the browser can't open the address GitHub sends it back to (127.0.0.1:{port}/callback?code=...) after you click "
              "Create, copy that address and re-run with --code '<address>'.")
    # Waiting by default only when the page opened in a browser on this machine: someone is there
    # to click. Otherwise the URL has to reach them first, so return it now.
    wait = a.wait if a.wait is not None else (600 if browser else 0)
    if wait > 0:
        step.info(f"Waiting up to {wait}s for you to click Create (the page reports back here)...")
        deadline = time.time() + wait
        while time.time() < deadline:
            if settle(step, a, login, read_pending(a.pending), expired_fails=True):
                return
            time.sleep(1)
    step.info("Re-run this command once you've clicked Create (--wait 90 waits for it).")
    step.need("APP_CREATE")


if __name__ == "__main__":
    if sys.argv[1:2] == ["_serve"] and len(sys.argv) == 10:
        sys.exit(serve(*sys.argv[2:]))
    c.cli(NAME, main)
