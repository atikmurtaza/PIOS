"""Git sensor + history importer, against a real temporary git repo."""
import os
import shutil
import subprocess
import time

import pytest

from pios import gitimport, gitsensor

pytestmark = pytest.mark.skipif(shutil.which('git') is None, reason='git not on PATH')


def _git(repo, *args):
    subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True,
                   timeout=30)


def _commit(repo, filename, msg):
    (repo / filename).write_text(msg, encoding='utf-8')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-m', msg)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / 'demo'
    r.mkdir()
    _git(r, 'init', '-b', 'main')
    _git(r, 'config', 'user.email', 'test@example.com')
    _git(r, 'config', 'user.name', 'Tester')
    _commit(r, 'a.txt', 'first commit')
    _commit(r, 'b.txt', 'second commit')
    return r


def test_sensor_detects_commit_and_branch_switch(repo):
    events = []
    s = gitsensor.GitSensor(events.append, [str(repo)])

    s.tick()                       # baseline poll: emits nothing
    assert events == []

    _commit(repo, 'c.txt', 'third commit')
    s.tick()
    assert len(events) == 1
    ev = events[0]
    assert ev['source'] == 'git' and ev['app'] == 'git' and ev['dur_s'] == 0
    assert ev['title'] == 'demo: committed "third commit" on main'
    assert str(repo) in ev['detail'] and len(ev['detail'].split()[-1]) == 40
    assert ev['ts'] > 0

    _git(repo, 'checkout', '-b', 'feature')
    s.tick()
    assert events[-1]['title'] == 'demo: switched to feature'

    s.tick()                       # no change -> no new event
    assert len(events) == 2


def test_sensor_survives_bad_repo(tmp_path, capsys):
    events = []
    s = gitsensor.GitSensor(events.append,
                            [str(tmp_path / 'gone'), str(tmp_path)])
    s.tick()
    s.tick()
    assert events == []


def test_sensor_survives_repo_disappearing(repo):
    events = []
    s = gitsensor.GitSensor(events.append, [str(repo)])
    s.tick()
    os.rename(repo, str(repo) + '-moved')   # unmounted drive / renamed folder
    s.tick()                                # must not raise
    assert events == []


def test_import_repo_backfills_real_timestamps(repo):
    events = []
    n = gitimport.import_repo(str(repo), events.append)
    assert n == 2 == len(events)
    now = time.time()
    for ev in events:
        assert ev['source'] == 'git' and ev['app'] == 'git'
        assert ev['title'].startswith('demo: ')
        assert len(ev['detail'].split()[0]) == 40          # sha exposed for dedupe
        assert 0 < ev['ts'] <= now + 5                     # real author timestamp
    assert {e['title'] for e in events} == {'demo: first commit',
                                            'demo: second commit'}
    # calling twice re-emits (documented); caller dedupes on the sha in detail
    assert gitimport.import_repo(str(repo), events.append) == 2


def test_import_respects_max_commits(repo):
    events = []
    assert gitimport.import_repo(str(repo), events.append, max_commits=1) == 1


def test_import_degrades_gracefully(tmp_path, monkeypatch):
    events = []
    assert gitimport.import_repo(str(tmp_path), events.append) == 0   # not a repo
    assert gitimport.import_repo(str(tmp_path / 'nope'), events.append) == 0

    empty = tmp_path / 'empty'
    empty.mkdir()
    _git(empty, 'init', '-b', 'main')
    assert gitimport.import_repo(str(empty), events.append) == 0      # no commits
    assert gitsensor.read_state(str(empty)) == ('main', '')

    def boom(*a, **k):
        raise FileNotFoundError('git')
    monkeypatch.setattr(subprocess, 'run', boom)                      # git missing
    assert gitimport.import_repo(str(tmp_path), events.append) == 0
    assert gitsensor.commit_subject(str(tmp_path), 'deadbeef') == ''
    assert events == []


def test_import_all_uses_config(repo):
    events = []
    assert gitimport.import_all({'git_repos': [str(repo)]}, events.append) == 2
    assert gitimport.import_all({}, events.append) == 0               # no key -> nothing
    assert len(events) == 2


def test_packed_refs_fallback(repo):
    _git(repo, 'pack-refs', '--all')
    branch, sha = gitsensor.read_state(str(repo))
    assert branch == 'main' and len(sha) == 40
    assert not os.path.exists(os.path.join(repo, '.git', 'refs', 'heads', 'main'))


def test_slashed_branch_name_not_truncated(repo):
    """feature/x must not report as 'x' — slashes are normal in branch names."""
    _git(repo, "checkout", "-qb", "feature/billing-retry")
    branch, _sha = gitsensor.read_state(str(repo))
    assert branch == "feature/billing-retry"
