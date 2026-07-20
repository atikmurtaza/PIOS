"""PIOS git sensor: notices branch switches and new commits in watched repos.

Signals, not surveillance: branch names and commit SUBJECTS only — never diffs,
never file contents. State is read straight off disk (.git/HEAD, refs, packed-refs);
`git` is only shelled out to (with a timeout, no shell=True) to resolve one commit
subject, and only when a new sha actually appeared.
"""
import os
import subprocess
import sys
import threading
import time
from typing import Callable

POLL_S = 60.0  # git state changes slowly; nowhere near window-sensor speed


def _log(msg: str) -> None:
    print(f'[pios.gitsensor] {msg}', file=sys.stderr)


def git_dir(repo: str) -> str | None:
    """The repo's .git directory (follows the 'gitdir: ...' file of worktrees)."""
    p = os.path.join(repo, '.git')
    if os.path.isdir(p):
        return p
    if os.path.isfile(p):  # linked worktree / submodule
        with open(p, encoding='utf-8', errors='replace') as f:
            head = f.read().strip()
        if head.startswith('gitdir:'):
            target = head[7:].strip()
            return target if os.path.isabs(target) else os.path.join(repo, target)
    return None


def read_state(repo: str) -> tuple[str, str] | None:
    """(branch, sha) for a repo by reading files only. None if unreadable/empty."""
    gd = git_dir(repo)
    if not gd:
        return None
    try:
        with open(os.path.join(gd, 'HEAD'), encoding='utf-8', errors='replace') as f:
            head = f.read().strip()
    except OSError:
        return None
    if not head.startswith('ref:'):
        return ('(detached)', head) if head else None
    ref = head[4:].strip()                      # refs/heads/<branch>
    # strip the prefix, don't split on the last '/': branch names routinely
    # contain slashes (feature/x, fix/PIOS-12, release/1.0)
    branch = (ref[len('refs/heads/'):] if ref.startswith('refs/heads/')
              else ref.rsplit('/', 1)[-1])
    try:  # loose ref
        with open(os.path.join(gd, *ref.split('/')), encoding='utf-8') as f:
            return branch, f.read().strip()
    except OSError:
        pass
    try:  # packed-refs fallback
        with open(os.path.join(gd, 'packed-refs'), encoding='utf-8',
                  errors='replace') as f:
            for line in f:
                if line.startswith(('#', '^')):
                    continue
                parts = line.split()
                if len(parts) == 2 and parts[1] == ref:
                    return branch, parts[0]
    except OSError:
        pass
    return branch, ''  # branch exists, no commits yet (fresh repo)


def commit_subject(repo: str, sha: str) -> str:
    """One commit's subject line. '' if git is missing/slow/unhappy."""
    try:
        out = subprocess.run(['git', '-C', repo, 'log', '-1', '--format=%s', sha],
                             capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        _log(f'subject lookup failed for {repo}: {e!r}')
        return ''
    if out.returncode != 0:
        return ''
    return out.stdout.decode('utf-8', errors='replace').strip()


class GitSensor:
    """Polls configured repos; emits only when branch or commit sha changes.

    First poll of a repo establishes the baseline and emits nothing.
    Event: {'source':'git','app':'git','title':...,'detail':'<repo> <sha>',
            'ts':now,'dur_s':0}
    """

    def __init__(self, emit: Callable[[dict], None], repos: list,
                 poll_s: float = POLL_S):
        self.emit = emit
        self.repos = list(repos)
        self.poll_s = poll_s
        self._state = {}  # repo -> (branch, sha)
        self._stop = threading.Event()
        self._thread = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name='pios-git-sensor',
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.wait(self.poll_s):
            self.tick()

    def tick(self) -> None:
        """One poll of every repo. Never raises."""
        for repo in self.repos:
            try:
                self._tick_repo(repo)
            except Exception as e:  # unmounted drive, deleted folder, corrupt .git
                _log(f'{repo}: {e!r}')

    def _tick_repo(self, repo: str) -> None:
        state = read_state(repo)
        if state is None:
            self._state.pop(repo, None)  # gone; re-baseline if it comes back
            return
        prev = self._state.get(repo)
        self._state[repo] = state
        if prev is None or state == prev:
            return
        branch, sha = state
        name = os.path.basename(os.path.normpath(repo))
        if branch != prev[0]:
            title = f'{name}: switched to {branch}'
        else:
            subject = commit_subject(repo, sha) or sha[:7]
            title = f'{name}: committed "{subject}" on {branch}'
        self.emit({'source': 'git', 'app': 'git', 'title': title,
                   'detail': f'{repo} {sha}', 'ts': time.time(), 'dur_s': 0})
