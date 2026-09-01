# test the graceful pausing of the execution with the HaltExecution
# exception in reactive rendering mode.
import asyncio
import json
import os
import re

import pytest
import tornado.websocket

TIMEOUT = 60

WIDGET_VIEW_MIMETYPE = "application/vnd.jupyter.widget-view+json"


@pytest.fixture
def reactive_rendering_mode():
    return True


@pytest.fixture
def voila_notebook(notebook_directory):
    return os.path.join(notebook_directory, "reactive_halt.ipynb")


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


def has_continue_button(payload):
    """Check that the output cell contains a widget view (the continue
    button) and no error output."""
    outputs = payload["output_cell"].get("outputs", [])
    has_widget_view = any(
        WIDGET_VIEW_MIMETYPE in output.get("data", {}) for output in outputs
    )
    has_error = any(output.get("output_type") == "error" for output in outputs)
    return has_widget_view and not has_error


async def test_halt_execution_pauses_execution(
    http_server_client, base_url, http_server_port
):
    """When a cell raises HaltExecution, a continue button is displayed
    instead of the stack trace and the following cells are not executed."""
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
        # The second cell raises HaltExecution: a continue button widget is
        # displayed instead of the stack trace.
        assert results[1]["cell_index"] == 1
        assert has_continue_button(results[1])
        # The execution is paused: the third cell is not executed.
        await assert_no_more_results(conn)
    finally:
        conn.close()


async def test_continue_button_reexecutes_cells_below(
    http_server_client, base_url, http_server_port
):
    """Triggering the continue button (i.e. requesting the re-execution of
    the halted cell and the cells below, like the frontend does when the
    button is clicked) resumes the execution."""
    kernel_id = await get_kernel_id(http_server_client, base_url)
    conn = await connect_execution_websocket(base_url, http_server_port, kernel_id)
    try:
        await conn.write_message(
            json.dumps({"action": "execute", "payload": {"kernel_id": kernel_id}})
        )
        results = await read_execution_results(conn, 2)
        assert has_continue_button(results[1])
        await assert_no_more_results(conn)

        # Simulate a click on the continue button: the frontend re-executes
        # the cell of the button and all the cells below.
        await conn.write_message(
            json.dumps(
                {
                    "action": "execute_cells",
                    "payload": {"kernel_id": kernel_id, "cell_ids": [1, 2]},
                }
            )
        )
        results = await read_execution_results(conn, 2)
        # The halted cell does not raise HaltExecution anymore and the
        # remaining cells are executed.
        assert [result["cell_index"] for result in results] == [1, 2]
        assert "cell 1 continued" in output_texts(results[0])
        assert "cell 2" in output_texts(results[1])
    finally:
        conn.close()
