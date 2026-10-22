"""Shared library for the provider-neutral git tasks (branch, git:status, commit, push). Python 3.9+,
standard library only; imported by branch.py, status.py, commit.py and push.py.

Contract every task follows:
  - Read-only preflight first; nothing is changed until it passes.
  - Exit codes: 0 done (incl. no-op) | 10 refused, nothing changed | 20 validation or a commit hook
    failed | 30 conflict, the repo restored | 40 incomplete: this run already changed something (a
    branch made, a commit, a push) but didn't finish; re-running the same command resumes.
    A refusal after something was changed is reported as 40, never 10, with what was done.
  - The last line on stdout is always:
      RESULT code=<n> state=<STATE> branch=<b> sha=<sha> mr=<url|none>
  - Human messages go to stderr.
"""
import fnmatch
import json
import os
import re
import signal
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "setup"))
import crelib as c  # noqa: E402

OK, REFUSED, FAILED, CONFLICT, INCOMPLETE = 0, 10, 20, 30, 40
MAX_TITLE = 255   # GitLab's MR title limit (GitHub's is 256)


class Die(Exception):
    def __init__(self, code, state, msg):
        super().__init__(msg)
        self.code, self.state, self.msg = code, state, msg


def info(msg):
    print(f"ℹ️  {msg}", file=sys.stderr, flush=True)


def ok(msg):
    print(f"✅ {msg}", file=sys.stderr, flush=True)


def warn(msg):
    print(f"⚠️  {msg}", file=sys.stderr, flush=True)


def indent(items, pad="     "):
    return "\n".join(pad + i for i in items)


# ---- git -----------------------------------------------------------------------------------------

def git(*args, input=None):
    return subprocess.run(["git", *args], input=input, stdin=None if input is not None else subprocess.DEVNULL,
                          capture_output=True, text=True, errors="replace")


def gout(*args):
    """stdout of a git command, stripped; "" when it failed."""
    r = git(*args)
    return r.stdout.strip() if r.returncode == 0 else ""


def succeeds(*args):
    return git(*args).returncode == 0


def git_streamed(*args):
    """A git command whose output (hooks, remote messages) goes straight to stderr. Returns the code."""
    return subprocess.run(["git", *args], stdin=subprocess.DEVNULL, stdout=sys.stderr, stderr=sys.stderr).returncode


def head():
    return gout("rev-parse", "-q", "--verify", "HEAD")


def short(sha):
    return sha[:9] if sha else "none"


def rev_list(*args):
    return gout("rev-list", *args).split()


def is_dirty():
    return bool(gout("status", "--porcelain"))


# ---- arguments -----------------------------------------------------------------------------------

class Args:
    def __init__(self):
        self.files, self.allow, self.positional = [], [], []
        self.message = self.branch = self.title = ""


def parse_args(argv):
    """--files may repeat; each value is whitespace-separated, or newline-separated when a path
    contains a space. --name=value works too."""
    a = Args()
    i = 0
    while i < len(argv):
        x = argv[i]
        if x == "--":
            a.positional += argv[i + 1:]
            break
        name, eq, inline = x.partition("=") if x.startswith("--") else (x, "", "")
        flags = {"--files": "files", "--message": "message", "-m": "message", "--branch": "branch", "-b": "branch",
                 "--title": "title", "-t": "title", "--allow-secret-path": "allow"}
        if name in flags:
            if eq:
                val, i = inline, i + 1
            elif i + 1 < len(argv):
                val, i = argv[i + 1], i + 2
            else:
                raise Die(REFUSED, "BAD_ARGS", f"{name} needs a value")
            key = flags[name]
            if key == "files":
                a.files += [ln for ln in val.splitlines() if ln] if "\n" in val else val.split()
            elif key == "allow":
                a.allow.append(val)
            else:
                setattr(a, key, val)
        elif x.startswith("-") and x != "-":
            raise Die(REFUSED, "BAD_ARGS", f"Unknown argument: {x}")
        else:
            a.positional.append(x)
            i += 1
    return a


def check_message_and_title(a):
    """Before anything changes: git refuses a push option with a newline only AFTER the commit,
    and GitLab rejects an over-long title only after the push (the MR is then never opened)."""
    if not a.message.strip():
        raise Die(REFUSED, "BAD_ARGS", "--message is required.")
    if "\n" in a.title:
        raise Die(REFUSED, "BAD_ARGS", "--title must be a single line (git push options cannot contain newlines).")
    a.title = a.title or a.message.splitlines()[0]
    if len(a.title) > MAX_TITLE:
        raise Die(REFUSED, "BAD_ARGS", f"The MR/PR title is {len(a.title)} characters; GitLab allows {MAX_TITLE}. "
                  "Shorten --title (or the message's first line).")


# ---- the task: state, result, lock ---------------------------------------------------------------

class Task:
    def __init__(self, name):
        self.name = name
        self.state, self.branch, self.sha, self.mr = "UNKNOWN", "", "", "none"
        self.done = []          # what this run changed, for an incomplete (40) report
        self.args = Args()
        self.provider = None
        self.default = ""
        self.lock_dir = ""
        # classification
        self.cls = self.current = self.carry = self.note = self.start_head = ""
        self.merged_reason = ""
        self.state_override = ""   # a provider's finding that replaces the final state (MERGED)

    def result(self, code):
        sha = self.sha or head() or "none"
        print(f"RESULT code={code} state={self.state} branch={self.branch or 'none'} sha={sha} mr={self.mr or 'none'}", flush=True)

    def lock(self):
        """One run per clone (all worktrees share the common git dir). mkdir is atomic; a lock whose
        process is gone is reclaimed."""
        d = os.path.join(gout("rev-parse", "--path-format=absolute", "--git-common-dir") or ".git", "cre-git.lock")
        try:
            os.mkdir(d)
        except FileExistsError:
            try:
                pid = int(open(os.path.join(d, "pid")).read().strip())
            except (OSError, ValueError):
                pid = 0
            if pid and pid_alive(pid):
                raise Die(REFUSED, "LOCKED", f"Another git task (pid {pid}) is running in this clone. Wait for it, or remove {d} if it is stale.")
            for f in os.listdir(d):
                os.remove(os.path.join(d, f))
            try:
                os.rmdir(d)
                os.mkdir(d)
            except OSError:
                raise Die(REFUSED, "LOCKED", f"Could not take the lock at {d}.")
        with open(os.path.join(d, "pid"), "w") as f:
            f.write(str(os.getpid()))
        self.lock_dir = d

    def unlock(self):
        if self.lock_dir:
            try:
                os.remove(os.path.join(self.lock_dir, "pid"))
            except OSError:
                pass
            try:
                os.rmdir(self.lock_dir)
            except OSError:
                pass
            self.lock_dir = ""


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def run_task(name, body):
    """Run body(task, argv); report and exit with the contract's code and RESULT line."""
    t = Task(name)
    code = OK

    def on_term(signum, frame):
        raise Die(FAILED, "INTERRUPTED", f"Stopped by signal {signum}.")
    signal.signal(signal.SIGTERM, on_term)
    try:
        body(t, sys.argv[1:])
    except Die as d:
        code, t.state, msg = d.code, d.state, d.msg
        if t.done:
            if code == REFUSED:
                code = INCOMPLETE
            msg += f"\n   Already done by this run: {'; '.join(t.done)}. Fix the cause, then re-run the same command; it resumes."
        print(f"❌ {msg}", file=sys.stderr, flush=True)
    except KeyboardInterrupt:
        code, t.state = FAILED, "INTERRUPTED"
        print("❌ Interrupted." + (f" Already done: {'; '.join(t.done)}." if t.done else ""), file=sys.stderr)
    finally:
        if t.provider is not None:
            try:
                t.provider.close()
            except Exception:  # noqa: BLE001 - cleanup must not mask the result
                pass
        t.unlock()
    if not t.branch:
        t.branch = gout("branch", "--show-current")
    t.result(code)
    sys.exit(code)


# ---- environment ---------------------------------------------------------------------------------

def require_git():
    m = re.search(r"(\d+)\.(\d+)", gout("--version"))
    # merge-tree --write-tree (merged and conflict detection) needs git >= 2.38.
    if not m or (int(m.group(1)), int(m.group(2))) < (2, 38):
        raise Die(REFUSED, "GIT_TOO_OLD", f"{gout('--version') or 'git'} is too old; these tasks need git >= 2.38 (merge-tree --write-tree).")


def provider_name():
    """Set by the projen class in the task env; otherwise derived from origin, so the tasks also work
    in a repo that isn't projen-managed (cre-demo-template itself)."""
    if os.environ.get("CRE_GIT_PROVIDER"):
        return os.environ["CRE_GIT_PROVIDER"]
    url = gout("remote", "get-url", "origin")
    return "github" if "github.com" in url else "gitlab" if "gitlab" in url else "unknown"


def preamble(t, argv):
    """Arguments, repo root (caller-relative --files re-anchored), git version, origin, lock,
    provider, and no half-finished git operation."""
    t.args = parse_args(argv)
    prefix = gout("rev-parse", "--show-prefix")
    root = gout("rev-parse", "--show-toplevel")
    if not root:
        raise Die(REFUSED, "NOT_A_REPO", "Not inside a git repository.")
    # Paths are relative to where the command ran. Under `npx projen` that is always the repo root
    # (projen runs tasks there), so they are repo-root relative in practice.
    if prefix:
        t.args.files = [f if os.path.isabs(f) else prefix + f for f in t.args.files]
    os.chdir(root)
    require_git()
    if not succeeds("remote", "get-url", "origin"):
        raise Die(REFUSED, "NO_ORIGIN", "No 'origin' remote is configured.")
    t.lock()
    name = provider_name()
    import cregit_providers as providers
    if name == "gitlab":
        t.provider = providers.GitLab(t)
    elif name == "github":
        t.provider = providers.GitHub(t)
    else:
        raise Die(REFUSED, "UNKNOWN_PROVIDER", f"Cannot tell GitHub from GitLab (origin: {gout('remote', 'get-url', 'origin') or 'none'}). "
                  "Set CRE_GIT_PROVIDER=gitlab|github.")
    op = in_progress_op()
    if op:
        raise Die(REFUSED, "IN_PROGRESS", f"A {op} is in progress. Finish it (git {op} --continue) or abort it (git {op} --abort), then re-run.")


def in_progress_op():
    def exists(p):
        return os.path.exists(gout("rev-parse", "--git-path", p))
    if exists("rebase-merge") or exists("rebase-apply"):
        return "rebase"
    for path, op in (("MERGE_HEAD", "merge"), ("CHERRY_PICK_HEAD", "cherry-pick"), ("REVERT_HEAD", "revert"), ("BISECT_LOG", "bisect")):
        if exists(path):
            return op
    return ""


def fetch():
    """Fails CLOSED: every branch decision depends on fresh remote state."""
    r = git("fetch", "--quiet", "--prune", "origin")
    if r.returncode != 0:
        raise Die(REFUSED, "ORIGIN_UNREACHABLE", "git fetch origin failed, so the remote state is unknown and no branch decision is safe:\n"
                  + r.stderr.strip())


def default_branch():
    """Explicit override > what the remote says > local origin/HEAD > main/master. "" when the remote
    has no branches at all (a brand-new repo)."""
    override = os.environ.get("CRE_GIT_TARGET") or os.environ.get("GL_PUSH_TARGET")
    if override:
        return override
    for line in git("ls-remote", "--symref", "origin", "HEAD").stdout.splitlines():
        if line.startswith("ref:"):
            return line.split()[1].replace("refs/heads/", "", 1)
    h = gout("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if h:
        return h[len("origin/"):] if h.startswith("origin/") else h
    for b in ("main", "master"):
        if succeeds("rev-parse", "-q", "--verify", f"refs/remotes/origin/{b}"):
            return b
    return "main" if git("ls-remote", "--heads", "origin").stdout.strip() else ""


def is_protected(t, b):
    return b in ("main", "master", t.default)


def remote_branch(b):
    """(present, sha) via ls-remote. Exit 2 from --exit-code is "no such ref"; anything else is a
    failure, never an absence (that once made an unreachable origin read as "deleted on merge")."""
    r = git("ls-remote", "--exit-code", "--heads", "origin", f"refs/heads/{b}")
    if r.returncode == 0:
        return True, r.stdout.split()[0]
    if r.returncode == 2:
        return False, ""
    raise Die(REFUSED, "ORIGIN_UNREACHABLE", f"git ls-remote origin failed (exit {r.returncode}); cannot tell whether {b} exists on the remote.")


def local_branch_exists(b):
    return succeeds("show-ref", "--verify", "--quiet", f"refs/heads/{b}")


def validate_new_branch_name(t, name):
    if not name:
        raise Die(REFUSED, "NO_BRANCH_NAME", "A new branch is needed but no name was given. Pass --branch <type>/<description>.")
    if not succeeds("check-ref-format", "--branch", name):
        raise Die(REFUSED, "BAD_BRANCH_NAME", f"'{name}' is not a valid branch name.")
    if "@" in name or name == "HEAD" or name.startswith(("origin/", "refs/", "remotes/", "-")):
        raise Die(REFUSED, "BAD_BRANCH_NAME", f"'{name}' would be confused with another ref; use <type>/<description> (e.g. feat/add-edge).")
    if len(name) > 100:
        raise Die(REFUSED, "BAD_BRANCH_NAME", f"'{name}' is {len(name)} characters; keep branch names to 100.")
    if succeeds("show-ref", "--verify", "--quiet", f"refs/tags/{name}"):
        raise Die(REFUSED, "BAD_BRANCH_NAME", f"'{name}' is also a tag; pick another name.")
    if is_protected(t, name):
        raise Die(REFUSED, "BAD_BRANCH_NAME", f"'{name}' is the default/protected branch; pick a feature branch name.")
    if local_branch_exists(name):
        raise Die(REFUSED, "BRANCH_EXISTS", f"Branch {name} already exists locally. A branch is never reused after its MR/PR merges; pick a "
                  "new name. If it is unmerged work you meant to continue, check it out and re-run without --branch.")
    if remote_branch(name)[0]:
        raise Die(REFUSED, "BRANCH_EXISTS", f"Branch {name} already exists on origin. Pick a new name.")


# ---- pushed-SHA record ---------------------------------------------------------------------------
# Written after every successful push. Survives `git fetch --prune` deleting the remote-tracking ref
# after a merge, which is exactly when we need to know what we pushed.

def record_pushed(b, sha):
    git("config", f"branch.{b}.crePushedSha", sha)


def pushed_sha(b):
    return gout("config", "--get", f"branch.{b}.crePushedSha")


def was_pushed(b):
    """Our own record, or upstream config pointing at the branch's OWN name on origin (a
    `git checkout -b feat origin/main` sets merge=refs/heads/main, which must not count)."""
    if pushed_sha(b):
        return True
    return gout("config", "--get", f"branch.{b}.remote") == "origin" and gout("config", "--get", f"branch.{b}.merge") == f"refs/heads/{b}"


def contained_in_default(t, commit):
    """Everything in <commit> is already in origin/<default>: merging it wouldn't change the default
    branch's tree. Catches squash merges, which ancestry checks miss."""
    if not succeeds("cat-file", "-e", f"{commit}^{{commit}}"):
        return False
    r = git("merge-tree", "--write-tree", f"refs/remotes/origin/{t.default}", commit)
    merged = r.stdout.split("\n", 1)[0].strip() if r.stdout else ""
    return bool(merged) and merged == gout("rev-parse", f"refs/remotes/origin/{t.default}^{{tree}}")


def branch_merged(t, b):
    """(merged, deleted_unmerged). Sets t.merged_reason. Gated on the branch having been pushed: a
    fresh branch's tip IS origin/main and would otherwise always look contained."""
    t.merged_reason = ""
    if not was_pushed(b):
        return False, False
    present, rsha = remote_branch(b)
    p = pushed_sha(b) or rsha
    if not present:
        # Deleted on merge, or deleted unmerged: only its content landing in the default branch tells.
        if not p or contained_in_default(t, p):
            t.merged_reason = "the remote branch is gone (deleted on merge)"
            return True, False
        return False, True
    if contained_in_default(t, p):
        t.merged_reason = f"everything pushed is already in origin/{t.default} (merged; squash merges included)"
        return True, False
    return False, False


def conflicts_with_default(t):
    """Files that conflict between HEAD and origin/<default>; [] when it merges."""
    r = git("merge-tree", "--write-tree", "--name-only", "--no-messages", f"refs/remotes/origin/{t.default}", "HEAD")
    if r.returncode != 1:
        return []
    return [ln for ln in r.stdout.splitlines()[1:] if ln]


# ---- classification ------------------------------------------------------------------------------

def classify(t):
    """Read-only (after fetch). Sets t.cls (BOOTSTRAP | DETACHED | ON_DEFAULT | MERGED |
    MERGED_UNKNOWN_BASE | CLOSED | ACTIVE | NEW), t.current, t.carry (a rev range to carry onto a
    new branch, "" for none), t.note."""
    t.current = gout("branch", "--show-current")
    t.carry = t.note = ""
    t.start_head = head()
    if not t.default:
        t.cls, t.note = "BOOTSTRAP", "origin has no branches yet; the first push goes directly to main."
        return
    if not t.current:
        t.cls, t.note = "DETACHED", "detached HEAD; a new branch is created at HEAD (its commits stay)."
        return
    b = t.current
    if is_protected(t, b):
        t.cls = "ON_DEFAULT"
        if succeeds("rev-parse", "-q", "--verify", f"refs/remotes/origin/{b}") and rev_list(f"refs/remotes/origin/{b}..HEAD"):
            t.carry = f"refs/remotes/origin/{b}..HEAD"
            t.note = f"on {b} with unpushed commits; they move to a new branch."
        else:
            t.note = f"on {b}; work moves to a new branch from origin/{t.default}."
        return
    merged, deleted_unmerged = branch_merged(t, b)
    if merged:
        t.cls = "MERGED"
        p = pushed_sha(b)
        if p and succeeds("cat-file", "-e", f"{p}^{{commit}}"):
            if rev_list(f"{p}..HEAD"):
                t.carry = f"{p}..HEAD"
        elif not contained_in_default(t, "HEAD"):
            # Pushed and merged, but no record of what was pushed (an older tool pushed it), so
            # post-merge commits can't be told from the merged ones.
            t.cls = "MERGED_UNKNOWN_BASE"
        t.note = f"{b} looks merged: {t.merged_reason}."
        return
    if deleted_unmerged:
        # Its MR/PR was closed and the branch deleted, or someone deleted it: what was pushed never
        # reached the default branch, so all of it (not only what came after the push) moves on.
        t.cls = "CLOSED"
        t.note = f"{b} was deleted on origin without being merged; its commits move to a new branch."
        if rev_list(f"refs/remotes/origin/{t.default}..HEAD"):
            t.carry = f"refs/remotes/origin/{t.default}..HEAD"
        return
    if was_pushed(b):
        # Only a branch we pushed can have a closed MR/PR of its own; a new branch that reuses an
        # old name must not inherit that MR's state.
        if t.provider.branch_closed(b):
            t.cls = "CLOSED"
            t.note = f"{b}'s MR/PR was closed without merging; a closed branch is done."
            p = pushed_sha(b)
            if p and git("rev-list", f"{p}..HEAD").returncode == 0 and rev_list(f"{p}..HEAD"):
                t.carry = f"{p}..HEAD"
            return
        t.cls, t.note = "ACTIVE", f"{b} is pushed and not merged; pushing continues it."
    else:
        t.cls, t.note = "NEW", f"{b} has never been pushed."


# ---- new branch ----------------------------------------------------------------------------------

def new_branch(t, name, carry_range):
    """Create <name> from origin/<default>, re-applying the commits in carry_range and the working
    changes. On any conflict the repository is put back exactly as it was (exit 30)."""
    orig = gout("branch", "--show-current")
    commits = []
    if carry_range:
        # Never commits the default branch already has (a merge of main into the branch brings them).
        commits = rev_list("--reverse", "--no-merges", carry_range, "--not", f"refs/remotes/origin/{t.default}")
    stash = ""
    untracked = []
    if is_dirty():
        before = gout("rev-parse", "-q", "--verify", "refs/stash")
        if not succeeds("stash", "push", "-q", "--include-untracked", "-m", f"cre-git: carrying work to {name}"):
            raise Die(REFUSED, "STASH_FAILED", "git stash failed; nothing was changed.")
        stash = gout("rev-parse", "-q", "--verify", "refs/stash")
        # Only a submodule's own changes (nothing stashable): no new entry. The top of the stack is
        # then someone else's older stash, which must never be applied or dropped.
        if stash == before:
            stash = ""
        if stash and succeeds("cat-file", "-e", f"{stash}^3"):
            untracked = [p for p in gout("ls-tree", "-r", "--name-only", f"{stash}^3").splitlines() if p]

    def rollback(why):
        git("cherry-pick", "--abort")
        if stash:
            git("reset", "-q", "--hard")
            for p in untracked:
                try:
                    os.remove(p)
                except OSError:
                    pass
        if orig:
            git("switch", "-q", orig)
        else:
            git("switch", "-q", "--detach", t.start_head)
        git("branch", "-q", "-D", name)
        if stash:
            if not succeeds("stash", "apply", "-q", "--index", stash) and not succeeds("stash", "apply", "-q", stash):
                raise Die(CONFLICT, "CONFLICT", f"{why}. Restoring your changes also failed; they are safe in: git stash apply {stash}")
            drop_stash(stash)
        raise Die(CONFLICT, "CONFLICT", f"{why}. Nothing changed: you are back on {orig or 'the detached HEAD'} with your work as it was.")

    # Never `git checkout main`: it fails when main is checked out in another worktree.
    if not succeeds("switch", "-q", "--no-track", "-c", name, f"refs/remotes/origin/{t.default}"):
        rollback(f"git switch -c {name} failed")
    if commits and not succeeds("cherry-pick", "--allow-empty", "--keep-redundant-commits", *commits):
        rollback(f"Re-applying your commits onto origin/{t.default} conflicts ({len(commits)} commit(s) from {carry_range})")
    if stash:
        if not succeeds("stash", "apply", "-q", stash):
            rollback(f"Your uncommitted changes conflict with origin/{t.default}")
        drop_stash(stash)
    # Last, so a rollback never has to undo it: the local default branch goes back to origin's. Its
    # unpushed commits now live on the new branch; left there, the next new branch would carry them
    # again. Fails harmlessly when it's checked out in another worktree.
    if orig == t.default:
        git("branch", "-q", "-f", t.default, f"refs/remotes/origin/{t.default}")
    what = f"created {name} from origin/{t.default}"
    if commits:
        what += f", carrying {len(commits)} commit(s)"
    if stash:
        what += " and your uncommitted changes"
    t.done.append(what)
    ok(what[0].upper() + what[1:] + ".")


def branch_at_head(t, name):
    """A detached HEAD: the new branch starts at HEAD, so its commits (and the working tree) stay."""
    if not succeeds("switch", "-q", "-c", name):
        raise Die(REFUSED, "BRANCH_FAILED", f"git switch -c {name} failed.")
    t.done.append(f"created {name} at the detached HEAD")
    ok(f"Created {name} at the detached HEAD.")


def drop_stash(sha):
    """Drop exactly our entry: the stash stack is shared by every worktree of the clone."""
    for line in gout("stash", "list", "--format=%H %gd").splitlines():
        h, _, ref = line.partition(" ")
        if h == sha:
            git("stash", "drop", "-q", ref)
            return


def resolve_branch(t, requested):
    """The branch to work on, creating one when the state requires it. Sets t.branch."""
    if t.cls == "DETACHED":
        validate_new_branch_name(t, requested)
        branch_at_head(t, requested)
    elif t.cls in ("ON_DEFAULT", "MERGED", "CLOSED"):
        if not requested:
            raise Die(REFUSED, f"NEEDS_BRANCH_{t.cls}", f"{t.note} Re-run with --branch <type>/<description>.")
        validate_new_branch_name(t, requested)
        info(t.note)
        new_branch(t, requested, t.carry)
    elif t.cls == "MERGED_UNKNOWN_BASE":
        raise Die(REFUSED, "MERGED_UNKNOWN_BASE", f"{t.note} It has local commits, but no record of what was pushed, so the new commits "
                  f"can't be separated from the merged ones. Create a branch from origin/{t.default} yourself and cherry-pick the new commits.")
    elif requested and requested != t.current:
        info(f"Continuing {t.current} ({t.note}); --branch {requested} is only used when a new branch is needed.")
    t.branch = gout("branch", "--show-current")


# ---- validate / stage / commit -------------------------------------------------------------------

def is_projen():
    return os.path.isfile(".projen/tasks.json") or any(f.startswith(".projenrc.") for f in os.listdir("."))


def validate(t, why=""):
    """Re-synth, then build. Synth first so task-definition changes in .projenrc take effect."""
    if os.environ.get("CRE_SKIP_VALIDATE"):
        info("Validation skipped (CRE_SKIP_VALIDATE set).")
        return
    if not is_projen():
        info("Not a projen project; skipping projen validation. Run the project's own build/tests first.")
        return
    log = os.path.join(gout("rev-parse", "--path-format=absolute", "--git-common-dir"), "cre-git-validate.log")
    info(f"Validating{' ' + why if why else ''}: npx projen, then npx projen build (log: {log})")
    with open(log, "w") as f:
        for cmd, state, what in ((["npx", "projen"], "VALIDATE_FAILED", "npx projen (synth)"),
                                 (["npx", "projen", "build"], "BUILD_FAILED", "npx projen build")):
            f.flush()
            if subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT).returncode != 0:
                f.flush()
                sys.stderr.write("\n".join(open(log, errors="replace").read().splitlines()[-30:]) + "\n")
                raise Die(FAILED, state, f"{what} failed. Nothing was staged or committed by this step.")


# Paths that hold credentials, matched against the file name, or (entries with a `/`) the
# repo-relative path. *.example / *.sample / *.template are always allowed.
SECRET_GLOBS = (".env .env.* *.pem *.key *.p12 *.pfx *.jks *.keystore id_rsa* id_ecdsa* id_ed25519* credentials credentials.* "
                ".netrc _netrc *.tfvars *.tfvars.json *.tfstate *.tfstate.* secrets.* *secret*.json *.har *cookie* cookies.txt "
                "*.kdbx .git-credentials .pgpass gitlab-token").split()
SECRET_PATH_GLOBS = ".aws/* */.aws/* .ssh/* */.ssh/* .docker/config.json */.docker/config.json .gitlab/access_token .kube/config".split()
# Config files often committed on purpose: refused only when they hold a credential.
SECRET_IF_TOKEN = (".npmrc", ".yarnrc.yml", ".pypirc")
# Well-known credential formats, looked for in every staged file's content.
SECRET_CONTENT = re.compile(rb"AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|-----BEGIN ([A-Z]+ )?PRIVATE KEY-----|glpat-[A-Za-z0-9_-]{20}|"
                            rb"gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{40,}|xox[baprs]-[A-Za-z0-9-]{10,}|npm_[A-Za-z0-9]{36}|"
                            rb"_authToken *= *[^ $]|aws_secret_access_key *= *[A-Za-z0-9/+]{40}")
CONFIG_AUTH = re.compile(rb"(_auth|_authToken|password|token) *[:=]", re.I)


def is_secret_path(t, p):
    base = p.rsplit("/", 1)[-1]
    if p in t.args.allow or base.endswith((".example", ".sample", ".template")):
        return False
    return any(fnmatch.fnmatchcase(base, g) for g in SECRET_GLOBS) or any(fnmatch.fnmatchcase(p, g) for g in SECRET_PATH_GLOBS)


def has_secret_content(t, p):
    """The STAGED content of <path> holds a credential (reads the index, not the working tree)."""
    if p in t.args.allow:
        return False
    data = subprocess.run(["git", "show", f":{p}"], stdin=subprocess.DEVNULL, capture_output=True).stdout
    if SECRET_CONTENT.search(data):
        return True
    return p.rsplit("/", 1)[-1] in SECRET_IF_TOKEN and bool(CONFIG_AUTH.search(data))


def projen_managed():
    if not os.path.isfile(".projen/files.json"):
        return []
    try:
        files = json.load(open(".projen/files.json")).get("files", [])
    except (OSError, ValueError, AttributeError):
        files = []
    return [f for f in files if isinstance(f, str)] + [".projen/files.json", ".projen/tasks.json", ".projen/deps.json"]


def staged(*extra):
    return [p for p in subprocess.run(["git", "diff", "--cached", "--name-only", "-z", *extra], stdin=subprocess.DEVNULL,
                                      capture_output=True, text=True, errors="replace").stdout.split("\0") if p]


def stage(t):
    """Stage the requested paths plus every changed projen-managed file, so a commit never leaves out
    regenerated config. Refuses gitlinks, credentials and large files, leaving nothing staged."""
    managed = projen_managed()
    files = t.args.files
    if files:
        # --files limits the commit: something already staged outside them would go in too
        # (projen-managed files excepted: they're always staged).
        outside = [p for p in staged("--", ".", *[f":(exclude){f}" for f in files]) if p not in managed]
        if outside:
            raise Die(REFUSED, "STAGED_OUTSIDE_FILES", "These are already staged but aren't in --files, so they would be committed too:\n"
                      + indent(outside) + "\n   Unstage them (git restore --staged <path>) or add them to --files, then re-run.")
    files = files or ["."]
    r = git("add", "--dry-run", "--", *files)
    if r.returncode != 0:
        raise Die(REFUSED, "BAD_PATHSPEC", f"git add refused the --files paths ({' '.join(files)}): {r.stderr.strip()}")
    if not succeeds("add", "--", *files):
        raise Die(REFUSED, "BAD_PATHSPEC", f"git add failed for {' '.join(files)}.")
    for f in managed:
        if os.path.lexists(f) or succeeds("ls-files", "--error-unmatch", "--", f):
            git("add", "-A", "--", f)

    gitlinks = []
    for line in gout("diff", "--cached", "--raw", "--diff-filter=A").splitlines():
        meta, _, path = line.partition("\t")
        if len(meta.split()) > 1 and meta.split()[1] == "160000":
            gitlinks.append(path)
    if gitlinks:
        git("reset", "-q")
        raise Die(REFUSED, "GITLINK", "Refusing to commit embedded git repositories (nested clones / agent worktrees):\n" + indent(gitlinks)
                  + "\n   Nothing is staged. Add them to .gitignore (agent worktrees: .claude/worktrees/) and re-run.")
    max_mb = int(os.environ.get("CRE_MAX_FILE_MB") or 10)
    secrets, large = [], []
    for p in staged("--diff-filter=AM"):
        if is_secret_path(t, p) or has_secret_content(t, p):
            secrets.append(p)
            continue
        try:
            size = int(gout("cat-file", "-s", f":{p}") or 0)
        except ValueError:
            size = 0
        if size > max_mb * 1024 * 1024 and p not in t.args.allow:
            large.append(f"{p} ({size // 1024 // 1024} MB)")
    if secrets:
        git("reset", "-q")
        raise Die(REFUSED, "SECRET_PATH", "Refusing to commit files that look like, or contain, credentials:\n" + indent(secrets)
                  + "\n   Nothing is staged. Add them to .gitignore (and remove any real credential from the file), or\n"
                  "   pass --allow-secret-path <path> for each one if it is intentional.")
    if large:
        git("reset", "-q")
        raise Die(REFUSED, "LARGE_FILE", f"Refusing to commit files over {max_mb} MB (they stay in the history for good):\n" + indent(large)
                  + "\n   Nothing is staged. Add them to .gitignore (build output belongs in dist/ or cdk.out/), or pass\n"
                  "   --allow-secret-path <path> for one that must be committed (CRE_MAX_FILE_MB raises the limit).")
    names = gout("diff", "--cached", "--name-status")
    if names:
        info("Staged:\n" + indent(names.splitlines()))


def commit(t):
    """True when a commit was made, False when there was nothing to commit."""
    if succeeds("diff", "--cached", "--quiet"):
        return False
    if git_streamed("commit", "-q", "-m", t.args.message) != 0:
        raise Die(FAILED, "COMMIT_HOOK_FAILED", "git commit failed (a commit hook rejected it). Nothing was committed; your changes are still staged.")
    sha = head()
    t.done.append(f"committed {short(sha)}")
    ok(f"Committed {short(sha)}: {t.args.message.splitlines()[0]}")
    return True
