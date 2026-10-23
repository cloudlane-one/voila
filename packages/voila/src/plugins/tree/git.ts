/***************************************************************************
 * Copyright (c) 2026, Voilà contributors                                   *
 *                                                                          *
 * Distributed under the terms of the BSD 3-Clause License.                 *
 *                                                                          *
 * The full license is in the file LICENSE, distributed with this software. *
 ****************************************************************************/
import { PageConfig, URLExt } from '@jupyterlab/coreutils';
import { ServerConnection } from '@jupyterlab/services';

import { IGitStatus } from './gitview';

/**
 * The client of the git endpoints of the Voilà server.
 */
export class GitClient {
  constructor(settings: ServerConnection.ISettings) {
    this._settings = settings;
  }

  /**
   * Get the git state of a folder of the served directory.
   */
  async status(path: string): Promise<IGitStatus> {
    const url = this._url('status') + `?path=${encodeURIComponent(path)}`;
    return this._request(url, { method: 'GET' });
  }

  /**
   * Switch the repository to another branch, local or remote.
   */
  async checkout(path: string, branch: string): Promise<IGitStatus> {
    return this._request(this._url('checkout'), {
      method: 'POST',
      body: JSON.stringify({ path, branch })
    });
  }

  /**
   * Pull the current branch.
   */
  async pull(path: string): Promise<IGitStatus> {
    return this._request(this._url('pull'), {
      method: 'POST',
      body: JSON.stringify({ path })
    });
  }

  private _url(endpoint: string): string {
    return (
      URLExt.join(PageConfig.getBaseUrl(), 'voila/api/git') + '/' + endpoint
    );
  }

  private async _request(url: string, init: RequestInit): Promise<IGitStatus> {
    const response = await ServerConnection.makeRequest(
      url,
      init,
      this._settings
    );
    if (!response.ok) {
      // ResponseError carries the `message` of the server's JSON error body.
      throw await ServerConnection.ResponseError.create(response);
    }
    return (await response.json()) as IGitStatus;
  }

  private _settings: ServerConnection.ISettings;
}
