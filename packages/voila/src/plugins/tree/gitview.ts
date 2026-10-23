/***************************************************************************
 * Copyright (c) 2026, Voilà contributors                                   *
 *                                                                          *
 * Distributed under the terms of the BSD 3-Clause License.                 *
 *                                                                          *
 * The full license is in the file LICENSE, distributed with this software. *
 ****************************************************************************/

/**
 * Pure rendering rules of the git status bar.
 *
 * Nothing here touches the DOM or the network: the bar widget feeds a status
 * in and writes the view out, which keeps the states that are easy to get
 * wrong (detached head, no upstream, a dirty worktree, nothing to pull) out of
 * the widget and testable on their own.
 */

export interface IGitCommit {
  hash: string;
  short_hash: string;
  subject: string;
}

/** The git state of the folder being browsed, as reported by the server. */
export interface IGitStatus {
  is_repository: boolean;
  repository_root: string | null;
  branch: string | null;
  detached: boolean;
  head: IGitCommit | null;
  tag: string | null;
  upstream: string | null;
  ahead: number;
  behind: number;
  incoming: IGitCommit[];
  dirty: boolean;
  changed_files: number;
  local_branches: string[];
  remote_branches: string[];
}

/** What the bar shows for a given status. */
export interface IGitBarView {
  visible: boolean;
  branch: string;
  tag: string | null;
  commit: string | null;
  worktree: string;
  worktreeDirty: boolean;
  incoming: string;
  canPull: boolean;
  pullHint: string;
  canSwitch: boolean;
  branches: string[];
  selected: string;
  repositoryRoot: string | null;
}

function plural(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? '' : 's'}`;
}

/**
 * Build the view of the bar from a status.
 */
export function renderStatus(status: IGitStatus): IGitBarView {
  const branches = [...status.local_branches, ...status.remote_branches];
  const selected = status.branch ?? '';

  let branch: string;
  if (status.detached) {
    branch = status.head ? `detached at ${status.head.short_hash}` : 'detached';
  } else if (status.branch === null) {
    branch = 'no branch yet';
  } else {
    branch = status.branch;
  }

  let incoming: string;
  let canPull: boolean;
  let pullHint: string;
  if (status.upstream === null) {
    canPull = false;
    incoming = 'no upstream';
    pullHint = 'This branch has no upstream branch to pull from.';
  } else if (status.behind === 0) {
    canPull = false;
    incoming = 'up to date';
    pullHint = 'There is nothing to pull.';
  } else {
    incoming = `${plural(status.behind, 'commit')} to pull`;
    if (status.dirty) {
      canPull = false;
      pullHint = 'Commit or discard the uncommitted changes before pulling.';
    } else {
      canPull = true;
      pullHint = `Pull ${plural(status.behind, 'commit')} from ${
        status.upstream
      }.`;
    }
  }

  return {
    visible: status.is_repository,
    branch,
    tag: status.tag,
    commit: status.head
      ? `${status.head.short_hash} ${status.head.subject}`
      : null,
    worktree: status.dirty
      ? `${plural(status.changed_files, 'uncommitted change')}`
      : 'clean',
    worktreeDirty: status.dirty,
    incoming,
    canPull,
    pullHint,
    canSwitch: !status.detached && branches.length > 0,
    branches,
    selected,
    repositoryRoot: status.repository_root
  };
}

/**
 * Build the message shown after a pull.
 */
export function pullSummary(before: IGitStatus): string {
  if (before.behind === 0) {
    return 'Already up to date.';
  }
  return `Pulled ${plural(before.behind, 'commit')}.`;
}
