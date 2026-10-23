# test the git endpoints behind the tree page's status bar: reading the status
# of the served folder, switching branch and pulling.
import json
import subprocess
import urllib.parse

import pytest
from tornado.httpclient import HTTPClientError

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
    (repo / name).write_text(content)
    git(repo, "add", name)
    git(repo, "commit", "-m", message)


@pytest.fixture
def served_repository(tmp_path):
    """A repository served as the app's root directory."""
    root = tmp_path / "root"
    root.mkdir()
    git(root, "init", "-q")
    commit(root, "print.ipynb", "{}", "one")
    git(root, "branch", "other")
    return root


@pytest.fixture
def remote_clone(tmp_path, served_repository):
    """A clone of the served repository's remote, to push from."""
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    git(served_repository, "remote", "add", "origin", str(bare))
    git(served_repository, "push", "-q", "-u", "origin", "main")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(bare), str(clone))
    return clone


@pytest.fixture
def voila_args_extra():
    return ["--VoilaConfiguration.git_ui=True", "--VoilaExecutor.timeout=60"]


@pytest.fixture
def voila_args(served_repository, voila_args_extra):
    return ["--VoilaTest.root_dir=%s" % served_repository, *voila_args_extra]


class GitApi:
    """The git endpoints, as the tree page talks to them."""

    def __init__(self, client, base_url):
        self._client = client
        self._base_url = base_url
        self._cookie = None

    async def connect(self):
        """Load the tree page, which is where the browser gets its XSRF cookie."""
        page = await self._client.fetch(self._base_url)
        for header in page.headers.get_list("Set-Cookie"):
            if header.startswith("_xsrf="):
                self._cookie = header.split(";", 1)[0]
                # The cookie value is percent-encoded; the header that tornado
                # compares carries the decoded token, as a browser's would.
                self._token = urllib.parse.unquote(self._cookie.split("=", 1)[1])
                return self
        raise AssertionError("the tree page did not set the _xsrf cookie")

    async def status(self, path=""):
        response = await self._client.fetch(
            self._base_url + "voila/api/git/status?path=" + urllib.parse.quote(path)
        )
        return json.loads(response.body)

    async def post(self, endpoint, payload):
        assert self._cookie is not None, "call connect() first"
        return await self._client.fetch(
            self._base_url + "voila/api/git/" + endpoint,
            method="POST",
            body=json.dumps(payload),
            headers={
                "Content-Type": "application/json",
                "Cookie": self._cookie,
                "X-XSRFToken": self._token,
            },
        )


def message_of(error):
    return json.loads(error.value.response.body)["message"]


async def test_tree_page_advertises_the_git_bar(http_server_client, base_url):
    response = await http_server_client.fetch(base_url)

    assert '"gitUi": true' in response.body.decode("utf-8")


async def test_status_of_the_served_repository(
    http_server_client, base_url, served_repository
):
    status = await GitApi(http_server_client, base_url).status()

    assert status["is_repository"] is True
    assert status["repository_root"] == str(served_repository)
    assert status["branch"] == "main"
    assert status["detached"] is False
    assert status["head"]["subject"] == "one"
    assert status["tag"] is None
    assert status["dirty"] is False
    assert status["changed_files"] == 0
    assert "main" in status["local_branches"]
    assert "other" in status["local_branches"]
    assert status["incoming"] == []


async def test_status_of_a_subfolder(http_server_client, base_url, served_repository):
    (served_repository / "sub").mkdir()

    status = await GitApi(http_server_client, base_url).status("/sub")

    assert status["is_repository"] is True
    assert status["repository_root"] == str(served_repository)


async def test_status_rejects_a_path_outside_the_served_directory(
    http_server_client, base_url
):
    with pytest.raises(HTTPClientError) as error:
        await GitApi(http_server_client, base_url).status("/../")

    assert error.value.code == 400
    assert "outside the served directory" in message_of(error)


async def test_status_rejects_a_missing_folder(http_server_client, base_url):
    with pytest.raises(HTTPClientError) as error:
        await GitApi(http_server_client, base_url).status("/missing")

    assert error.value.code == 400
    assert "not a directory" in message_of(error)


async def test_checkout_switches_branch(http_server_client, base_url):
    api = await GitApi(http_server_client, base_url).connect()

    response = await api.post("checkout", {"path": "", "branch": "other"})

    assert response.code == 200
    assert json.loads(response.body)["branch"] == "other"
    assert (await api.status())["branch"] == "other"


async def test_checkout_rejects_an_unknown_branch(http_server_client, base_url):
    api = await GitApi(http_server_client, base_url).connect()

    with pytest.raises(HTTPClientError) as error:
        await api.post("checkout", {"path": "", "branch": "nope"})

    assert error.value.code == 400
    assert "no branch named" in message_of(error)


async def test_checkout_requires_a_branch(http_server_client, base_url):
    api = await GitApi(http_server_client, base_url).connect()

    with pytest.raises(HTTPClientError) as error:
        await api.post("checkout", {"path": ""})

    assert error.value.code == 400
    assert "branch" in message_of(error)


async def test_pull_brings_the_commits_of_the_remote(
    http_server_client, base_url, served_repository, remote_clone
):
    api = await GitApi(http_server_client, base_url).connect()
    commit(remote_clone, "two.ipynb", "{}", "two")
    git(remote_clone, "push", "-q")
    before = await api.status()
    assert before["behind"] == 1
    assert [item["subject"] for item in before["incoming"]] == ["two"]

    response = await api.post("pull", {"path": ""})

    assert response.code == 200
    status = json.loads(response.body)
    assert status["behind"] == 0
    assert status["head"]["subject"] == "two"
    assert (served_repository / "two.ipynb").exists()


async def test_pull_refuses_a_dirty_worktree(
    http_server_client, base_url, served_repository, remote_clone
):
    api = await GitApi(http_server_client, base_url).connect()
    commit(remote_clone, "two.ipynb", "{}", "two")
    git(remote_clone, "push", "-q")
    (served_repository / "local.txt").write_text("local\n")

    with pytest.raises(HTTPClientError) as error:
        await api.post("pull", {"path": ""})

    assert error.value.code == 400
    assert "uncommitted changes" in message_of(error)
