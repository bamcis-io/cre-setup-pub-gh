"""Shared helpers for the CRE setup steps (aws_account, dns, repo_gitlab, repo_github, github_app)
and the end-to-end orchestrator (new_project). Python 3.9+, standard library only.

Contract every setup step follows, so an agent or a person can drive it deterministically:
  - It prints what it is about to change, then changes it. `--dry-run` prints only.
  - It is idempotent: anything already in the desired state is reported and skipped.
  - A change to something possibly shared (an existing IAM role's trust) shows a before/after diff
    and needs confirmation: interactive y/N, or `--yes`. Without a terminal and without `--yes`
    it stops with exit 12 instead of guessing.
  - Values the next step needs are printed on stdout as `KEY=value` lines; everything else goes to
    stderr. The last stdout line is `RESULT code=<n> step=<name>`.
  - Exit codes:
       0  done (including "nothing to change")
      10  refused: bad or missing input, or a conflict that needs a human decision; nothing changed
      11  credentials missing or expired; the message names which and how to refresh them
      12  confirmation required (re-run interactively, or with --yes after reviewing the diff)
      13  done as far as a script can go: a person must act on each `NEEDS=<what>` line (a browser
          step, an org owner's approval). The same command, re-run afterwards, carries on.
      20  an operation failed part way; the message says what was done and what wasn't

AWS, GitHub and GitLab are reached through their CLIs (`aws`, `gh`, `git`, `ssh`) and `curl`:
curl carries the Midway cookie jar through gitlab.aws.dev's sign-in redirects exactly as verified
live, and the offline harness (test/setup/run.sh) replaces each of these tools with a fake.
"""
import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

OK, REFUSED, CREDENTIALS, CONFIRM, NEEDS, FAILED = 0, 10, 11, 12, 13, 20

# Names the deploy sets itself (stack-jobs.ts RESERVED_VARIABLES; a jest test keeps them equal).
# A project CI/CD variable outranks a GitLab job's own variables, so one of these would silently
# replace the generated value. STACK_* names carry earlier stacks' outputs and are reserved too.
RESERVED_VARS = ("STACK_NAME TEMPLATE_NAME AWS_REGION AWS_DEFAULT_REGION ASSETS_BUCKET OUTPUTS_PREFIX "
                 "OUTPUTS ROLE_ARN ASSUME_ROLE STACK_PARAMETERS REQUIRED_VARS REQUIRED_OUTPUTS "
                 "ASSETS_PREFIX PROJECT_NAME CRE_ROLE_ASSUMED").split()
RESERVED_PREFIX = "STACK_"

SETUP_DIR = os.path.dirname(os.path.abspath(__file__))


# ---- the step: messages, outputs, exit ---------------------------------------------------------

class Exit(Exception):
    """Ends a step with an exit code (the message is already printed)."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class Step:
    """One setup step's run: its flags (yes / dry_run), the KEY=value outputs and the NEEDS.

    echo=True (a script run from the command line) prints each output on stdout as it is made;
    the orchestrator runs steps with echo=False and reads `outputs`.
    """

    def __init__(self, name, echo=True):
        self.name = name
        self.echo = echo
        self.yes = False
        self.dry_run = False
        self.outputs = {}
        self.needs = []
        self._cleanups = []

    # messages (stderr)
    def info(self, msg):
        print(f"ℹ️  {msg}", file=sys.stderr)

    def ok(self, msg):
        print(f"✅ {msg}", file=sys.stderr)

    def warn(self, msg):
        print(f"⚠️  {msg}", file=sys.stderr)

    def plan(self, msg):
        print(f"→  {msg}", file=sys.stderr)

    def out(self, key, value):
        value = "" if value is None else str(value)
        # The first value of a key is the one callers read; a step never sets a key twice on purpose.
        self.outputs.setdefault(key, value)
        if self.echo:
            print(f"{key}={value}", flush=True)

    def need(self, what):
        """A person must act (exit 13): printed as NEEDS=<what>."""
        if what not in self.needs:
            self.needs.append(what)
        if self.echo:
            print(f"NEEDS={what}", flush=True)

    def die(self, code, msg):
        print(f"❌ {msg}", file=sys.stderr)
        raise Exit(code)

    def confirm(self, question):
        """True when confirmed. Dies 12 when it can't ask and --yes wasn't given."""
        if self.yes:
            return True
        if self.dry_run:
            self.info(f"(dry run) would ask: {question}")
            return False
        if not sys.stdin.isatty():
            self.die(CONFIRM, f"Confirmation needed: {question} Review the change above and re-run with --yes to apply it.")
        sys.stderr.write(f"{question} [y/N] ")
        sys.stderr.flush()
        return sys.stdin.readline().strip() in ("y", "Y", "yes", "YES")

    def apply(self, what, fn, *args, **kwargs):
        """Run fn unless --dry-run (then print the plan and return None)."""
        if self.dry_run:
            self.plan(f"(dry run) {what}")
            return None
        self.plan(what)
        return fn(*args, **kwargs)

    def on_exit(self, fn):
        self._cleanups.append(fn)

    def close(self):
        while self._cleanups:
            try:
                self._cleanups.pop()()
            except Exception:  # cleanup must never mask the step's own result
                pass

    def final_code(self, code):
        return NEEDS if code == OK and self.needs else code


def invoke(name, fn, argv, echo=True):
    """Run a step function fn(step, argv) and return (code, step)."""
    step = Step(name, echo=echo)
    try:
        fn(step, argv)
        code = OK
    except Exit as e:
        code = e.code
    except KeyboardInterrupt:
        print("❌ Interrupted.", file=sys.stderr)
        code = FAILED
    except Exception:  # a bug or an unexpected environment: still a setup exit code and a RESULT line
        import traceback
        traceback.print_exc()
        print(f"❌ {name} failed unexpectedly (the traceback above). Re-run; if it repeats, report it.", file=sys.stderr)
        code = FAILED
    finally:
        step.close()
    return step.final_code(code), step


def cli(name, fn):
    """The command-line entry point of a step: prints the RESULT line and exits with its code."""
    code, _ = invoke(name, fn, sys.argv[1:], echo=True)
    print(f"RESULT code={code} step={name}", flush=True)
    sys.exit(code)


class Parser(argparse.ArgumentParser):
    """argparse that refuses bad input with exit 10 (the setup contract), not argparse's 2."""

    def __init__(self, step, prog, **kw):
        super().__init__(prog=prog, allow_abbrev=False, **kw)
        self._step = step

    def error(self, message):
        self._step.die(REFUSED, f"{self.prog}: {message}")


def common_flags(p):
    p.add_argument("--yes", action="store_true", help="apply changes that need confirmation")
    p.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")


# ---- processes -----------------------------------------------------------------------------------

def run(cmd, *, input=None, cwd=None, env=None):
    """Run a command; returns CompletedProcess with text stdout/stderr. Never raises on exit code.

    stdin is closed unless `input` is given, so a child can never wait on a prompt nobody sees
    (its output is captured). Undecodable bytes are replaced. A missing executable is returncode
    127, like a shell."""
    full_env = None
    if env:
        full_env = {**os.environ, **env}
    try:
        return subprocess.run(cmd, input=input, stdin=None if input is not None else subprocess.DEVNULL, cwd=cwd,
                              env=full_env, capture_output=True, text=True, errors="replace")
    except FileNotFoundError as e:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: command not found ({e})")


def last_line(text):
    lines = [x for x in (text or "").strip().splitlines() if x.strip()]
    return lines[-1] if lines else ""


def require_tools(step, *names):
    """Exit 10 naming the missing tools (not 11: there is nothing to log in to)."""
    missing = [n for n in names if shutil.which(n) is None]
    if missing:
        step.die(REFUSED, f"Missing tools: {' '.join(missing)}. Install them (macOS: brew install {' '.join(missing)}), then re-run.")


def open_url(url):
    """Open a URL in a GUI browser when there is one (macOS, or a Linux desktop); never a console
    browser, which would block. Returns False when nothing was opened."""
    if os.environ.get("CRE_NO_BROWSER"):
        return False
    if sys.platform == "darwin" and shutil.which("open"):
        # macOS `open` hands the URL to the browser and exits at once: its exit code says whether
        # it worked (it fails outside a logged-in desktop session, e.g. over SSH).
        return run(["open", url]).returncode == 0
    if (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")) and shutil.which("xdg-open"):
        cmd = ["xdg-open", url]
    else:
        return False
    # Detached, with no pipes: a browser xdg-open starts would otherwise hold them until it exits.
    try:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return True
    except OSError:
        return False


def write_json(path, doc, sort_keys=True):
    with open(path, "w") as f:
        json.dump(doc, f, indent=2, sort_keys=sort_keys)
        f.write("\n")


def tmpdir(step):
    d = tempfile.mkdtemp(prefix="cre-setup-")
    step.on_exit(lambda: shutil.rmtree(d, ignore_errors=True))
    return d


# ---- cre-setup.json ------------------------------------------------------------------------------

class SetupFile:
    """The project's recorded setup values (cre-setup.json at the project root, written by the
    scaffold), so `npx projen setup:*` in a project needs no flags it already knows. Flags always
    win. $CRE_SETUP_FILE overrides the location."""

    def __init__(self, step):
        self.step = step
        self.path = os.environ.get("CRE_SETUP_FILE") or ""
        if not self.path:
            top = run(["git", "rev-parse", "--show-toplevel"])
            cand = os.path.join(top.stdout.strip(), "cre-setup.json") if top.returncode == 0 else ""
            if cand and os.path.isfile(cand):
                self.path = cand
        self.data = {}
        if self.path and os.path.isfile(self.path):
            try:
                with open(self.path) as f:
                    self.data = json.load(f)
            except (OSError, ValueError) as e:
                step.die(REFUSED, f"{self.path} isn't valid JSON ({e}). Fix it, or set CRE_SETUP_FILE to another file.")
        self.used = []

    @property
    def dir(self):
        """The project directory, "" when there is no real cre-setup.json (CRE_SETUP_FILE=/dev/null)."""
        return os.path.dirname(self.path) if self.path and os.path.isfile(self.path) else ""

    def default(self, args, attr, key):
        """Set args.<attr> from the recorded value when the flag wasn't given."""
        if getattr(args, attr) in (None, "", []):
            v = self.data.get(key)
            if v not in (None, ""):
                setattr(args, attr, v)
                self.used.append(key)

    def report(self):
        if self.used:
            self.step.info(f"From {self.path}: {' '.join(self.used)}")

    def set(self, key, value):
        """Record a value (only in a project that has a cre-setup.json)."""
        if not self.path or not os.path.isfile(self.path) or value in (None, "") or self.step.dry_run:
            return
        if self.data.get(key) != value:
            self.data[key] = value
            write_json(self.path, self.data, sort_keys=False)  # a committed file: keep its order


# ---- AWS -----------------------------------------------------------------------------------------

class Aws:
    """The aws CLI with one explicit profile (AWS_PROFILE semantics, but explicit so a stray env
    var can't silently point setup at a different account)."""

    def __init__(self, step, profile):
        self.step = step
        self.profile = profile or ""
        self.account = self.caller = ""
        self.partition = "aws"

    def call(self, *args):
        cmd = ["aws"] + (["--profile", self.profile] if self.profile else []) + list(args)
        return run(cmd)

    def text(self, *args):
        """stdout of a successful call (stripped), else None."""
        r = self.call(*args)
        return r.stdout.strip() if r.returncode == 0 else None

    def json(self, *args):
        r = self.call(*args)
        if r.returncode != 0:
            return None
        try:
            return json.loads(r.stdout or "null")
        except ValueError:
            return None

    def require(self):
        """Check the credentials work; sets account, caller, partition. Dies 11 with how to refresh."""
        r = self.call("sts", "get-caller-identity", "--query", "[Account,Arn]", "--output", "text")
        parts = r.stdout.split() if r.returncode == 0 else []
        if len(parts) != 2:
            self.step.die(CREDENTIALS, f"AWS credentials for profile '{self.profile or '<default>'}' are missing or expired:\n"
                          f"   {(r.stderr or r.stdout).strip()}\n"
                          "   Refresh them (for example: isengard / ada credentials update for that profile), then re-run.")
        self.account, self.caller = parts
        # The partition (aws, aws-us-gov, aws-eusc, ...) from the caller's own ARN: every ARN setup
        # builds uses it, including policy.py's, so nothing assumes commercial AWS.
        self.partition = self.caller.split(":")[1] if self.caller.count(":") >= 2 else "aws"
        os.environ["CRE_PARTITION"] = self.partition
        self.step.info(f"AWS account {self.account} as {self.caller} (profile {self.profile or '<default>'})")
        # Production is assumed when the identity says prod.
        alias = self.text("iam", "list-account-aliases", "--query", "AccountAliases[0]", "--output", "text") or ""
        if "prod" in f"{self.caller} {alias} {self.profile}".lower():
            self.step.warn(f"This account looks like PRODUCTION ({alias}). Setup creates S3 buckets and IAM roles.")
            if not self.step.confirm(f"Continue in account {self.account} ({alias})?"):
                self.step.die(REFUSED, "Stopped: not continuing in a production-looking account.")
        return self

    def role_arn(self, name):
        """The role's ARN as IAM returns it ("" if the role doesn't exist). ARNs are never assembled
        from a name: IAM finds a role case-insensitively (GitlabRunner finds GitLabRunner) but STS,
        trust-policy ARN conditions and the GitLab credential vendor compare the ARN exactly, and a
        role may carry a path."""
        v = self.text("iam", "get-role", "--role-name", name, "--query", "Role.Arn", "--output", "text")
        return "" if v in (None, "", "None") else v

    def can(self, principal, action, resource, passed_to=None):
        """Read-only check that a role's own policies allow an action. True when allowed, or when the
        simulation can't run (no permission to simulate): advice only, so unknown isn't a gap."""
        args = ["iam", "simulate-principal-policy", "--policy-source-arn", principal, "--action-names", action,
                "--resource-arns", resource]
        if passed_to:
            args += ["--context-entries",
                     f"ContextKeyName=iam:PassedToService,ContextKeyValues={passed_to},ContextKeyType=string"]
        v = self.text(*args, "--query", "EvaluationResults[0].EvalDecision", "--output", "text")
        return v is None or v == "allowed"


# ---- GitLab (Midway only) ------------------------------------------------------------------------

GITLAB_HOST = os.environ.get("CRE_GITLAB_HOST", "gitlab.aws.dev")
GITLAB_SSH = os.environ.get("CRE_GITLAB_SSH", "git@ssh.gitlab.aws.dev")
GITLAB_API = os.environ.get("CRE_GITLAB_API", f"https://{GITLAB_HOST}/api/v4")
MANAGE_API = os.environ.get("CRE_GITLAB_MANAGE_API", "https://manage.gitlab.aws.dev")


def midway_cookie():
    return os.environ.get("MIDWAY_COOKIE") or os.path.expanduser("~/.midway/cookie")


def midway_expiry(path):
    """The latest expiry (epoch seconds) of the Midway session cookie, 0 when there is none."""
    best = 0
    try:
        with open(path) as f:
            for line in f:
                cols = line.rstrip("\n").split("\t")
                if len(cols) >= 7 and "midway-auth.amazon.com" in cols[0] and cols[5] == "session":
                    try:
                        best = max(best, int(cols[4]))
                    except ValueError:
                        pass
    except OSError:
        return 0
    return best


# ---- where the GitLab API token file is ---------------------------------------------------------
# The token itself is never copied anywhere. Its path is recorded once per machine, as a link in
# the template clone (.agents/gitlab-token, gitignored; made by new_project.py from /cre-demo-init's
# answer), and every GitLab project gets the same link at .cre/gitlab-token (gitignored; made by
# attach-template.sh at scaffold and on every update-template). No default path is assumed: where a
# builder keeps the file, and what it's called, is theirs.

TEMPLATE_TOKEN_LINK = os.path.join(".agents", "gitlab-token")
PROJECT_TOKEN_LINK = os.path.join(".cre", "gitlab-token")


def template_dir():
    """The cre-demo-template clone: this file's own (run from the template), or the one a project
    records in projenrc/cre/SOURCE.json (run from a project's vendored copy). "" when unknown."""
    # setup → scripts → cre-projen | cre → packages | projenrc → the template | the project
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(SETUP_DIR))))
    if os.path.isfile(os.path.join(root, "recipe", "recipe.md")) and os.path.isdir(os.path.join(root, "skeleton")):
        return root
    tp = ""
    # .cre/template.json (per machine) since #35; SOURCE.json in older projects.
    for rec in (os.path.join(root, ".cre", "template.json"), os.path.join(root, "projenrc", "cre", "SOURCE.json")):
        try:
            with open(rec) as f:
                tp = json.load(f).get("templatePath", "")
        except (OSError, ValueError, AttributeError):
            tp = ""
        if tp:
            break
    return tp if tp and os.path.isdir(tp) else ""


def template_token_record():
    """The template's record of the token file's path (a link), "" when the template is unknown.
    $CRE_TOKEN_RECORD replaces it (the offline harnesses: never the real clone's record)."""
    if os.environ.get("CRE_TOKEN_RECORD"):
        return os.environ["CRE_TOKEN_RECORD"]
    tpl = template_dir()
    return os.path.join(tpl, TEMPLATE_TOKEN_LINK) if tpl else ""


def gitlab_token_file(project_dir=None):
    """(path, where) of the GitLab token file, ("", "") when none is known. In order:
    $CRE_GITLAB_TOKEN_FILE; the project's .cre/gitlab-token link (project_dir, else the current
    directory); the template's .agents/gitlab-token record. A link is returned even when it dangles,
    so the message names what it points at."""
    if os.environ.get("CRE_GITLAB_TOKEN_FILE"):
        return expand_path(os.environ["CRE_GITLAB_TOKEN_FILE"]), "$CRE_GITLAB_TOKEN_FILE"
    link = os.path.join(project_dir or os.getcwd(), PROJECT_TOKEN_LINK)
    if os.path.lexists(link):
        return link, PROJECT_TOKEN_LINK
    rec = template_token_record()
    if rec and os.path.lexists(rec):
        return rec, f"the template's {TEMPLATE_TOKEN_LINK}"
    return "", ""


def recorded_token_target():
    """The token file the template records (what its link points at), "" when none."""
    rec = template_token_record()
    return os.readlink(rec) if rec and os.path.islink(rec) else ""


def read_token_file(path):
    """(token, "") or ("", what is wrong with the file)."""
    if os.path.islink(path) and not os.path.exists(path):
        return "", f"points at {os.readlink(path)}, which doesn't exist"
    try:
        with open(path) as f:
            text = f.read()
    except (OSError, UnicodeDecodeError) as e:
        return "", f"can't be read ({e})"
    parts = text.split()
    if not parts:
        return "", "is empty"
    if len(parts) > 1:
        return "", "holds more than one word; it must hold only the token"
    return parts[0], ""


def expand_path(p):
    """A path as the builder typed it (~, ~user, $HOME, relative) → absolute."""
    return os.path.abspath(os.path.expanduser(os.path.expandvars(p.strip())))


def check_gitlab_token(step, token, path):
    """GitLab accepts the token: active, not expired, `api` scope. Warns when it expires within 30
    days. Needs the Midway cookie (the front door). Dies 11 naming the file otherwise."""
    with GitLab(step) as gl:
        gl.token = token
        status, d = gl.call("GET", "/personal_access_tokens/self")
    if status in ("401", "403", "404") or not isinstance(d, dict) or status != "200":
        step.die(CREDENTIALS, f"GitLab refused the token in {path} ({status}): it is expired, revoked or mistyped "
                 "(or the Midway session lapsed: mwinit -f -k ~/.ssh/id_ecdsa.pub).\n" + token_help())
    if d.get("active") is False or d.get("revoked"):
        step.die(CREDENTIALS, f"The token in {path} is no longer active.\n" + token_help())
    if "api" not in (d.get("scopes") or []):
        step.die(CREDENTIALS, f"The token in {path} lacks the 'api' scope (it has: {', '.join(d.get('scopes') or []) or 'none'}).\n"
                 + token_help())
    exp = d.get("expires_at")
    if exp:
        import datetime
        left = (datetime.date.fromisoformat(exp) - datetime.date.today()).days
        if left < 30:
            step.warn(f"The GitLab token in {path} expires in {left} day(s) ({exp}). Create a new one and replace the file's contents.")
    step.ok(f"GitLab token in {path} is valid ('{d.get('name', '?')}', api scope{', expires ' + exp if exp else ''}).")


def token_help():
    return ("   Create a personal access token with the 'api' scope at\n"
            f"   https://{GITLAB_HOST}/-/user_settings/personal_access_tokens and save it in a file only you can read,\n"
            "   e.g.  (umask 077; mkdir -p <dir>; cat > <dir>/<file>)  then paste the token, Enter, Ctrl-D.\n"
            "   /cre-demo-init records where it is (gitlab.tokenFile in the answers) for every later project. For a\n"
            f"   project made before that: ln -s <that file> {PROJECT_TOKEN_LINK}   (in the project; .cre/ is gitignored).\n"
            "   For one run only: $GITLAB_TOKEN or CRE_GITLAB_TOKEN_FILE=<that file>.")


def place_link(path, target):
    """Make <path> a symlink to <target>, replacing an older link (never a real file). Returns
    "created", "updated", "unchanged" or "blocked" (a real file or directory is there)."""
    if os.path.islink(path):
        if os.readlink(path) == target:
            return "unchanged"
        state = "updated"
    elif os.path.lexists(path):
        return "blocked"
    else:
        state = "created"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp{os.getpid()}"
    if os.path.lexists(tmp):
        os.remove(tmp)
    os.symlink(target, tmp)
    os.replace(tmp, path)
    return state


class GitLab:
    """GitLab REST calls with the Midway cookie plus an API token (gitlab.aws.dev requires both).

    Use as a context manager: a token minted over SSH is revoked and the cookie jar deleted on exit."""

    def __init__(self, step):
        self.step = step
        self.token = ""
        self.minted = False
        self.jar = ""

    def require_midway(self):
        cookie = midway_cookie()
        if midway_expiry(cookie) <= time.time():
            self.step.die(CREDENTIALS, "Midway cookie missing or expired. Run: mwinit -f -k ~/.ssh/id_ecdsa.pub   then re-run.")
        r = run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-T", GITLAB_SSH], input="")
        if "Welcome to GitLab" not in (r.stdout + r.stderr):
            self.step.die(CREDENTIALS, f"Cannot authenticate to GitLab over SSH ({GITLAB_SSH}); the Midway SSH certificate is missing or "
                          "expired. Run: mwinit -f -k ~/.ssh/id_ecdsa.pub   then re-run.")

    def __enter__(self):
        # A per-run cookie jar seeded from the Midway cookie and primed with a GET. GETs survive the
        # Midway front door's sign-in redirect (-L), but a write that arrives without the front
        # door's own session cookie is redirected too and answered 405. Every call reads and
        # updates this jar, so writes carry the session the priming GET obtained.
        fd, self.jar = tempfile.mkstemp(prefix="cre-gl-jar-")
        os.close(fd)
        shutil.copyfile(midway_cookie(), self.jar)
        run(["curl", "-sS", "-L", "-o", os.devnull, "-b", self.jar, "-c", self.jar, f"{GITLAB_API}/version"])
        return self

    def __exit__(self, *exc):
        self.revoke()
        if self.jar:
            try:
                os.remove(self.jar)
            except OSError:
                pass
            self.jar = ""
        return False

    def get_token(self, mint=True):
        """An api token. Tried in order:
          1. (mint=True) Minted over the Midway-signed SSH session (gitlab-shell
             `personal_access_token`), held only in memory and revoked on exit. GitLab issues these
             only to an SSH *key* it has on record; a Midway certificate authenticates by username,
             so on gitlab.aws.dev today this is refused. Kept first so it starts working without a
             change if GitLab allows it.
          2. $GITLAB_TOKEN.
          3. The token file gitlab_token_file() finds (never an assumed default path).
        A stored token (2, 3) is never revoked."""
        why = "not tried"
        if mint:
            name = "cre-setup-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            r = run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", GITLAB_SSH,
                     "personal_access_token", name, "api", "1"], input="")
            text = r.stdout + "\n" + r.stderr
            m = re.search(r"glpat-[A-Za-z0-9_-]+", text) or re.search(r"^\s*Token:\s*(\S+)", text, re.M)
            if m and r.returncode == 0:
                self.token = m.group(1) if m.lastindex else m.group(0)
                self.minted = True
                self.step.info("Using a one-day GitLab token minted over SSH (revoked on exit).")
                return
            why = next((ln[len("remote:"):].strip() for ln in text.splitlines()
                        if ln.startswith("remote:") and ln[len("remote:"):].strip().strip("=")), "") or "no output"
            why = f"minting one over SSH was refused ({why})"
        if os.environ.get("GITLAB_TOKEN"):
            self.token = os.environ["GITLAB_TOKEN"].strip()
            self.step.info(f"{why[0].upper()}{why[1:]}; using $GITLAB_TOKEN." if mint else "Using $GITLAB_TOKEN.")
            return
        path, where = gitlab_token_file()
        if not path:
            self.step.die(CREDENTIALS, "No GitLab API token" + (f": {why}, and none is recorded." if mint else " is recorded.")
                          + "\n" + token_help())
        token, err = read_token_file(path)
        if err:
            self.step.die(CREDENTIALS, f"The GitLab token file ({where}: {path}) {err}.\n" + token_help())
        self.token = token
        if mint:
            self.step.info(f"{why[0].upper()}{why[1:]}; using the token file ({where}).")

    def revoke(self):
        if self.token and self.minted:
            run(["curl", "-sS", "-L", "-o", os.devnull, "-b", self.jar or midway_cookie(), "-H", "@-",
                 "-X", "DELETE", f"{GITLAB_API}/personal_access_tokens/self"],
                input=f"PRIVATE-TOKEN: {self.token}\n")
            self.token = ""
            self.minted = False

    def call(self, method, path, body=None, raw=False):
        """(status, parsed body) of a REST call (raw=True: the body as text, e.g. a job log). Status "000" when curl itself failed. Never dies:
        the Midway cookie gets the request past the SSO front door, the token identifies you (without
        it GitLab answers 404 for anything private)."""
        fd, out = tempfile.mkstemp(prefix="cre-gl-")
        os.close(fd)
        data = ""
        try:
            cmd = ["curl", "-sS", "-L", "-o", out, "-w", "%{http_code}", "-b", self.jar or midway_cookie(),
                   "-c", self.jar or os.devnull, "-H", "@-", "-X", method]
            if body is not None:
                # The body (variable values) in a 0600 file, never on curl's command line.
                fd, data = tempfile.mkstemp(prefix="cre-gl-body-")
                with os.fdopen(fd, "w") as f:
                    json.dump(body, f)
                cmd += ["-H", "Content-Type: application/json", "--data-binary", f"@{data}"]
            cmd.append(f"{GITLAB_API}{path}")
            r = run(cmd, input=f"PRIVATE-TOKEN: {self.token}\n")
            status = r.stdout.strip()[-3:] if r.returncode == 0 and r.stdout.strip() else "000"
            with open(out, errors="replace") as f:
                text = f.read()
        finally:
            os.remove(out)
            if data:
                os.remove(data)
        if raw:
            return status, text
        try:
            parsed = json.loads(text) if text.strip() else {}
        except ValueError:
            parsed = {"raw": text[:500]}
        return status, parsed

    def manage(self, method, path, body=None):
        """The GitLab Management Portal API (manage.gitlab.aws.dev), Amazon's front for actions
        GitLab's own API refuses here (create a project with a Bindle). Behind Midway via ALB OIDC:
        the cookie authenticates it. A GET first, through the Midway redirect, gets the portal's ALB
        session cookie: a POST without one is redirected to sign-in and answered 405."""
        d = tempfile.mkdtemp(prefix="cre-manage-")
        try:
            jar, out = os.path.join(d, "jar"), os.path.join(d, "out")
            run(["curl", "-sS", "-L", "-o", os.devnull, "-b", midway_cookie(), "-c", jar,
                 f"{MANAGE_API}/api/public/gitlab/groups"])
            cmd = ["curl", "-sS", "-o", out, "-w", "%{http_code}", "-b", jar, "-X", method]
            if body is not None:
                with open(os.path.join(d, "body"), "w") as f:
                    json.dump(body, f)
                cmd += ["-H", "Content-Type: application/json", "--data-binary", f"@{os.path.join(d, 'body')}"]
            cmd.append(f"{MANAGE_API}{path}")
            r = run(cmd)
            status = r.stdout.strip()[-3:] if r.returncode == 0 and r.stdout.strip() else "000"
            raw = open(out, errors="replace").read() if os.path.exists(out) else ""
        finally:
            shutil.rmtree(d, ignore_errors=True)
        return status, raw


def urlenc(s):
    from urllib.parse import quote
    return quote(s, safe="")


# ---- GitHub --------------------------------------------------------------------------------------

def gh(*args, input=None):
    return run(["gh"] + list(args), input=input)


def require_gh(step):
    if gh("auth", "status", "-h", "github.com").returncode != 0:
        step.die(CREDENTIALS, "The GitHub CLI isn't logged in. Run: gh auth login   then re-run.")


def gh_fail(step, code, what, output, owner=""):
    """Dies, with exit 11 when the problem is the login, not the request."""
    org = owner or "the organization"
    if "SAML" in output:
        step.die(CREDENTIALS, f"GitHub refused {what}: {org} uses SAML single sign-on and your gh login isn't authorized for it. "
                 f"Run: gh auth refresh -h github.com   and authorize {org} when GitHub asks (or "
                 f"https://github.com/settings/applications → GitHub CLI → Grant for {org}), then re-run. ({last_line(output)})")
    if "HTTP 401" in output or "Bad credentials" in output:
        step.die(CREDENTIALS, f"GitHub rejected your gh login ({what}: {last_line(output)}). Run: gh auth login   then re-run.")
    step.die(code, f"{what} failed: {last_line(output)}")


def github_api():
    return os.environ.get("CRE_GITHUB_API", "https://api.github.com")


def github_web():
    return os.environ.get("CRE_GITHUB_WEB", "https://github.com")


PROJEN_PERMISSIONS = ("contents", "pull_requests", "workflows")


def missing_permissions(perms):
    """The repository permissions projen's workflows need (write) missing from a permissions map."""
    perms = perms or {}
    return [k for k in PROJEN_PERMISSIONS if perms.get(k) != "write"]


class App:
    """Calls to GitHub as the App itself: a JWT signed with its private key (openssl, RS256)."""

    def __init__(self, step, slug, app_id, key_file):
        self.step, self.slug, self.id, self.key_file = step, slug, str(app_id), key_file

    def jwt(self):
        """GitHub accepts at most 10 minutes; issued 60 s in the past and valid 5 minutes, so a clock
        up to a minute off either way works."""
        def b64(b):
            return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
        now = int(time.time())
        head = b64(b'{"alg":"RS256","typ":"JWT"}')
        body = b64(json.dumps({"iat": now - 60, "exp": now + 300, "iss": self.id}, separators=(",", ":")).encode())
        r = subprocess.run(["openssl", "dgst", "-sha256", "-sign", self.key_file], input=f"{head}.{body}".encode(),
                           capture_output=True)
        if r.returncode != 0 or not r.stdout:
            self.step.die(REFUSED, f"Can't sign with {self.key_file}: it isn't an RSA private key. Use the .pem GitHub generated for {self.slug}.")
        return f"{head}.{body}.{b64(r.stdout)}"

    def get(self, path):
        """(status, body dict). Status "000" when GitHub couldn't be reached."""
        fd, out = tempfile.mkstemp(prefix="cre-app-")
        os.close(fd)
        try:
            r = run(["curl", "-sS", "-o", out, "-w", "%{http_code}", "-H", "@-",
                     "-H", "Accept: application/vnd.github+json", f"{github_api()}{path}"],
                    input=f"Authorization: Bearer {self.jwt()}\n")
            status = r.stdout.strip()[-3:] if r.returncode == 0 and r.stdout.strip() else "000"
            raw = open(out).read()
        finally:
            os.remove(out)
        try:
            body = json.loads(raw) if raw.strip() else {}
        except ValueError:
            body = {"message": raw[:200]}
        return status, body if isinstance(body, dict) else {}

    def fail(self, status, path, body):
        msg = body.get("message", "")
        if status == "401":
            self.step.die(REFUSED, f"GitHub rejected a token signed with {self.key_file} for App {self.slug} (ID {self.id}): {msg}. "
                          "The key isn't one of this App's (rotated, or another App's), or this machine's clock is off. "
                          f"Make a new key: {github_web()}/settings/apps/{self.slug} → Private keys → Generate a private key, "
                          f"and save it as {self.key_file} (github_app.py says how).")
        if status == "000":
            self.step.die(FAILED, f"Could not reach GitHub's API ({github_api()}{path}). Check the network, then re-run.")
        self.step.die(FAILED, f"GitHub answered {status} to {path}: {msg}")

    def check_key(self):
        """The key is this App's: GitHub accepts a token it signed (GET /app) and names this App."""
        status, body = self.get("/app")
        if status != "200":
            self.fail(status, "/app", body)
        if str(body.get("id")) != self.id:
            self.step.die(REFUSED, f"The key at {self.key_file} signs for App ID {body.get('id')}, not {self.slug} ({self.id}). Use {self.slug}'s key.")
        return body


# ---- secrets and variables -----------------------------------------------------------------------

def _reserved(step, key, flag):
    if key in RESERVED_VARS:
        step.die(REFUSED, f"{key} is a reserved name: the deploy sets it for every stack job, so a {flag} would override it.")
    if key.startswith(RESERVED_PREFIX):
        step.die(REFUSED, f"{key} is a reserved name: STACK_* variables carry earlier stacks' outputs.")


def parse_var(step, spec):
    """A GitLab `--var KEY=VALUE[:protected][:masked]` → (key, value, protected, masked). Masked
    (hidden in job logs) is for a secret a `noEcho` parameter takes; GitLab requires such a value to
    be at least 8 characters with no spaces."""
    protected = masked = False
    body = spec
    while True:
        if body.endswith(":protected"):
            protected, body = True, body[: -len(":protected")]
        elif body.endswith(":masked"):
            masked, body = True, body[: -len(":masked")]
        else:
            break
    if "=" not in body:
        step.die(REFUSED, f"--var needs KEY=VALUE[:protected][:masked], got '{spec.split('=')[0]}'")
    key, value = body.split("=", 1)
    _reserved(step, key, "variable")
    return key, value, protected, masked


def record_deploy_env(project_dir, pairs):
    """Write KEY=VALUE lines into <project>/.cre/deploy.env (gitignored, 0600), which the local
    `npx projen deploy` reads for the variables CI gets from its CI/CD variables. Existing keys are
    updated, others kept. Never pass a masked/secret value here."""
    if not project_dir or not pairs:
        return
    path = os.path.join(project_dir, ".cre", "deploy.env")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines, seen = [], set()
    if os.path.isfile(path):
        with open(path) as f:
            for line in f.read().splitlines():
                k = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
                if k in pairs:
                    line = f"{k}={pairs[k]}"
                    seen.add(k)
                lines.append(line)
    else:
        lines.append("# Deploy variables for the local `npx projen deploy` (CI has them as CI/CD variables). Written by setup.")
    lines += [f"{k}={v}" for k, v in pairs.items() if k not in seen]
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")


def record_ci_variables(sf, names, secret_names=()):
    """Add variable names (never values) to cre-setup.json's ciVariables / secretVariables, which
    check:deploy-prereqs compares with what the stacks need."""
    for key, new in (("ciVariables", names), ("secretVariables", secret_names)):
        if not new:
            continue
        cur = sf.data.get(key) or []
        merged = sorted(set(cur) | set(new))
        if merged != cur:
            sf.set(key, merged)


def parse_secret(step, spec):
    """A GitHub `--secret KEY=VALUE` → (key, value). Taken whole (no `:protected`: GitHub has no
    such flag); set later on stdin, never argv."""
    if "=" not in spec:
        step.die(REFUSED, f"--secret needs KEY=VALUE, got '{spec.split('=')[0]}'")
    key, value = spec.split("=", 1)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        step.die(REFUSED, f"--secret name '{key}' must be letters, digits and _ (not starting with a digit).")
    if key.upper().startswith("GITHUB_"):
        step.die(REFUSED, f"--secret {key}: GitHub refuses secret names starting with GITHUB_.")
    _reserved(step, key, "secret")
    return key, value


# ---- repo bootstrap (both providers) -------------------------------------------------------------

def git(*args, cwd, env=None):
    return run(["git"] + list(args), cwd=cwd, env=env)


def require_git_identity(step, cwd=None):
    name = git("config", "user.name", cwd=cwd).stdout.strip()
    email = git("config", "user.email", cwd=cwd).stdout.strip()
    if not name or not email:
        step.die(REFUSED, 'git has no identity for the first commit. Run: git config --global user.name "<Your Name>" && '
                 "git config --global user.email <alias>@amazon.com   then re-run.")


def preflight_git(step, cwd):
    """Before the first commit and push: a git identity, and origin reachable without a prompt (an
    agent's shell has no terminal, so git can't ask for a password or a host key; it just fails)."""
    require_git_identity(step, cwd)
    url = git("config", "--get", "remote.origin.url", cwd=cwd).stdout.strip()
    env = {"GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": os.environ.get("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")}
    r = git("ls-remote", "--heads", "origin", cwd=cwd, env=env)
    if r.returncode == 0:
        return
    why = last_line(r.stderr or r.stdout)
    if url.startswith("https://github.com/"):
        step.die(CREDENTIALS, f"git can't authenticate to {url} without a prompt ({why}). Let gh supply your credentials: "
                 "gh auth setup-git   then re-run.")
    if url.startswith("git@github.com:"):
        step.die(CREDENTIALS, f"git can't reach {url} over SSH ({why}). Add your SSH key to GitHub (gh ssh-key add ~/.ssh/id_ed25519.pub), "
                 "accept github.com's host key once (ssh -o StrictHostKeyChecking=accept-new -T git@github.com), or switch gh to "
                 "HTTPS (gh config set git_protocol https, then gh auth setup-git); then re-run.")
    if "gitlab.aws.dev" in url:
        step.die(CREDENTIALS, f"git can't reach {url} ({why}). Refresh your Midway SSH certificate: mwinit -f -k ~/.ssh/id_ecdsa.pub   then re-run.")
    step.die(CREDENTIALS, f"git can't reach {url} without a prompt: {why}")


def remote_state(cwd):
    """origin/main after a fetch: empty | trivial | history | unknown.
    trivial: exactly one commit touching only README*, LICENSE* or .gitignore (a UI "initialize")."""
    heads = git("ls-remote", "--heads", "origin", cwd=cwd)
    if heads.returncode == 0 and not heads.stdout.strip():
        return "empty"
    if git("fetch", "-q", "origin", cwd=cwd).returncode != 0:
        return "unknown"
    count = git("rev-list", "--count", "origin/main", cwd=cwd)
    if count.returncode != 0 or count.stdout.strip() != "1":
        return "history"
    files = git("ls-tree", "-r", "--name-only", "origin/main", cwd=cwd).stdout.split()
    if all(re.fullmatch(r"(README[^/]*|LICENSE[^/]*|\.gitignore)", f) for f in files):
        return "trivial"
    return "history"


def bootstrap_push(step, cwd, message="chore: initial scaffold"):
    """Commit the scaffold (if not yet) and push main as the first commit. Handles an empty remote
    and a trivial one (merged, the scaffold winning on shared paths). Refuses real history (10).
    Until the first push lands, anything changed since the scaffold commit (a re-run after a failed
    push, a fix made in between) is added, so the first push is the whole scaffold."""
    preflight_git(step, cwd)
    if step.dry_run:
        step.plan("(dry run) commit the scaffold and push main as the first commit")
        return
    has_head = git("rev-parse", "-q", "--verify", "HEAD", cwd=cwd).returncode == 0
    pushed = git("config", "branch.main.crePushedSha", cwd=cwd).stdout.strip()
    dirty = bool(git("status", "--porcelain", cwd=cwd).stdout.strip())
    def commit(*args):
        r = git("commit", "-q", *args, cwd=cwd)
        if r.returncode != 0:
            sys.stderr.write(r.stdout + r.stderr)
        return r.returncode == 0

    if not has_head:
        git("add", "-A", cwd=cwd)
        if not commit("-m", message):
            step.die(FAILED, "The initial commit failed (git's output above: a commit hook, or commit signing).")
        step.ok(f"Committed the scaffold: {message}")
    elif not pushed and dirty:
        git("add", "-A", cwd=cwd)
        if git("rev-parse", "-q", "--verify", "HEAD~1", cwd=cwd).returncode != 0:
            if not commit("--amend", "--no-edit"):
                step.die(FAILED, "Adding the latest changes to the scaffold commit failed (git's output above).")
            step.ok("Added the changes made since the scaffold commit to it (nothing pushed yet).")
        else:
            # Already merged with a placeholder commit: amending would drop the merge. A new commit.
            if not commit("-m", f"{message} (changes since the first attempt)"):
                step.die(FAILED, "Committing the latest changes failed (git's output above).")
            step.ok("Committed the changes made since the scaffold commit (nothing pushed yet).")
    state = remote_state(cwd)
    if state == "trivial":
        step.info("origin has a one-commit placeholder (README/LICENSE/.gitignore); merging it, scaffold wins on conflicts.")
        r = git("merge", "-q", "--allow-unrelated-histories", "-X", "ours", "-m", "chore: merge initial repository commit",
                "origin/main", cwd=cwd)
        if r.returncode != 0:
            step.die(FAILED, f"Merging the remote's initial commit failed: {last_line(r.stderr)}")
    elif state == "history":
        if git("merge-base", "--is-ancestor", "origin/main", "HEAD", cwd=cwd).returncode == 0:
            step.info("origin/main is already contained in local main.")
        else:
            step.die(REFUSED, "origin already has real history, so this isn't a fresh project. Nothing was pushed. "
                     "Point this scaffold at an empty repo, or merge by hand.")
    elif state == "unknown":
        step.die(FAILED, "Could not fetch origin to inspect it.")
    step.plan("git push -u origin main")
    r = git("push", "-q", "-u", "origin", "main", cwd=cwd, env={"GIT_TERMINAL_PROMPT": "0"})
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
        step.die(FAILED, "git push -u origin main failed (see above). Fix the cause and re-run: anything changed meanwhile is added before the push.")
    git("config", "branch.main.crePushedSha", git("rev-parse", "HEAD", cwd=cwd).stdout.strip(), cwd=cwd)


def set_origin(step, cwd, url):
    if git("config", "--get", "remote.origin.url", cwd=cwd).stdout.strip() != url:
        git("remote", "remove", "origin", cwd=cwd)
        git("remote", "add", "origin", url, cwd=cwd)
        step.ok(f"origin → {url}")


def require_main(step, cwd):
    branch = git("branch", "--show-current", cwd=cwd).stdout.strip()
    if branch != "main":
        step.die(REFUSED, f"The scaffold must be on branch main for the first push (it is on '{branch}').")
