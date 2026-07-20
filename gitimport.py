"""PIOS repo history importer: back-fills memory from a repo's commit log.

The cold-start fix — a new user's memory is empty on day 1, but their git history
already holds months of real work at real timestamps. Emits one event per commit
with the commit's TRUE author timestamp.

Signals, not surveillance: commit subjects, authors, shas. Never diffs.

Duplicates: shas are deduped within a single call, but calling twice re-emits the
same commits. The sha is the first token of `detail` ('<sha> <author>') so a
caller that owns the DB can filter already-stored commits.
"""
import os
import subprocess
import sys
from typing import Callable

SEP = '\x1f'


def _log(msg: str) -> None:
    print(f'[pios.gitimport] {msg}', file=sys.stderr)


def import_repo(repo_path: str, emit: Callable[[dict], None], since_days: int = 90,
                max_commits: int = 500) -> int:
    """Emit one 'git' event per recent commit. Returns the number emitted."""
    name = os.path.basename(os.path.normpath(repo_path))
    cmd = ['git', '-C', repo_path, 'log', f'--since={since_days} days ago',
           f'--max-count={max_commits}', f'--format=%H{SEP}%at{SEP}%an{SEP}%s']
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        _log(f'{repo_path}: git unavailable or timed out: {e!r}')
        return 0
    if out.returncode != 0:  # not a repo, or empty repo (no HEAD)
        _log(f'{repo_path}: {out.stderr.decode("utf-8", "replace").strip()[:200]}')
        return 0

    seen, n = set(), 0
    for line in out.stdout.decode('utf-8', errors='replace').splitlines():
        parts = line.split(SEP)
        if len(parts) != 4:
            continue
        sha, ts, author, subject = parts
        if sha in seen:
            continue
        seen.add(sha)
        try:
            ts_f = float(ts)
        except ValueError:
            continue
        emit({'source': 'git', 'app': 'git', 'title': f'{name}: {subject}',
              'detail': f'{sha} {author}', 'ts': ts_f, 'dur_s': 0})
        n += 1
    return n


def import_all(config: dict, emit: Callable[[dict], None], **kw) -> int:
    """Import every repo in config['git_repos']. Returns total events emitted."""
    return sum(import_repo(r, emit, **kw) for r in (config.get('git_repos') or []))
