import asyncio
import json
import sys
import traceback
from typing import Awaitable, List, Tuple, Union

import nbformat
from jupyter_core.utils import ensure_async
from jupyter_server.base.handlers import JupyterHandler
from nbclient.exceptions import CellExecutionError
from nbformat.v4 import new_code_cell
from tornado.web import HTTPError
from tornado.websocket import WebSocketHandler

from voila.execute import VoilaExecutor, strip_code_cell_warnings
from voila.utils import HaltExecution

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

        When a cell raises a kernel-side
        :class:`voila.utils.HaltExecution` exception, the execution is
        gracefully paused and a *Continue* button widget is displayed in the
        cell output instead of the stack trace. Any other kernel-side
        exception terminates the execution.
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
                executor.kc = await executor.async_start_new_kernel_client()
            return self._executor, nb

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

                except CellExecutionError as e:
                    self.log.exception(
                        "Error at server while executing cell: %r", input_cell
                    )
                    if e.ename == HaltExecution.__name__:
                        # Gracefully pause the execution: display a
                        # *Continue* button instead of the stack trace.
                        halt_error = HaltExecution.from_cell_execution_error(e)
                        output_cell = await self._continue_button_cell(
                            executor,
                            input_cell,
                            cell_idx,
                            halt_error.continue_button_label,
                        )
                    else:
                        if executor.should_strip_error():
                            strip_code_cell_warnings(input_cell)
                            executor.strip_code_cell_errors(input_cell)
                        output_cell = input_cell
                    # Do not run the following cells, either because the
                    # execution is paused (`HaltExecution`) or because a
                    # kernel-side exception terminated it.
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

        async def _continue_button_cell(
            self,
            executor: VoilaExecutor,
            input_cell: nbformat.NotebookNode,
            cell_index: int,
            continue_button_label: str,
        ) -> nbformat.NotebookNode:
            """Build the output cell for a halted cell: a *Continue* button
            widget is displayed instead of the exception stack trace.

            The button is a regular `ipywidgets` button, so it benefits from
            the default reactive re-execution mechanism: clicking it triggers
            the re-execution of the current cell and all the cells below.
            """
            button_cell = new_code_cell(
                source=(
                    "from ipywidgets.widgets.widget_button import Button\n"
                    "from IPython.display import display\n"
                    f"_voila_continue_button = Button(description={continue_button_label!r})\n"
                    "display(_voila_continue_button)"
                )
            )
            try:
                await executor.execute_cell(
                    button_cell, None, cell_index, store_history=False
                )
            except Exception:
                self.log.exception(
                    "Error at server while creating the continue button"
                )
                return input_cell
            output_cell = input_cell.copy()
            output_cell["outputs"] = button_cell.get("outputs", [])
            return output_cell

        def on_close(self) -> None:
            self._execution_data.pop(self._kernel_id, None)
            if self._executor and self._executor.kc:
                asyncio.create_task(ensure_async(self._executor.kc.stop_channels()))

else:

    class ReactiveExecutionHandler:
        pass
