/***************************************************************************
 * Copyright (c) 2026, Voilà contributors                                   *
 *                                                                          *
 * Distributed under the terms of the BSD 3-Clause License.                 *
 *                                                                          *
 * The full license is in the file LICENSE, distributed with this software. *
 ****************************************************************************/
import { FileBrowserModel } from '@jupyterlab/filebrowser';
import { Widget } from '@lumino/widgets';

import { GitClient } from './git';
import { IGitBarView, IGitStatus, pullSummary, renderStatus } from './gitview';

/**
 * The git status bar of the tree page.
 *
 * It shows the branch, the head commit, the tag of that commit and the state
 * of the worktree of the repository the browsed folder belongs to, and lets the
 * user switch branch or pull. It follows the folder shown by the file browser.
 */
export class VoilaGitBar extends Widget {
  constructor(options: VoilaGitBar.IOptions) {
    super();
    // The shell's top area uses the widget id as the DOM id, and refuses
    // widgets without one.
    this.id = 'voila-git-bar';
    this.addClass('voila-GitBar');
    this._model = options.model;
    this._client = options.client;
    this._status = null;

    this._branch = this._createSpan('voila-GitBar-branch');
    this._tag = this._createSpan('voila-GitBar-tag');
    this._commit = this._createSpan('voila-GitBar-commit');
    this._worktree = this._createSpan('voila-GitBar-worktree');
    this._incoming = this._createSpan('voila-GitBar-incoming');

    this._branches = document.createElement('select');
    this._branches.className = 'voila-GitBar-branches';
    this._branches.title = 'Switch branch';
    this._branches.addEventListener('change', () => {
      void this._switchBranch();
    });

    this._pull = document.createElement('button');
    this._pull.className = 'voila-GitBar-pull';
    this._pull.textContent = 'Pull';
    this._pull.addEventListener('click', () => {
      void this._pullChanges();
    });

    this._message = document.createElement('div');
    this._message.className = 'voila-GitBar-message';

    const summary = document.createElement('div');
    summary.className = 'voila-GitBar-summary';
    summary.append(
      this._branch,
      this._tag,
      this._commit,
      this._worktree,
      this._incoming
    );

    const actions = document.createElement('div');
    actions.className = 'voila-GitBar-actions';
    actions.append(this._branches, this._pull);

    this.node.append(summary, actions, this._message);

    this._model.pathChanged.connect(this._onPathChanged, this);
    void this.refresh();
  }

  /**
   * Re-read the status of the folder currently shown and update the bar.
   */
  async refresh(): Promise<void> {
    try {
      this._showStatus(await this._client.status(this._model.path));
      this._setMessage('');
    } catch (reason) {
      this.setHidden(false);
      this._setMessage(this._errorMessage(reason), true);
    }
  }

  /**
   * Dispose of the widget's resources.
   */
  dispose(): void {
    this._model.pathChanged.disconnect(this._onPathChanged, this);
    super.dispose();
  }

  private _onPathChanged(): void {
    this._status = null;
    void this.refresh();
  }

  private async _switchBranch(): Promise<void> {
    const branch = this._branches.value;
    if (this._busy || !branch || branch === this._branches.dataset.current) {
      return;
    }
    await this._run(() => this._client.checkout(this._model.path, branch));
  }

  private async _pullChanges(): Promise<void> {
    if (this._busy) {
      return;
    }
    const before = this._status;
    await this._run(
      () => this._client.pull(this._model.path),
      before ? pullSummary(before) : ''
    );
  }

  private async _run(
    action: () => Promise<IGitStatus>,
    successMessage = ''
  ): Promise<void> {
    this._busy = true;
    this._pull.disabled = true;
    this._branches.disabled = true;
    this._setMessage('Working...');
    try {
      this._showStatus(await action());
      this._setMessage(successMessage);
    } catch (reason) {
      this._setMessage(this._errorMessage(reason), true);
      // The status may have moved even when the action failed (a pull that was
      // reverted, a checkout git refused): show what the server has now.
      try {
        this._showStatus(await this._client.status(this._model.path));
      } catch (ignored) {
        // Keep the error of the action, it is the informative one.
      }
    } finally {
      this._busy = false;
      this._pull.disabled = !this._view?.canPull;
      this._branches.disabled = !this._view?.canSwitch;
    }
  }

  private _showStatus(status: IGitStatus): void {
    this._status = status;
    const view = (this._view = renderStatus(status));
    this.setHidden(!view.visible);
    if (!view.visible) {
      return;
    }

    this._branch.textContent = view.branch;
    this._tag.textContent = view.tag ? `tag ${view.tag}` : '';
    this._tag.hidden = view.tag === null;
    this._commit.textContent = view.commit ?? '';
    this._commit.hidden = view.commit === null;
    this._commit.title = status.head?.hash ?? '';
    this._worktree.textContent = view.worktree;
    this._worktree.classList.toggle(
      'voila-GitBar-worktree-dirty',
      view.worktreeDirty
    );
    this._incoming.textContent = view.incoming;
    this._incoming.hidden = view.incoming === '';

    this._branches.replaceChildren(
      ...view.branches.map((branch) => {
        const option = document.createElement('option');
        option.value = branch;
        option.textContent = branch;
        option.selected = branch === view.selected;
        return option;
      })
    );
    this._branches.dataset.current = view.selected;
    this._branches.hidden = !view.canSwitch;
    this._branches.disabled = !view.canSwitch;
    this._pull.disabled = !view.canPull;
    this._pull.title = view.pullHint;
  }

  private _setMessage(text: string, isError = false): void {
    this._message.textContent = text;
    this._message.hidden = text === '';
    this._message.classList.toggle('voila-GitBar-message-error', isError);
  }

  private _errorMessage(reason: unknown): string {
    if (reason instanceof Error && reason.message) {
      return reason.message;
    }
    return String(reason);
  }

  private _createSpan(className: string): HTMLSpanElement {
    const span = document.createElement('span');
    span.className = className;
    return span;
  }

  private _model: FileBrowserModel;
  private _client: GitClient;
  private _status: IGitStatus | null;
  private _view: IGitBarView | null = null;
  private _busy = false;
  private _branch: HTMLSpanElement;
  private _tag: HTMLSpanElement;
  private _commit: HTMLSpanElement;
  private _worktree: HTMLSpanElement;
  private _incoming: HTMLSpanElement;
  private _branches: HTMLSelectElement;
  private _pull: HTMLButtonElement;
  private _message: HTMLDivElement;
}

export namespace VoilaGitBar {
  export interface IOptions {
    model: FileBrowserModel;
    client: GitClient;
  }
}
