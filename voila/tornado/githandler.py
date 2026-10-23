#############################################################################
# Copyright (c) 2026, Voilà Contributors                                     #
#                                                                           #
# Distributed under the terms of the BSD 3-Clause License.                  #
#                                                                           #
# The full license is in the file LICENSE, distributed with this software.  #
#############################################################################
"""Git endpoints behind the status bar of the tree page.

They are only registered when ``VoilaConfiguration.git_ui`` is enabled, they
require authentication, and every action runs in a worker thread because git
is a blocking subprocess.
"""

from __future__ import annotations

from functools import partial
from typing import Any, Callable, Dict

from jupyter_server.base.handlers import APIHandler
from tornado import web
from tornado.ioloop import IOLoop

from ..git import GitError, GitStatus, checkout, get_status, pull
from ..git import resolve_served_directory


class VoilaGitHandler(APIHandler):
    """Common plumbing of the git endpoints."""

    def initialize(self, **kwargs: Any) -> None:
        super().initialize()
        self.root_dir: str = kwargs["root_dir"]
        self.git_executable: str = kwargs["git_executable"]

    def _directory(self, contents_path: Any) -> str:
        if contents_path is None:
            contents_path = ""
        if not isinstance(contents_path, str):
            raise web.HTTPError(400, "The 'path' argument must be a string.")
        try:
            return resolve_served_directory(self.root_dir, contents_path)
        except GitError as error:
            raise web.HTTPError(400, str(error)) from error

    def _body(self) -> Dict[str, Any]:
        body = self.get_json_body()
        if not isinstance(body, dict):
            raise web.HTTPError(400, "A JSON object body is required.")
        return body

    async def _invoke(
        self, function: Callable[..., GitStatus], **kwargs: Any
    ) -> Dict[str, Any]:
        """Run a git function in a thread and return the new status."""
        try:
            status = await IOLoop.current().run_in_executor(
                None,
                partial(function, executable=self.git_executable, **kwargs),
            )
        except GitError as error:
            self.log.warning("git request failed: %s", error)
            raise web.HTTPError(400, str(error)) from error
        return status.to_dict()

    def _finish_status_dict(self, status: Dict[str, Any]) -> None:
        self.set_header("Content-Type", "application/json")
        self.finish(status)


class VoilaGitStatusHandler(VoilaGitHandler):
    """Report the git state of the folder being browsed."""

    @web.authenticated
    async def get(self) -> None:
        directory = self._directory(self.get_query_argument("path", ""))
        self._finish_status_dict(await self._invoke(get_status, directory=directory))


class VoilaGitCheckoutHandler(VoilaGitHandler):
    """Switch the repository to another local or remote branch."""

    @web.authenticated
    async def post(self) -> None:
        body = self._body()
        branch = body.get("branch")
        if not isinstance(branch, str) or not branch.strip():
            raise web.HTTPError(400, "The 'branch' field must be a non-empty string.")
        directory = self._directory(body.get("path", ""))
        self._finish_status_dict(
            await self._invoke(checkout, directory=directory, branch=branch)
        )


class VoilaGitPullHandler(VoilaGitHandler):
    """Pull the current branch of the repository."""

    @web.authenticated
    async def post(self) -> None:
        body = self._body()
        directory = self._directory(body.get("path", ""))
        self._finish_status_dict(await self._invoke(pull, directory=directory))
