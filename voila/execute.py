#############################################################################
# Copyright (c) 2018, Voilà Contributors                                    #
# Copyright (c) 2018, QuantStack                                            #
#                                                                           #
# Distributed under the terms of the BSD 3-Clause License.                  #
#                                                                           #
# The full license is in the file LICENSE, distributed with this software.  #
#############################################################################

from typing import Any, Callable, Dict, Optional

from nbclient.client import NotebookClient
from nbclient.exceptions import CellExecutionError
from nbconvert.preprocessors.clearoutput import ClearOutputPreprocessor
from traitlets import Bool, Unicode, Integer

#: MIME type of the output displaying a Jupyter widget view.
WIDGET_VIEW_MIMETYPE = "application/vnd.jupyter.widget-view+json"

#: IOPub message types whose content is an output of the executed cell.
OUTPUT_MESSAGE_TYPES = ("execute_result", "display_data", "stream", "error")


def strip_code_cell_warnings(cell):
    """Strip any warning outputs and traceback from a code cell."""
    if cell["cell_type"] != "code":
        return cell

    outputs = cell["outputs"]

    cell["outputs"] = [
        output
        for output in outputs
        if output["output_type"] != "stream" or output["name"] != "stderr"
    ]

    return cell


class VoilaExecutor(NotebookClient):
    """Execute, but respect the output widget behaviour"""

    #: Called with ``(cell_index, output)`` for every output of a cell as soon
    #: as the kernel produces it, where ``output`` is the content of the IOPub
    #: message extended with its ``output_type``. It is used to stream the
    #: outputs of a still running cell to the client, streaming is disabled
    #: when it is ``None``.
    output_callback: Optional[Callable[[int, Dict[str, Any]], None]] = None

    cell_error_instruction = Unicode(
        "Please run Voilà with --show_tracebacks=True or --debug to see the error message, or configure VoilaConfiguration.show_tracebacks.",
        config=True,
        help=("instruction given to user to debug cell errors"),
    )

    cell_timeout_instruction = Unicode(
        "Please run Voilà with --VoilaExecutor.interrupt_on_timeout=True to continue executing the rest of the notebook.",
        config=True,
        help=("instruction given to user to continue execution on timeout"),
    )

    show_tracebacks = Bool(
        False,
        config=True,
        help=("Whether to send tracebacks to clients on exceptions."),
    )

    startup_timeout: int = Integer(
        60,
        config=True,
        help=(
            """
            The time to wait (in seconds) for the kernel to start.
            If kernel startup takes longer, a RuntimeError is
            raised.
            """
        ),
    )

    def execute(self, nb, resources, km=None):
        try:
            result = super().execute()
        except CellExecutionError as e:
            self.log.error(e)
            result = (nb, resources)

        # Strip errors and traceback if not in debug mode
        if self.should_strip_error():
            self.strip_notebook_errors(nb)

        return result

    async def execute_cell(self, cell, resources, cell_index, store_history=True):
        try:
            result = await self.async_execute_cell(cell, cell_index, store_history)
        except TimeoutError as e:
            self.log.error(e)
            self.show_code_cell_timeout(cell)
            raise e

        # Strip errors and traceback if not in debug mode
        if self.should_strip_error():
            strip_code_cell_warnings(cell)
            self.strip_code_cell_errors(cell)

        return result

    def should_strip_error(self):
        """Return True if errors should be stripped from the Notebook, False otherwise, depending on the current config."""
        return not self.show_tracebacks

    def process_message(self, msg, cell, cell_index):
        """Process an IOPub message, streaming its output to the client when
        an output callback is set."""
        output = super().process_message(msg, cell, cell_index)
        if self.output_callback is not None:
            streamed_output = self.streamed_output(msg, output)
            if streamed_output is not None:
                self.output_callback(cell_index, streamed_output)
        return output

    def streamed_output(self, msg, output):
        """Return the output to stream to the client for an IOPub message, or
        ``None`` if this message produces no output to stream.

        The outputs which are not part of the final cell outputs are not
        streamed either, so that streaming a running cell does not display
        what the final rendering of the cell hides:

        - the outputs captured by an ``ipywidgets.Output`` widget (``output``
          is ``None`` for those) are sent to the client over the widget comm
          channel instead,
        - widget views are rendered from the final cell outputs, so that a
          single view is created for each widget model,
        - stderr streams and tracebacks are stripped from the final cell
          outputs unless ``show_tracebacks`` is set.
        """
        msg_type = msg["msg_type"]
        if msg_type == "clear_output":
            return {
                "output_type": "clear_output",
                "wait": bool(msg["content"].get("wait")),
            }
        if msg_type == "update_display_data":
            return {**msg["content"], "output_type": "update_display_data"}
        if output is None or msg_type not in OUTPUT_MESSAGE_TYPES:
            return None
        if WIDGET_VIEW_MIMETYPE in output.get("data", {}):
            return None
        if self.should_strip_error() and (
            msg_type == "error"
            or (msg_type == "stream" and msg["content"].get("name") == "stderr")
        ):
            return None
        return {**msg["content"], "output_type": msg_type}

    def strip_notebook_errors(self, nb):
        """Strip error messages and traceback from a Notebook."""
        cells = nb["cells"]

        code_cells = [cell for cell in cells if cell["cell_type"] == "code"]

        for cell in code_cells:
            strip_code_cell_warnings(cell)
            self.strip_code_cell_errors(cell)

        return nb

    def strip_code_cell_errors(self, cell):
        """Strip any error outputs and traceback from a code cell."""
        # There is no 'outputs' key for markdown cells
        if cell["cell_type"] != "code":
            return cell

        outputs = cell["outputs"]

        error_outputs = [
            output for output in outputs if output["output_type"] == "error"
        ]

        error_message = "There was an error when executing cell [{}]. {}".format(
            cell["execution_count"], self.cell_error_instruction
        )

        for output in error_outputs:
            output["ename"] = "ExecutionError"
            output["evalue"] = "Execution error"
            output["traceback"] = [error_message]

        return cell

    def show_code_cell_timeout(self, cell):
        """Show a timeout error output in a code cell."""

        timeout_message = f"Cell execution timed out, aborting notebook execution. {self.cell_timeout_instruction}"

        output = {
            "output_type": "error",
            "ename": "TimeoutError",
            "evalue": "Timeout error",
            "traceback": [timeout_message],
        }

        cell["outputs"] = [output]


def executenb(nb, cwd=None, km=None, **kwargs):
    resources = {}
    if cwd is not None:
        resources["metadata"] = {"path": cwd}  # pragma: no cover
    # Clear any stale output, in case of exception
    nb, resources = ClearOutputPreprocessor().preprocess(nb, resources)
    executor = VoilaExecutor(nb, km=km, **kwargs)
    return executor.execute(nb, resources, km=km)
