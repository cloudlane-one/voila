# test that a kernel-side exception other than HaltExecution terminates
# the execution in reactive rendering mode.
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
    return os.path.join(notebook_directory, "reactive_error.ipynb")


async def get_kernel_id(http_server_client, base_url):
    response = await http_server_client.fetch(base_url, request_timeout=180)
    html_text = response.body.decode("utf-8")
    assert '"reactiveRendering": true' in html_text
    pattern = r"""kernelId": ["']([0-9a-zA-Z-]+)["']"""
    groups = re.findall(pattern, html_text)
    return groups[0]


async def connect_execution_websocket(base_url, http_server_port, kernel_id):
    url = f"ws://localhost:{http_server_port[1]}{base_url}voila/execution/{kernel_id}"
    conn = await tornado.websocket.websocket_connect(url)
    message = json.loads(await conn.read_message())
    assert message["action"] == "initialized"
    return conn


async def read_execution_results(conn, count, timeout=TIMEOUT):
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


async def assert_no_more_results(conn, timeout=5):
    try:
        message = await asyncio.wait_for(conn.read_message(), timeout)
    except asyncio.TimeoutError:
        return
    if message is None:
        return
    data = json.loads(message)
    raise AssertionError(f"Unexpected message: {data}")


def output_texts(payload):
    return "".join(
        output.get("text", "") for output in payload["output_cell"].get("outputs", [])
    )


async def test_error_terminates_execution(
    http_server_client, base_url, http_server_port
):
    """A kernel-side exception other than HaltExecution terminates the
    execution: the following cells are not executed."""
    kernel_id = await get_kernel_id(http_server_client, base_url)
    conn = await connect_execution_websocket(base_url, http_server_port, kernel_id)
    try:
        await conn.write_message(
            json.dumps({"action": "execute", "payload": {"kernel_id": kernel_id}})
        )
        results = await read_execution_results(conn, 2)
        # The first cell is executed normally.
        assert results[0]["cell_index"] == 0
        assert "cell 0" in output_texts(results[0])
        # The second cell raises a ValueError: an error output is displayed.
        assert results[1]["cell_index"] == 1
        error_outputs = [
            output
            for output in results[1]["output_cell"].get("outputs", [])
            if output.get("output_type") == "error"
        ]
        assert len(error_outputs) == 1
        # The execution is terminated: the third cell is not executed.
        await assert_no_more_results(conn)
    finally:
        conn.close()


async def test_execute_cells_after_error(
    http_server_client, base_url, http_server_port
):
    """The kernel is still usable after a terminated execution: cells can
    be re-executed on demand."""
    kernel_id = await get_kernel_id(http_server_client, base_url)
    conn = await connect_execution_websocket(base_url, http_server_port, kernel_id)
    try:
        await conn.write_message(
            json.dumps({"action": "execute", "payload": {"kernel_id": kernel_id}})
        )
        await read_execution_results(conn, 2)
        await assert_no_more_results(conn)

        await conn.write_message(
            json.dumps(
                {
                    "action": "execute_cells",
                    "payload": {"kernel_id": kernel_id, "cell_ids": [2]},
                }
            )
        )
        results = await read_execution_results(conn, 1)
        assert results[0]["cell_index"] == 2
        assert "cell 2" in output_texts(results[0])
    finally:
        conn.close()
