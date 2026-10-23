"""Tests of the git plumbing behind the tree page's status bar."""

import subprocess

import pytest

from voila import git as voila_git

_IDENTITY = [
    "-c",
    "user.name=Voila Test",
    "-c",
    "user.email=voila@example.com",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "init.defaultBranch=main",
]


def git(cwd, *args):
    """Run git in ``cwd`` and return its standard output."""
    completed = subprocess.run(
        ["git", *_IDENTITY, *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout


def commit(repo, name, content, message):
    """Write a file and commit it."""
    (repo / name).write_text(content)
    git(repo, "add", name)
    git(repo, "commit", "-m", message)


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q")
    commit(path, "a.txt", "one\n", "one")
    return path


@pytest.fixture
def remote_and_clones(tmp_path):
    """A bare remote and two clones of it, each with one commit pushed."""
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    first = tmp_path / "first"
    git(tmp_path, "clone", "-q", str(remote), str(first))
    commit(first, "a.txt", "one\n", "one")
    git(first, "push", "-q", "-u", "origin", "main")
    second = tmp_path / "second"
    git(tmp_path, "clone", "-q", str(remote), str(second))
    return remote, first, second


def test_plain_directory_is_not_a_repository(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()

    status = voila_git.get_status(str(plain))

    assert status.is_repository is False
    assert status.to_dict()["is_repository"] is False


def test_clean_repository(repo):
    status = voila_git.get_status(str(repo))

    assert status.is_repository is True
    assert status.repository_root == str(repo)
    assert status.branch == "main"
    assert status.detached is False
    assert status.head is not None
    assert status.head.subject == "one"
    assert status.head.short_hash
    assert status.head.hash.startswith(status.head.short_hash)
    assert status.tag is None
    assert status.upstream is None
    assert status.dirty is False
    assert status.changed_files == 0
    assert status.local_branches == ("main",)
    assert status.remote_branches == ()
    assert status.incoming == ()


def test_dirty_worktree(repo):
    (repo / "b.txt").write_text("two\n")

    status = voila_git.get_status(str(repo))

    assert status.dirty is True
    assert status.changed_files == 1


def test_tag_of_the_head_commit(repo):
    git(repo, "tag", "v1.0")

    assert voila_git.get_status(str(repo)).tag == "v1.0"


def test_subdirectory_reports_the_repository_it_belongs_to(repo):
    subdirectory = repo / "notebooks"
    subdirectory.mkdir()

    status = voila_git.get_status(str(subdirectory))

    assert status.is_repository is True
    assert status.repository_root == str(repo)
    assert status.branch == "main"


def test_repository_without_commits(tmp_path):
    path = tmp_path / "empty"
    path.mkdir()
    git(path, "init", "-q")

    status = voila_git.get_status(str(path))

    assert status.is_repository is True
    assert status.branch == "main"
    assert status.detached is False
    assert status.head is None


def test_detached_head(repo):
    git(repo, "checkout", "-q", "--detach")

    status = voila_git.get_status(str(repo))

    assert status.detached is True
    assert status.branch is None
    assert status.head is not None


def test_commits_available_for_pull(remote_and_clones):
    _, first, second = remote_and_clones
    commit(first, "b.txt", "two\n", "two")
    git(first, "push", "-q")

    status = voila_git.get_status(str(second))

    assert status.upstream == "origin/main"
    assert status.behind == 1
    assert status.ahead == 0
    assert [incoming.subject for incoming in status.incoming] == ["two"]


def test_checkout_local_branch(repo):
    git(repo, "branch", "other")

    status = voila_git.checkout(str(repo), "other")

    assert status.branch == "other"


def test_checkout_remote_branch_creates_a_tracking_branch(remote_and_clones):
    _, first, second = remote_and_clones
    git(first, "checkout", "-q", "-b", "feature")
    commit(first, "b.txt", "two\n", "two")
    git(first, "push", "-q", "-u", "origin", "feature")
    git(second, "fetch", "-q")
    assert "origin/feature" in voila_git.get_status(str(second)).remote_branches

    status = voila_git.checkout(str(second), "origin/feature")

    assert status.branch == "feature"
    assert status.head is not None
    assert status.head.subject == "two"


def test_checkout_unknown_branch(repo):
    with pytest.raises(voila_git.GitError, match="no branch named"):
        voila_git.checkout(str(repo), "nope")


def test_checkout_outside_a_repository(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()

    with pytest.raises(voila_git.GitError, match="not inside a git repository"):
        voila_git.checkout(str(plain), "main")


def test_pull_fast_forwards(remote_and_clones):
    _, first, second = remote_and_clones
    commit(first, "b.txt", "two\n", "two")
    git(first, "push", "-q")

    status = voila_git.pull(str(second))

    assert status.behind == 0
    assert status.head is not None
    assert status.head.subject == "two"
    assert (second / "b.txt").read_text().strip() == "two"


def test_pull_with_nothing_to_pull(remote_and_clones):
    _, _, second = remote_and_clones

    status = voila_git.pull(str(second))

    assert status.behind == 0
    assert status.head is not None
    assert status.head.subject == "one"


def test_pull_refuses_a_dirty_worktree(remote_and_clones):
    _, first, second = remote_and_clones
    commit(first, "b.txt", "two\n", "two")
    git(first, "push", "-q")
    (second / "b.txt").write_text("local\n")
    before = voila_git.get_status(str(second))

    with pytest.raises(voila_git.GitError, match="uncommitted changes"):
        voila_git.pull(str(second))

    after = voila_git.get_status(str(second))
    assert after.head == before.head
    assert after.behind == 1
    assert (second / "b.txt").read_text() == "local\n"


def test_pull_conflict_is_reverted(remote_and_clones):
    _, first, second = remote_and_clones
    commit(first, "a.txt", "theirs\n", "theirs")
    git(first, "push", "-q")
    commit(second, "a.txt", "ours\n", "ours")
    before = voila_git.get_status(str(second))

    with pytest.raises(voila_git.GitError, match="failed and was reverted"):
        voila_git.pull(str(second))

    after = voila_git.get_status(str(second))
    assert after.head == before.head
    assert after.dirty is False
    assert after.behind == 1
    assert (second / "a.txt").read_text() == "ours\n"
    # No merge is left in progress for the user to discover later.
    assert not (second / ".git" / "MERGE_HEAD").exists()


def test_pull_without_upstream(repo):
    with pytest.raises(voila_git.GitError, match="no upstream"):
        voila_git.pull(str(repo))


def test_resolve_served_directory(tmp_path):
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)

    assert voila_git.resolve_served_directory(str(root), "") == str(root)
    assert voila_git.resolve_served_directory(str(root), "/") == str(root)
    assert voila_git.resolve_served_directory(str(root), "sub") == str(root / "sub")
    assert voila_git.resolve_served_directory(str(root), "/sub") == str(root / "sub")


def test_resolve_served_directory_rejects_escapes(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "outside").mkdir()

    with pytest.raises(voila_git.GitError, match="outside the served directory"):
        voila_git.resolve_served_directory(str(root), "../outside")
    with pytest.raises(voila_git.GitError, match="outside the served directory"):
        voila_git.resolve_served_directory(str(root), "sub/../../outside")
    with pytest.raises(voila_git.GitError, match="not a directory"):
        voila_git.resolve_served_directory(str(root), "missing")


def test_missing_git_executable(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()

    with pytest.raises(voila_git.GitError, match="was not found"):
        voila_git.get_status(str(plain), executable="git-does-not-exist")
