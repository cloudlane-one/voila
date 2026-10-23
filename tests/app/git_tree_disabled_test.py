# the git bar and its endpoints are off unless they are asked for: they run git
# in the served directory, so they are not part of the default surface.
import pytest
from tornado.httpclient import HTTPClientError


@pytest.fixture
def voila_args(notebook_directory, voila_args_extra):
    return ["--VoilaTest.root_dir=%s" % notebook_directory, *voila_args_extra]


async def test_tree_page_does_not_advertise_the_git_bar(http_server_client, base_url):
    response = await http_server_client.fetch(base_url)

    assert '"gitUi": false' in response.body.decode("utf-8")


async def test_git_endpoints_do_not_exist(http_server_client, base_url):
    with pytest.raises(HTTPClientError) as error:
        await http_server_client.fetch(base_url + "voila/api/git/status")

    assert error.value.code == 404
