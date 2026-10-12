# test the streaming of the outputs of a running cell in reactive
# rendering mode: the outputs of a cell are sent to the client as soon as
# the kernel produces them, instead of being sent along with the final
# result of the cell.
import asyncio
import json
import os
import re
import time

import pytest
import tornado.websocket

TIMEOUT = 60

WIDGET_VIEW_MIMETYPE = "application/vnd.jupyter.widget-view+json"

# The first cell prints its first output and then sleeps, so the time at
# which the streamed output was received tells whether it was sent while
# the cell was still executing.
CELL_0_SLEEP = 2.0


@pytest.fixture
def reactive_rendering_mode():
    return True


@pytest.fixture
def voila_notebook(notebook_directory):
    return os.path.join(notebook_directory, "reactive_streaming.ipynb")


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


async def read_messages_until_complete(conn, timeout=TIMEOUT):
    """Read the websocket messages until the last cell has been executed,
    keeping the time at which each of them was received."""
    messages = []
    while True:
        message = await asyncio.wait_for(conn.read_message(), timeout)
        assert message is not None, "Websocket connection closed unexpectedly"
        data = json.loads(message)
        if data["action"] == "execution_error":
            raise AssertionError(f"Execution error: {data['payload']}")
        messages.append((time.monotonic(), data))
        if (
            data["action"] == "execution_result"
            and data["payload"]["cell_index"] + 1 == data["payload"]["total_cell"]
        ):
            return messages


def streamed_outputs(messages, cell_index):
    """The outputs streamed by the cell at `cell_index`."""
    return [
        data["payload"]["output"]
        for _, data in messages
        if data["action"] == "execution_output"
        and data["payload"]["cell_index"] == cell_index
    ]


def final_outputs(messages, cell_index):
    """The outputs of the final result of the cell at `cell_index`."""
    for _, data in messages:
        if (
            data["action"] == "execution_result"
            and data["payload"]["cell_index"] == cell_index
        ):
            return data["payload"]["output_cell"].get("outputs", [])
    raise AssertionError(f"No execution result for cell {cell_index}")


def message_time(messages, action, cell_index):
    for timestamp, data in messages:
        if (
            data["action"] == action
            and data["payload"]["cell_index"] == cell_index
        ):
            return timestamp
    raise AssertionError(f"No {action} message for cell {cell_index}")


async def execute_notebook(conn, kernel_id):
    await conn.write_message(
        json.dumps({"action": "execute", "payload": {"kernel_id": kernel_id}})
    )
    return await read_messages_until_complete(conn)


async def test_outputs_are_streamed_while_a_cell_is_executing(
    http_server_client, base_url, http_server_port
):
    """The outputs of a cell are sent to the client as the kernel produces
    them, without waiting for the end of the cell."""
    kernel_id = await get_kernel_id(http_server_client, base_url)
    conn = await connect_execution_websocket(base_url, http_server_port, kernel_id)
    try:
        messages = await execute_notebook(conn, kernel_id)

        # The two outputs of the first cell are streamed one by one.
        assert [output["output_type"] for output in streamed_outputs(messages, 0)] == [
            "stream",
            "stream",
        ]
        assert "".join(
            output.get("text", "") for output in streamed_outputs(messages, 0)
        ) == "first output\nsecond output\n"

        # The first output is received while the cell is still sleeping,
        # well before its execution result.
        time_to_result = message_time(
            messages, "execution_result", 0
        ) - message_time(messages, "execution_output", 0)
        assert time_to_result > CELL_0_SLEEP / 2, (
            "The first output of the cell was not received while the cell was "
            f"executing (it was received {time_to_result:.2f}s before the "
            "execution result of the cell)"
        )

        # Streamed outputs are the outputs of the final cell result.
        assert "".join(
            output.get("text", "") for output in final_outputs(messages, 0)
        ) == "first output\nsecond output\n"
    finally:
        conn.close()


async def test_streaming_does_not_show_what_the_final_result_hides(
    http_server_client, base_url, http_server_port
):
    """Outputs which are stripped from the final cell outputs are not
    streamed either, so that streaming a running cell never displays what
    the final rendering of the cell does not."""
    kernel_id = await get_kernel_id(http_server_client, base_url)
    conn = await connect_execution_websocket(base_url, http_server_port, kernel_id)
    try:
        messages = await execute_notebook(conn, kernel_id)

        # The stderr output of the second cell is stripped from its final
        # outputs (the tracebacks are hidden by default) and is not streamed.
        assert streamed_outputs(messages, 1) == []
        assert final_outputs(messages, 1) == []

        # The widget view of the third cell is rendered from the final cell
        # outputs, so that a single view is created for the widget model.
        assert streamed_outputs(messages, 2) == []
        assert [
            output
            for output in final_outputs(messages, 2)
            if WIDGET_VIEW_MIMETYPE in output.get("data", {})
        ]
    finally:
        conn.close()


async def read_until_execution_result(conn, cell_index, timeout=TIMEOUT):
    """Read the websocket messages until the cell at `cell_index` has been
    executed."""
    messages = []
    while True:
        message = await asyncio.wait_for(conn.read_message(), timeout)
        assert message is not None, "Websocket connection closed unexpectedly"
        data = json.loads(message)
        if data["action"] == "execution_error":
            raise AssertionError(f"Execution error: {data['payload']}")
        messages.append((time.monotonic(), data))
        if (
            data["action"] == "execution_result"
            and data["payload"]["cell_index"] == cell_index
        ):
            return messages


async def test_clear_output_is_streamed(
    http_server_client, base_url, http_server_port
):
    """`clear_output` messages are streamed along with the outputs, so that
    the client can replace the outputs displayed by a running cell."""
    kernel_id = await get_kernel_id(http_server_client, base_url)
    conn = await connect_execution_websocket(base_url, http_server_port, kernel_id)
    try:
        messages = await execute_notebook(conn, kernel_id)

        # The fourth cell clears its outputs before printing each iteration.
        assert [
            output["output_type"] for output in streamed_outputs(messages, 3)
        ] == [
            "clear_output",
            "stream",
            "clear_output",
            "stream",
            "clear_output",
            "stream",
        ]
        clear_outputs = [
            output
            for output in streamed_outputs(messages, 3)
            if output["output_type"] == "clear_output"
        ]
        assert all(output["wait"] for output in clear_outputs)
        assert "".join(
            output.get("text", "")
            for output in streamed_outputs(messages, 3)
            if output["output_type"] == "stream"
        ) == "iteration 0\niteration 1\niteration 2\n"

        # Only the last iteration is kept by the final cell output.
        assert "".join(
            output.get("text", "") for output in final_outputs(messages, 3)
        ) == "iteration 2\n"
    finally:
        conn.close()


async def test_reexecution_streams_the_outputs_again(
    http_server_client, base_url, http_server_port
):
    """The outputs of a cell are streamed again when it is re-executed."""
    kernel_id = await get_kernel_id(http_server_client, base_url)
    conn = await connect_execution_websocket(base_url, http_server_port, kernel_id)
    try:
        await execute_notebook(conn, kernel_id)

        await conn.write_message(
            json.dumps(
                {
                    "action": "execute_cells",
                    "payload": {"kernel_id": kernel_id, "cell_ids": [0]},
                }
            )
        )
        messages = await read_until_execution_result(conn, 0)

        assert "".join(
            output.get("text", "") for output in streamed_outputs(messages, 0)
        ) == "first output\nsecond output\n"
    finally:
        conn.close()
