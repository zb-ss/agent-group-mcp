"""Repo identity by normalised remote URL, and local clone resolution."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_bus import config, repo_identity
from agent_bus.repo_identity import (
    AmbiguousRepoError,
    RepoNotFoundError,
    clone_target,
    normalize_remote,
    resolve,
)

ID_A = "github.com/example-org/repo-a"
ID_B = "github.com/example-org/repo-b"


@pytest.fixture
def cfg(tmp_path):
    c = config.defaults()["repos"]
    c["clone_root"] = str(tmp_path / "clones")
    return c


class FakeGit:
    """Maps a repo path to its remote URL; counts calls."""

    def __init__(self, remotes=None):
        self.remotes = {str(k): v for k, v in (remotes or {}).items()}
        self.calls = 0

    def __call__(self, argv, *, timeout, env=None, cwd=None):
        self.calls += 1
        url = self.remotes.get(argv[2])
        if url is None:
            return subprocess.CompletedProcess(argv, 2, "", "no remote")
        return subprocess.CompletedProcess(argv, 0, url + "\n", "")


def make_repo(base: Path, name: str, *, worktree: bool = False) -> Path:
    path = base / name
    path.mkdir(parents=True)
    if worktree:
        (path / ".git").write_text("gitdir: elsewhere\n")
    else:
        (path / ".git").mkdir()
    return path


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:Example-Org/Repo-A.git",
        "https://github.com/example-org/repo-a",
        "https://user@github.com/example-org/repo-a.git/",
        # userinfo with a password part (left empty: no credential in fixtures)
        "https://user:@github.com/example-org/repo-a.git",
        "ssh://git@github.com:22/example-org/repo-a.git",
        "ssh://git@ssh.github.com:443/example-org/repo-a.git",
        "git://github.com/example-org/repo-a.git",
        "github.com/example-org/repo-a",
    ],
)
def test_normalize_equivalent_forms(url):
    assert normalize_remote(url, config.defaults()["repos"]["host_aliases"]) == ID_A


@pytest.mark.parametrize(
    "url", ["", "  ", "/srv/git/repo-a.git", "../repo-a", "~/repo-a", "file:///srv/git/repo-a.git", "repo-a"]
)
def test_normalize_rejects_non_remotes(url):
    assert normalize_remote(url) is None


def test_normalize_custom_alias():
    assert normalize_remote("git@git.internal:team/x.git", {"git.internal": "example.com"}) == "example.com/team/x"


def test_repo_id_none_on_failure(tmp_path, cfg):
    assert repo_identity.repo_id(tmp_path, cfg=cfg, run=FakeGit()) is None

    def boom(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 1)

    assert repo_identity.repo_id(tmp_path, cfg=cfg, run=boom) is None

    def missing(argv, **kw):
        raise FileNotFoundError("git")

    assert repo_identity.repo_id(tmp_path, cfg=cfg, run=missing) is None


def roster(storage, tmp_path):
    a = make_repo(tmp_path / "code", "repo-a")
    b = make_repo(tmp_path / "code", "repo-b")
    storage.upsert_agent("repo-a/claude", str(a))
    storage.upsert_agent("repo-b/codex", str(b))
    git = FakeGit({a: "git@github.com:example-org/repo-a.git", b: "https://github.com/example-org/repo-b"})
    return a, b, git


@pytest.mark.parametrize(
    "name",
    [
        "repo-a",
        ID_A,
        "git@github.com:Example-Org/repo-a.git",
        "https://github.com/example-org/repo-a.git",
        "example-org/repo-a",
        "Repo-A.git",
    ],
)
def test_resolve_from_roster(storage, cfg, tmp_path, name):
    a, _b, git = roster(storage, tmp_path)
    found = resolve(name, storage=storage, cfg=cfg, run=git)
    assert found.path == a
    assert found.repo_id == ID_A
    assert found.alias == "repo-a"
    assert storage.repo_ids()[str(a)] == ID_A


def test_resolve_scans_roots_when_roster_empty(storage, cfg, tmp_path):
    a = make_repo(tmp_path / "scan", "repo-a")
    cfg["roots"] = [str(tmp_path / "scan")]
    git = FakeGit({a: "git@github.com:example-org/repo-a.git"})
    found = resolve("example-org/repo-a", storage=storage, cfg=cfg, run=git)
    assert found.path == a.resolve()
    assert found.alias == "repo-a"


def test_scanning_roots_never_finds_a_clones_spawn_worktrees(storage, cfg, tmp_path):
    a = make_repo(tmp_path / "scan", "repo-a")
    (a / ".worktrees" / "fix-login").mkdir(parents=True)
    (a / ".worktrees" / "fix-login" / ".git").write_text("gitdir: elsewhere\n")
    cfg["roots"] = [str(tmp_path / "scan"), str(a / ".worktrees")]
    found = repo_identity._candidates(storage, cfg, FakeGit({a: "git@github.com:example-org/repo-a.git"}), scan=True)
    assert [c.path for c in found] == [a.resolve()]


def test_roster_wins_over_roots(storage, cfg, tmp_path):
    a, _b, git = roster(storage, tmp_path)
    other = make_repo(tmp_path / "scan", "repo-a-copy")
    git.remotes[str(other)] = "git@github.com:example-org/repo-a.git"
    cfg["roots"] = [str(tmp_path / "scan")]
    assert resolve(ID_A, storage=storage, cfg=cfg, run=git).path == a


def test_worktree_not_chosen_over_main_clone(storage, cfg, tmp_path):
    a, _b, git = roster(storage, tmp_path)
    wt = make_repo(tmp_path / "wt", "repo-a-wt", worktree=True)
    storage.upsert_agent("repo-a-wt/codex", str(wt))
    git.remotes[str(wt)] = "git@github.com:example-org/repo-a.git"
    assert resolve(ID_A, storage=storage, cfg=cfg, run=git).path == a


def test_ambiguous_suffix_lists_both(storage, cfg, tmp_path):
    a = make_repo(tmp_path / "code", "one")
    b = make_repo(tmp_path / "code", "two")
    storage.upsert_agent("one/claude", str(a))
    storage.upsert_agent("two/claude", str(b))
    git = FakeGit({a: "git@github.com:org-x/shared.git", b: "git@github.com:org-y/shared.git"})
    with pytest.raises(AmbiguousRepoError) as err:
        resolve("shared", storage=storage, cfg=cfg, run=git)
    assert {c.path for c in err.value.candidates} == {a, b}


def test_unknown_id_not_found_with_clone_target(storage, cfg, tmp_path):
    with pytest.raises(RepoNotFoundError) as err:
        resolve(ID_A, storage=storage, cfg=cfg, run=FakeGit())
    assert err.value.clone_url == "git@github.com:example-org/repo-a.git"
    assert err.value.clone_dest == tmp_path / "clones" / "repo-a"


def test_unknown_alias_has_no_url(storage, cfg):
    with pytest.raises(RepoNotFoundError) as err:
        resolve("repo-z", storage=storage, cfg=cfg, run=FakeGit())
    assert err.value.clone_url is None and err.value.clone_dest is None


def test_clone_target_url_input_and_no_root(cfg):
    cfg["clone_root"] = None
    cfg["roots"] = []
    assert repo_identity.clone_target("https://github.com/example-org/repo-b.git", cfg) == (
        "https://github.com/example-org/repo-b.git",
        None,
    )
    cfg["roots"] = ["/srv/code"]
    assert repo_identity.clone_target(ID_B, cfg) == (
        "git@github.com:example-org/repo-b.git",
        Path("/srv/code/repo-b"),
    )


def test_refresh_missing_fills_only_missing(storage, cfg, tmp_path):
    a, b, git = roster(storage, tmp_path)
    storage.set_repo_id(str(a), "cached/value")
    repo_identity.refresh_missing(storage, cfg, run=git)
    ids = storage.repo_ids()
    assert ids[str(a)] == "cached/value"
    assert ids[str(b)] == ID_B
    assert git.calls == 1


@pytest.mark.parametrize("url", [
    "--upload-pack=touch x;://a/h:r", "-c.example.com/o/r", "ext::sh -c id/a/b",
    "https://github.com/o/r r", "git@github.com:o/--r",
])
def test_normalize_rejects_option_like_and_odd_names(url):
    assert normalize_remote(url) is None or not normalize_remote(url).startswith("-")
    assert clone_target(url, {"clone_url": "git@{host}:{path}.git", "clone_root": "/c"}) is None


def test_remote_added_later_is_found_on_the_roster(storage, cfg, tmp_path):
    a, _b, _git = roster(storage, tmp_path)
    repo_identity.refresh_missing(storage, cfg, run=FakeGit())
    assert storage.repo_ids().get(str(a)) is None
    git = FakeGit({a: "git@github.com:example-org/repo-a.git"})
    assert resolve(ID_A, storage=storage, cfg=cfg, run=git).path == a
    assert storage.repo_ids()[str(a)] == ID_A


# ----------------------- url normalisation edge cases ------------------------


@pytest.mark.parametrize("url, expected", [
    ("ssh://git@bitbucket.example.com:7999/~jsmith/repo-a.git", "bitbucket.example.com/~jsmith/repo-a"),
    ("git@git.example.org:~jsmith/repo-a", "git.example.org/~jsmith/repo-a"),
    ("https://dev.example.com/example-org/My%20Project/_git/repo-a",
     "dev.example.com/example-org/my%20project/_git/repo-a"),
])
def test_normalize_hosted_path_forms(url, expected):
    assert normalize_remote(url) == expected


@pytest.mark.parametrize("url", [
    "https://host.example/x/-r", "https://host.example/x/a%2", "https://host.example/x/a%zz",
    "https://host.example/x/../r",
])
def test_normalize_still_rejects_hostile_segments(url):
    assert normalize_remote(url) is None


@pytest.mark.parametrize("url", [
    "https://host.example/x/.git.git", "https://host.example/x/.cargo",
    "https://host.example/x/%2egit",
])
def test_clone_target_refuses_dot_and_odd_folder_names(cfg, url):
    assert clone_target(url, cfg) is None


def test_clone_target_accepts_an_ordinary_folder_name(cfg, tmp_path):
    url, dest = clone_target("https://host.example/x/node_modules", cfg)
    assert dest == tmp_path / "clones" / "node_modules"
    # repeated suffixes collapse to the plain name
    assert clone_target("https://host.example/x/repo-a.git.git", cfg)[1] == tmp_path / "clones" / "repo-a"


def test_clone_target_bad_template_is_a_config_error(cfg):
    cfg["clone_url"] = "https://{host}/{owner}/{repo}.git"
    with pytest.raises(config.ConfigError, match="clone_url"):
        clone_target("github.com/example-org/repo-a", cfg)


def test_resolve_without_persist_writes_nothing(storage, cfg, tmp_path):
    a, _b, git = roster(storage, tmp_path)
    assert resolve(ID_A, storage=storage, cfg=cfg, run=git, persist=False).path == a
    assert storage.repo_ids() == {}
    assert resolve(ID_A, storage=storage, cfg=cfg, run=git).path == a
    assert storage.repo_ids()[str(a)] == ID_A


def _failing(argv, **kw):
    return subprocess.CompletedProcess(argv, 128, "", "fatal: not a git repository")


def test_lookup_separates_no_remote_from_errors(tmp_path, cfg):
    assert repo_identity.lookup_repo_id(tmp_path, cfg=cfg, run=FakeGit()).definite is True
    assert repo_identity.lookup_repo_id(tmp_path, cfg=cfg, run=_failing).definite is False

    def timeout(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 1)

    assert repo_identity.lookup_repo_id(tmp_path, cfg=cfg, run=timeout).definite is False


def test_failed_lookup_is_not_cached_and_keeps_a_good_id(storage, cfg, tmp_path):
    a, _b, git = roster(storage, tmp_path)
    repo_identity.refresh_missing(storage, cfg, run=_failing)
    assert storage.repo_ids() == {}
    storage.set_repo_id(str(a), ID_A)
    resolve(ID_A, storage=storage, cfg=cfg, run=_failing)
    assert storage.repo_ids()[str(a)] == ID_A


def test_a_definite_no_remote_clears_a_stale_id(storage, cfg, tmp_path):
    a, _b, _git = roster(storage, tmp_path)
    storage.set_repo_id(str(a), ID_A)
    resolve("repo-a", storage=storage, cfg=cfg, run=FakeGit())
    assert storage.repo_ids()[str(a)] is None


def test_old_cached_id_is_asked_again_after_the_recheck_period(storage, cfg, tmp_path):
    a, _b, _git = roster(storage, tmp_path)
    storage.set_repo_id(str(a), ID_A)
    moved = FakeGit({a: "git@github.com:example-org/renamed.git"})
    repo_identity.refresh_missing(storage, cfg, run=moved)
    assert storage.repo_ids()[str(a)] == ID_A and moved.calls == 1  # only repo-b asked
    cfg["id_recheck_seconds"] = 0
    repo_identity.refresh_missing(storage, cfg, run=moved)
    assert storage.repo_ids()[str(a)] == "github.com/example-org/renamed"


@pytest.mark.parametrize("url", [
    "https://evil.example#@github.com/example-org/repo-a",
    "https://evil.example?@github.com/example-org/repo-a",
    "https://evil.example\\@github.com/example-org/repo-a",
    "https://evil.example/x@github.com/example-org/repo-a?token=1",
    "https://evil.example/example-org/repo-a#frag",
    "ssh://-oProxyCommand=x@github.com/example-org/repo-a",
    "ssh://-user@github.com/example-org/repo-a",
    "https://us er@github.com/example-org/repo-a",
    "ssh://git@-host.example/example-org/repo-a",
    "https://github.com:abc/example-org/repo-a",
])
def test_ambiguous_authorities_are_not_remotes(url, cfg):
    assert normalize_remote(url) is None
    assert clone_target(url, cfg) is None


def test_scheme_clone_url_is_rebuilt_from_the_parsed_parts(cfg):
    url, _dest = clone_target("HTTPS://git-user@GitHub.com:443/Example-Org/Repo-A.git", cfg)
    assert url == "https://git-user@GitHub.com:443/Example-Org/Repo-A.git"
    same, _ = clone_target("ssh://git@github.com/example-org/repo-a.git", cfg)
    assert same == "ssh://git@github.com/example-org/repo-a.git"


def test_roster_group_names_for_one_path_all_resolve(storage, cfg, tmp_path):
    repo = make_repo(tmp_path / "code", "repo-a")
    storage.upsert_agent("alpha/claude-fix", str(repo))
    storage.upsert_agent("zeta/claude", str(repo))
    git = FakeGit()
    for name in ("alpha", "zeta"):
        found = resolve(name, storage=storage, cfg=cfg, run=git)
        assert found.path == repo and found.alias == name


def test_the_group_the_repo_wiring_gives_is_listed_first(storage, tmp_path):
    repo = make_repo(tmp_path / "code", "repo-a")
    storage.upsert_agent("old-name/claude-fix", str(repo))
    storage.upsert_agent("repo-a/claude", str(repo))
    assert repo_identity._roster_paths(storage) == [(repo, ["repo-a", "old-name"])]


def test_a_checked_no_remote_is_not_asked_again_while_fresh(storage, cfg, tmp_path):
    roster(storage, tmp_path)
    git = FakeGit()  # no remotes at all: definite "no remote" for both repos
    repo_identity.refresh_missing(storage, cfg, run=git)
    repo_identity.refresh_missing(storage, cfg, run=git)
    assert git.calls == 2
    assert storage.repo_ids() and set(storage.repo_ids().values()) == {None}


def test_a_stale_no_remote_is_asked_again(storage, cfg, tmp_path):
    roster(storage, tmp_path)
    git = FakeGit()
    cfg["id_recheck_seconds"] = 0
    repo_identity.refresh_missing(storage, cfg, run=git)
    repo_identity.refresh_missing(storage, cfg, run=git)
    assert git.calls == 4


def test_candidates_use_a_cached_null_instead_of_asking_git(storage, cfg, tmp_path):
    roster(storage, tmp_path)
    git = FakeGit()
    repo_identity._candidates(storage, cfg, git, scan=False)
    assert git.calls == 2
    repo_identity._candidates(storage, cfg, git, scan=False)
    assert git.calls == 2
    repo_identity._candidates(storage, cfg, git, scan=False, refresh=True)
    assert git.calls == 4


def test_failed_lookups_are_never_cached_but_the_budget_bounds_a_pass(storage, cfg, tmp_path):
    roster(storage, tmp_path)
    calls = []

    def slow_failure(argv, *, timeout, **kw):
        calls.append(timeout)
        raise subprocess.TimeoutExpired(argv, timeout)

    repo_identity.refresh_missing(storage, cfg, run=slow_failure, budget_seconds=0)
    assert calls == [] and storage.repo_ids() == {}
    repo_identity.refresh_missing(storage, cfg, run=slow_failure, budget_seconds=5)
    assert len(calls) == 2 and max(calls) <= 5
    assert storage.repo_ids() == {}
