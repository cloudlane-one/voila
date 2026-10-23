#############################################################################
# Copyright (c) 2026, Voilà Contributors                                     #
#                                                                           #
# Distributed under the terms of the BSD 3-Clause License.                  #
#                                                                           #
# The full license is in the file LICENSE, distributed with this software.  #
#############################################################################
"""Read an act on the git state of the directory served by the tree page.

Only the handful of things the tree page needs are implemented: reading the
state of a repository, switching branch and pulling. Pushing is deliberately
absent.

Every command is run with an argument list (never through a shell), with an
explicit timeout, with hooks disabled and with terminal prompts disabled, so a
request can neither inject options nor hang forever waiting for a password.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "DEFAULT_GIT_EXECUTABLE",
    "GitCommit",
    "GitError",
    "GitStatus",
    "checkout",
    "get_status",
    "pull",
    "resolve_served_directory",
]

#: Name of the git executable looked up on ``PATH``.
DEFAULT_GIT_EXECUTABLE = "git"

#: Timeout, in seconds, of a read-only git command.
STATUS_TIMEOUT = 30

#: Timeout, in seconds, of the ``git fetch`` that refreshes the remote-tracking
#: branches, so that "commits to pull" is not a stale count.
FETCH_TIMEOUT = 15

#: Timeout, in seconds, of ``git pull``, which talks to the remote.
PULL_TIMEOUT = 120

#: How many commits available for pull are reported.
MAX_INCOMING_COMMITS = 20

# NUL cannot appear in a commit subject or in a ref name, so it is a safe
# field separator for git's --format output.
_SEPARATOR = "\x00"

_COMMIT_FORMAT = "%H%x00%h%x00%s"


class GitError(Exception):
    """Raised when git fails, or when a request cannot be served."""


@dataclass(frozen=True)
class GitCommit:
    """The little bit of a commit the tree page shows."""

    hash: str
    short_hash: str
    subject: str

    @classmethod
    def from_log_line(cls, line: str) -> "GitCommit":
        """Build a commit from a line written with ``_COMMIT_FORMAT``."""
        full_hash, short_hash, subject = line.split(_SEPARATOR, 2)
        return cls(hash=full_hash, short_hash=short_hash, subject=subject.strip())


@dataclass(frozen=True)
class GitStatus:
    """The git state of the directory being browsed."""

    is_repository: bool
    repository_root: Optional[str] = None
    branch: Optional[str] = None
    detached: bool = False
    head: Optional[GitCommit] = None
    tag: Optional[str] = None
    upstream: Optional[str] = None
    ahead: int = 0
    behind: int = 0
    incoming: Tuple[GitCommit, ...] = ()
    dirty: bool = False
    changed_files: int = 0
    local_branches: Tuple[str, ...] = ()
    remote_branches: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable representation."""
        return asdict(self)


def resolve_served_directory(root_dir: str, contents_path: str) -> str:
    """Resolve a contents path against the served directory.

    The tree page addresses folders the way the contents API does, with a
    leading slash and no relation to the filesystem layout. Anything that
    escapes ``root_dir`` is rejected rather than resolved.
    """
    root = os.path.realpath(root_dir)
    relative = (contents_path or "").lstrip("/\\")
    candidate = os.path.realpath(os.path.join(root, relative))
    if not _is_within(root, candidate):
        raise GitError("The requested path is outside the served directory.")
    if not os.path.isdir(candidate):
        raise GitError(f"{contents_path!r} is not a directory.")
    return candidate


def get_status(
    directory: str,
    *,
    executable: str = DEFAULT_GIT_EXECUTABLE,
    update_remote: bool = True,
) -> GitStatus:
    """Return the git state of ``directory``.

    A directory that merely lives inside a repository (a folder of notebooks,
    say) is reported with the state of the repository it belongs to.

    With ``update_remote`` the remote of the current branch is fetched first,
    because the number of commits available for pull is otherwise only as fresh
    as the last fetch; a remote that cannot be reached is not an error and the
    counts then describe the last known state of the remote-tracking branches.
    """
    toplevel = _repository_root(directory, executable=executable)
    if toplevel is None:
        return GitStatus(is_repository=False)

    branch, detached = _current_branch(toplevel, executable=executable)
    upstream = _try_run(
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
        toplevel,
        executable=executable,
    )
    upstream = upstream.strip() if upstream and upstream.strip() else None

    if upstream is not None and update_remote:
        _update_remote(toplevel, upstream, executable=executable)

    ahead = behind = 0
    incoming: Tuple[GitCommit, ...] = ()
    if upstream is not None:
        ahead, behind = _ahead_behind(toplevel, upstream, executable=executable)
        if behind:
            incoming = _incoming_commits(toplevel, upstream, executable=executable)

    local_branches, remote_branches = _branches(toplevel, executable=executable)
    changes = _worktree_changes(toplevel, executable=executable)

    return GitStatus(
        is_repository=True,
        repository_root=toplevel,
        branch=branch,
        detached=detached,
        head=_head_commit(toplevel, executable=executable),
        tag=_tag_at_head(toplevel, executable=executable),
        upstream=upstream,
        ahead=ahead,
        behind=behind,
        incoming=incoming,
        dirty=bool(changes),
        changed_files=len(changes),
        local_branches=local_branches,
        remote_branches=remote_branches,
    )


def checkout(
    directory: str,
    branch: str,
    *,
    executable: str = DEFAULT_GIT_EXECUTABLE,
) -> GitStatus:
    """Switch the repository containing ``directory`` to ``branch``.

    ``branch`` is either the name of a local branch or the name of a remote
    branch as listed in the status (``origin/feature``). A remote branch that
    has no local counterpart is created locally, tracking the remote one.
    """
    toplevel = _require_repository(directory, executable=executable)
    status = get_status(directory, executable=executable)
    target = branch.strip()

    if target in status.local_branches:
        _run(["checkout", target, "--"], toplevel, executable=executable)
    elif target in status.remote_branches:
        local_name = _remote_branch_name(target, toplevel, executable=executable)
        if local_name in status.local_branches:
            _run(["checkout", local_name, "--"], toplevel, executable=executable)
        else:
            _run(["checkout", "--track", target], toplevel, executable=executable)
    else:
        raise GitError(f"There is no branch named {target!r}.")

    return get_status(directory, executable=executable, update_remote=False)


def pull(directory: str, *, executable: str = DEFAULT_GIT_EXECUTABLE) -> GitStatus:
    """Pull the current branch of the repository containing ``directory``.

    The pull is a merge, never a rebase, so that a conflict is reported instead
    of leaving a half-finished rebase behind. A clean worktree is required:
    that is both what makes the rollback below safe (nothing but commits and
    refs can move) and the only way to guarantee the tree looks untouched when
    the pull fails. On failure the repository is put back where it was and the
    error is re-raised.
    """
    toplevel = _require_repository(directory, executable=executable)
    status = get_status(directory, executable=executable)
    if status.upstream is None:
        raise GitError("The current branch has no upstream branch to pull from.")
    if status.dirty:
        raise GitError(
            "The working tree has uncommitted changes. Commit or discard them "
            "before pulling."
        )

    before = _run(["rev-parse", "HEAD"], toplevel, executable=executable).strip()
    try:
        _run(
            ["pull", "--no-rebase"],
            toplevel,
            executable=executable,
            timeout=PULL_TIMEOUT,
        )
    except GitError as error:
        _revert(toplevel, before, executable=executable)
        raise GitError(f"The pull failed and was reverted. {error}") from error

    # The pull just fetched, there is nothing to refresh.
    return get_status(directory, executable=executable, update_remote=False)


def _revert(toplevel: str, revision: str, *, executable: str) -> None:
    """Put the repository back at ``revision`` after a failed pull."""
    if _try_run(
        ["rev-parse", "-q", "--verify", "MERGE_HEAD"], toplevel, executable=executable
    ):
        _try_run(["merge", "--abort"], toplevel, executable=executable)
    head = _try_run(["rev-parse", "HEAD"], toplevel, executable=executable)
    if head is not None and head.strip() != revision:
        _try_run(["reset", "--hard", revision], toplevel, executable=executable)


def _check_executable(executable: str) -> None:
    """Fail early, and loudly, when git is not installed."""
    if shutil.which(executable) is None:
        raise GitError(f"The git executable {executable!r} was not found.")


def _repository_root(directory: str, *, executable: str) -> Optional[str]:
    _check_executable(executable)
    output = _try_run(
        ["rev-parse", "--show-toplevel"], directory, executable=executable
    )
    if output is None or not output.strip():
        return None
    # git always prints forward slashes, even on Windows.
    return os.path.normpath(output.strip())


def _require_repository(directory: str, *, executable: str) -> str:
    toplevel = _repository_root(directory, executable=executable)
    if toplevel is None:
        raise GitError(f"{directory!r} is not inside a git repository.")
    return toplevel


def _current_branch(toplevel: str, *, executable: str) -> Tuple[Optional[str], bool]:
    """Return the branch name and whether HEAD is detached."""
    name = _try_run(
        ["rev-parse", "--abbrev-ref", "HEAD"], toplevel, executable=executable
    )
    if name is not None:
        name = name.strip()
        if name == "HEAD":
            return None, True
        return name, False
    # No commit yet: HEAD points at a branch that does not exist.
    unborn = _try_run(
        ["symbolic-ref", "--short", "HEAD"], toplevel, executable=executable
    )
    if unborn is not None and unborn.strip():
        return unborn.strip(), False
    return None, True


def _head_commit(toplevel: str, *, executable: str) -> Optional[GitCommit]:
    line = _try_run(
        ["log", "-1", "--format=" + _COMMIT_FORMAT], toplevel, executable=executable
    )
    if line is None or not line.strip():
        return None
    return GitCommit.from_log_line(line.strip())


def _tag_at_head(toplevel: str, *, executable: str) -> Optional[str]:
    output = _try_run(
        ["describe", "--exact-match", "--tags", "HEAD"],
        toplevel,
        executable=executable,
    )
    if output is None or not output.strip():
        return None
    return output.strip()


def _update_remote(toplevel: str, upstream: str, *, executable: str) -> None:
    """Fetch the remote branch ``upstream`` belongs to, best effort."""
    remote, _ = _split_remote_branch(upstream, toplevel, executable=executable)
    try:
        _run(
            ["fetch", "--quiet", remote],
            toplevel,
            executable=executable,
            timeout=FETCH_TIMEOUT,
        )
    except GitError:
        # Offline, no credentials, or nothing new: the status is still usable,
        # it just describes the remote as of the last successful fetch.
        pass


def _ahead_behind(toplevel: str, upstream: str, *, executable: str) -> Tuple[int, int]:
    output = _run(
        ["rev-list", "--left-right", "--count", f"HEAD...{upstream}"],
        toplevel,
        executable=executable,
    )
    ahead, behind = (int(part) for part in output.split())
    return ahead, behind


def _incoming_commits(
    toplevel: str, upstream: str, *, executable: str
) -> Tuple[GitCommit, ...]:
    output = _run(
        [
            "log",
            f"--max-count={MAX_INCOMING_COMMITS}",
            "--format=" + _COMMIT_FORMAT,
            f"HEAD..{upstream}",
        ],
        toplevel,
        executable=executable,
    )
    return tuple(
        GitCommit.from_log_line(line) for line in output.splitlines() if line.strip()
    )


def _worktree_changes(toplevel: str, *, executable: str) -> Tuple[str, ...]:
    output = _run(
        ["status", "--porcelain=v1", "--untracked-files=normal"],
        toplevel,
        executable=executable,
    )
    return tuple(line for line in output.splitlines() if line.strip())


def _branches(
    toplevel: str, *, executable: str
) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """Return the local and the remote branch names."""
    local = _run(
        ["for-each-ref", "--format=%(refname:short)", "refs/heads"],
        toplevel,
        executable=executable,
    )
    remote = _run(
        [
            "for-each-ref",
            # %00 rather than a literal NUL: a Windows argument cannot hold one.
            "--format=%(refname:short)%00%(symref)",
            "refs/remotes",
        ],
        toplevel,
        executable=executable,
    )
    local_names = tuple(sorted(line for line in local.splitlines() if line.strip()))
    # Symbolic refs such as origin/HEAD are not branches that can be checked out.
    remote_names = tuple(
        sorted(
            name
            for name, symref in (
                line.split(_SEPARATOR, 1)
                for line in remote.splitlines()
                if line.strip()
            )
            if not symref
        )
    )
    return local_names, remote_names


def _split_remote_branch(
    remote_branch: str, toplevel: str, *, executable: str
) -> Tuple[str, str]:
    """Split ``origin/feature/x`` into its remote and its branch name.

    The remote part can itself contain a slash, so the configured remotes are
    matched instead of splitting on the first slash.
    """
    remotes = _run(["remote"], toplevel, executable=executable).split()
    for remote in sorted(remotes, key=len, reverse=True):
        if remote_branch.startswith(remote + "/"):
            return remote, remote_branch[len(remote) + 1 :]
    raise GitError(f"{remote_branch!r} does not belong to a known remote.")


def _remote_branch_name(remote_branch: str, toplevel: str, *, executable: str) -> str:
    """Return the local name a remote branch would be checked out under."""
    _, name = _split_remote_branch(remote_branch, toplevel, executable=executable)
    return name


def _run(
    args: List[str],
    cwd: str,
    *,
    executable: str,
    timeout: float = STATUS_TIMEOUT,
) -> str:
    """Run a git command and return its standard output."""
    command = [
        executable,
        "-C",
        cwd,
        "--no-pager",
        "-c",
        "core.quotepath=false",
        # An empty hooks path disables the repository's hooks: a pull from the
        # browser must not run arbitrary scripts the user forgot about.
        "-c",
        "core.hooksPath=",
        *args,
    ]
    environment = {
        **os.environ,
        # Never wait for a credential on a terminal nobody is looking at.
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
    }
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=environment,
            check=False,
        )
    except FileNotFoundError as error:
        raise GitError(f"The git executable {executable!r} was not found.") from error
    except subprocess.TimeoutExpired as error:
        raise GitError(f"git {args[0]} timed out after {timeout:g} seconds.") from error
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout).strip()
        raise GitError(
            message or f"git {args[0]} failed with exit code {completed.returncode}."
        )
    return completed.stdout


def _try_run(
    args: List[str], cwd: str, *, executable: str, timeout: float = STATUS_TIMEOUT
) -> Optional[str]:
    """Run a git command, returning ``None`` when it fails."""
    try:
        return _run(args, cwd, executable=executable, timeout=timeout)
    except GitError:
        return None


def _is_within(root: str, candidate: str) -> bool:
    root_key = os.path.normcase(root)
    candidate_key = os.path.normcase(candidate)
    if candidate_key == root_key:
        return True
    # Compare against the separator-terminated root so that "/srv/voila-two"
    # is not accepted as being inside "/srv/voila".
    return candidate_key.startswith(root_key.rstrip(os.sep) + os.sep)
