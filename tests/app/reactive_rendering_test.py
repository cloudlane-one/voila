# test the reactive rendering mode: page config, full execution and
# selective re-execution of cells over the execution websocket.
import asyncio
import json
import os
import re

import pytest
import tornado.websocket

TIMEOUT = 60


@pytest.fixture
def reactive_rendering_mode():
    return True


@pytest.fixture
def voila_notebook(notebook_directory):
    return os.path.join(notebook_directory, "reactive.ipynb")


async def get_kernel_id(http_server_client, base_url):
    """Fetch the rendered page and extract the kernel id from the page
    config."""
    response = await http_server_client.fetch(base_url, request_timeout=180)
    html_text = response.body.decode("utf-8")
    assert '"reactiveRendering": true' in html_text
    pattern = r"""kernelId": ["']([0-9a-zA-Z-]+)["']"""
    groups = re.findall(pattern, html_text)
    return groups[0]


async def connect_execution_websocket(base_url, http_server_port, kernel_id):
    """Open the execution websocket and wait for the initialized message."""
    url = f"ws://localhost:{http_server_port[1]}{base_url}voila/execution/{kernel_id}"
    conn = await tornado.websocket.websocket_connect(url)
    message = json.loads(await conn.read_message())
    assert message["action"] == "initialized"
    return conn


async def read_execution_results(conn, count, timeout=TIMEOUT):
    """Read websocket messages until `count` execution results are
    received."""
    results = []
    while len(results) < count:
        message = await asyncio.wait_for(conn.read_message(), timeout)
        assert message is not None, "Websocket connection closed unexpectedly"
        data = json.loads(message)
        if data["action"] == "execution_result":
            results.append(data["payload"])
        elif data["action"] == "execution_error":
            raise AssertionError(f"Execution error: {data['payload']}")
    return results


async def read_all_execution_results(conn, timeout=5):
    """Read websocket messages until no message arrives for `timeout`
    seconds."""
    results = []
    while True:
        try:
            message = await asyncio.wait_for(conn.read_message(), timeout)
        except asyncio.TimeoutError:
            break
        if message is None:
            break
        data = json.loads(message)
        if data["action"] == "execution_result":
            results.append(data["payload"])
        elif data["action"] == "execution_error":
            raise AssertionError(f"Execution error: {data['payload']}")
    return results


def output_texts(payload):
    return "".join(
        output.get("text", "") for output in payload["output_cell"].get("outputs", [])
    )


async def test_page_config(http_server_client, base_url):
    """The notebook is not executed when the page is rendered and the
    reactiveRendering option is passed to the frontend."""
    response = await http_server_client.fetch(base_url, request_timeout=180)
    html_text = response.body.decode("utf-8")
    assert '"reactiveRendering": true' in html_text
    assert "cell 0" not in html_text


async def test_stale_outputs_are_greyed_out_and_disabled(http_server_client, base_url):
    """The page carries the styles greying out and disabling the outputs of
    the cells which are not up to date with the current widget values: the
    client adds the `voila-stale-cell` class to the cells it is reloading,
    and to the cells which a stopped re-execution left behind."""
    response = await http_server_client.fetch(base_url, request_timeout=180)
    html_text = response.body.decode("utf-8")
    match = re.search(r"\.voila-stale-cell\s*\{([^}]*)\}", html_text)
    assert match is not None, "no style for the cells with stale outputs"
    declarations = match.group(1)
    # The outputs are greyed out...
    assert "opacity" in declarations
    # ...and disabled.
    assert "pointer-events: none" in declarations


async def test_execute(http_server_client, base_url, http_server_port):
    """All cells are executed through the execution websocket."""
    kernel_id = await get_kernel_id(http_server_client, base_url)
    conn = await connect_execution_websocket(base_url, http_server_port, kernel_id)
    try:
        await conn.write_message(
            json.dumps({"action": "execute", "payload": {"kernel_id": kernel_id}})
        )
        results = await read_execution_results(conn, 3)
        assert [result["cell_index"] for result in results] == [0, 1, 2]
        for cell_index, result in enumerate(results):
            assert result["total_cell"] == 3
            assert f"cell {cell_index}" in output_texts(result)
    finally:
        conn.close()


async def test_execute_cells(http_server_client, base_url, http_server_port):
    """Only the requested cells are re-executed, in the requested order."""
    kernel_id = await get_kernel_id(http_server_client, base_url)
    conn = await connect_execution_websocket(base_url, http_server_port, kernel_id)
    try:
        await conn.write_message(
            json.dumps({"action": "execute", "payload": {"kernel_id": kernel_id}})
        )
        await read_execution_results(conn, 3)

        await conn.write_message(
            json.dumps(
                {
                    "action": "execute_cells",
                    "payload": {"kernel_id": kernel_id, "cell_ids": [2, 0]},
                }
            )
        )
        results = await read_execution_results(conn, 2)
        assert [result["cell_index"] for result in results] == [2, 0]
        assert "cell 2" in output_texts(results[0])
        assert "cell 0" in output_texts(results[1])
    finally:
        conn.close()


async def test_execute_cells_wrong_kernel_id(
    http_server_client, base_url, http_server_port
):
    """A mismatching kernel id is rejected."""
    kernel_id = await get_kernel_id(http_server_client, base_url)
    conn = await connect_execution_websocket(base_url, http_server_port, kernel_id)
    try:
        await conn.write_message(
            json.dumps(
                {
                    "action": "execute_cells",
                    "payload": {"kernel_id": "wrong", "cell_ids": [0]},
                }
            )
        )
        message = json.loads(await asyncio.wait_for(conn.read_message(), TIMEOUT))
        assert message["action"] == "execution_error"
        assert message["payload"]["error"] == "Kernel ID does not match"
    finally:
        conn.close()


async def test_execute_cells_missing_cell_ids(
    http_server_client, base_url, http_server_port
):
    """A missing cell_ids field is rejected."""
    kernel_id = await get_kernel_id(http_server_client, base_url)
    conn = await connect_execution_websocket(base_url, http_server_port, kernel_id)
    try:
        await conn.write_message(
            json.dumps({"action": "execute_cells", "payload": {"kernel_id": kernel_id}})
        )
        message = json.loads(await asyncio.wait_for(conn.read_message(), TIMEOUT))
        assert message["action"] == "execution_error"
        assert message["payload"]["error"] == "Missing or invalid cell_ids"
    finally:
        conn.close()
