import asyncio
import json
import sys
import traceback
from typing import Any, Awaitable, Dict, List, Tuple, Union

import nbformat
from jupyter_core.utils import ensure_async
from jupyter_server.base.handlers import JupyterHandler
from nbclient.exceptions import CellExecutionError
from tornado.web import HTTPError
from tornado.websocket import WebSocketHandler

from voila.execute import VoilaExecutor, strip_code_cell_warnings

try:
    JUPYTER_SERVER_2 = True
    from jupyter_server.base.websocket import WebSocketMixin
except ImportError:
    JUPYTER_SERVER_2 = False

if JUPYTER_SERVER_2:

    class ReactiveExecutionHandler(WebSocketMixin, WebSocketHandler, JupyterHandler):
        """WebSocket handler used in reactive rendering mode.

        It supports the same ``execute`` action as the
        :class:`ExecutionRequestHandler` used in progressive rendering mode,
        plus an ``execute_cells`` action used to re-execute a subset of cells
        when a widget rendered in a cell output is updated by the user.

        The websocket connection is kept open after the full execution so
        that cells can be re-executed on demand.

        The exceptions raised by the cells are not interpreted: whatever the
        kernel-side exception, the execution of the remaining cells is stopped
        and the error output of the cell is sent to the client. Gracefully
        pausing a notebook, and offering a way to resume it, is the
        responsibility of the kernel-side code (see ``docs/customize.md``).
        """

        _execution_data = {}

        def initialize(self, **kwargs):
            super().initialize()
            self._executor = None
            self._execution_lock = asyncio.Lock()

        async def open(self, kernel_id: str) -> None:
            """Create a new websocket connection, this connection is
            identified by the kernel id.

            Args:
                kernel_id (str): Kernel id used by the notebook when it opens
                the websocket connection.
            """
            identity_provider = self.settings.get("identity_provider")
            user = await ensure_async(identity_provider.get_user(self))
            if user is None:
                raise HTTPError(403, "Unauthenticated")
            super().open()

            self._kernel_id = kernel_id
            self.write_message({"action": "initialized", "payload": {}})

        async def on_message(
            self, message_str: Union[str, bytes]
        ) -> Union[Awaitable[None], None]:
            message = json.loads(message_str)
            action = message.get("action", None)
            payload = message.get("payload", {})
            if action == "execute":
                request_kernel_id = payload.get("kernel_id")
                if request_kernel_id != self._kernel_id:
                    await self.write_message(
                        {
                            "action": "execution_error",
                            "payload": {"error": "Kernel ID does not match"},
                        }
                    )
                    return
                executor, nb = await self._get_executor()
                if executor is None:
                    return
                # Serialize the executions: concurrent `on_message`
                # coroutines would otherwise execute cells concurrently
                # on the same kernel.
                async with self._execution_lock:
                    await self._execute_cells(
                        executor, nb, list(range(len(nb.cells)))
                    )
            elif action == "execute_cells":
                request_kernel_id = payload.get("kernel_id")
                if request_kernel_id != self._kernel_id:
                    await self.write_message(
                        {
                            "action": "execution_error",
                            "payload": {"error": "Kernel ID does not match"},
                        }
                    )
                    return
                cell_ids = payload.get("cell_ids")
                if not isinstance(cell_ids, list):
                    await self.write_message(
                        {
                            "action": "execution_error",
                            "payload": {"error": "Missing or invalid cell_ids"},
                        }
                    )
                    return
                executor, nb = await self._get_executor()
                if executor is None:
                    return
                async with self._execution_lock:
                    await self._execute_cells(executor, nb, cell_ids)

        async def _get_executor(
            self,
        ) -> Tuple[Union[VoilaExecutor, None], Union[nbformat.NotebookNode, None]]:
            """Get the executor for the current kernel, creating it on first
            use so that the same kernel client is reused for all the
            execution requests of this connection.
            """
            kernel_future = self.kernel_manager.get_kernel(self._kernel_id)
            km = await ensure_async(kernel_future)
            # The execution data is not popped from the store, contrary to
            # the progressive rendering handler, since cells can be
            # re-executed several times over the lifetime of the connection.
            execution_data = self._execution_data.get(self._kernel_id, None)
            if execution_data is None:
                await self.write_message(
                    {
                        "action": "execution_error",
                        "payload": {"error": "Missing notebook data"},
                    }
                )
                return None, None
            nb = execution_data["nb"]
            if self._executor is None:
                self._executor = executor = VoilaExecutor(
                    nb,
                    km=km,
                    config=execution_data["config"],
                    show_tracebacks=execution_data["show_tracebacks"],
                )
                # Stream the outputs of the cells to the client while they are
                # executing, so that they are displayed as they are produced.
                executor.output_callback = self._send_output
                executor.kc = await executor.async_start_new_kernel_client()
            return self._executor, nb

        def _send_output(self, cell_index: int, output: Dict[str, Any]) -> None:
            """Send an output of a still running cell to the client.

            This is called by the executor while it processes the IOPub
            messages of the cell, so the outputs of a cell are always
            received in order, and before the execution result of that cell.

            Args:
                cell_index (int): Index of the cell producing the output.
                output (Dict[str, Any]): Content of the IOPub message,
                    extended with its ``output_type``.
            """
            try:
                self.write_message(
                    {
                        "action": "execution_output",
                        "payload": {"cell_index": cell_index, "output": output},
                    }
                )
            except Exception:
                # Failing to stream an output must not abort the execution of
                # the notebook: the final cell outputs are sent anyway.
                self.log.exception(
                    "Error at server while streaming an output of cell %s",
                    cell_index,
                )

        async def _execute_cells(
            self,
            executor: VoilaExecutor,
            nb: nbformat.NotebookNode,
            cell_ids: List[int],
        ) -> None:
            """Execute the given cells in order and send the execution
            results (or errors) back to the client, one cell at a time.

            Args:
                executor (VoilaExecutor): The executor used to run the cells.
                nb (nbformat.NotebookNode): The notebook being executed.
                cell_ids (List[int]): The indexes of the cells to execute,
                    executed in the given order.
            """
            total_cell = len(nb.cells)
            for cell_idx in cell_ids:
                if not isinstance(cell_idx, int) or cell_idx < 0 or cell_idx >= total_cell:
                    await self.write_message(
                        {
                            "action": "execution_error",
                            "payload": {"error": f"Invalid cell index: {cell_idx}"},
                        }
                    )
                    continue
                input_cell = nb.cells[cell_idx]
                stop_execution = False
                try:
                    output_cell = await executor.execute_cell(
                        input_cell, None, cell_idx, store_history=False
                    )
                except TimeoutError:
                    output_cell = input_cell

                except CellExecutionError:
                    # A kernel-side exception terminated the execution: the
                    # cell already holds the outputs produced before (and
                    # including) the exception, send them as they are.
                    self.log.exception(
                        "Error at server while executing cell: %r", input_cell
                    )
                    if executor.should_strip_error():
                        strip_code_cell_warnings(input_cell)
                        executor.strip_code_cell_errors(input_cell)
                    output_cell = input_cell
                    # Do not run the following cells.
                    stop_execution = True

                except Exception as e:
                    self.log.exception(
                        "Error at server while executing cell: %r", input_cell
                    )
                    output_cell = nbformat.v4.new_code_cell()
                    if executor.should_strip_error():
                        output_cell.outputs = [
                            {
                                "output_type": "stream",
                                "name": "stderr",
                                "text": "An exception occurred at the server (not the notebook). {}".format(
                                    executor.cell_error_instruction
                                ),
                            }
                        ]
                    else:
                        output_cell.outputs = [
                            {
                                "output_type": "error",
                                "ename": type(e).__name__,
                                "evalue": str(e),
                                "traceback": traceback.format_exception(
                                    *sys.exc_info()
                                ),
                            }
                        ]
                    stop_execution = True
                finally:
                    # Send a copy of the cell without its source: the
                    # notebook cells must keep their source so that they
                    # can be re-executed later.
                    sent_cell = output_cell.copy()
                    sent_cell.pop("source", None)
                    await self.write_message(
                        {
                            "action": "execution_result",
                            "payload": {
                                "output_cell": sent_cell,
                                "cell_index": cell_idx,
                                "total_cell": total_cell,
                            },
                        }
                    )
                if stop_execution:
                    break

        def on_close(self) -> None:
            self._execution_data.pop(self._kernel_id, None)
            if self._executor and self._executor.kc:
                asyncio.create_task(ensure_async(self._executor.kc.stop_channels()))

else:

    class ReactiveExecutionHandler:
        pass
