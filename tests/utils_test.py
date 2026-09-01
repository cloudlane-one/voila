from typing import Dict, List, Set
from voila.utils import HaltExecution, filter_extension


federated_extensions = [{"name": "foo"}, {"name": "bar"}, {"name": "foo-bar"}]


def list_to_set(inp: List[Dict]) -> Set:
    return set([x["name"] for x in inp])


def test_filter_extension():
    extensions = filter_extension(federated_extensions=federated_extensions)
    assert list_to_set(extensions) == list_to_set(federated_extensions)


def test_filter_extension_with_disabled():
    disabled_extensions = ["bar"]
    extensions = filter_extension(
        federated_extensions=federated_extensions,
        disabled_extensions=disabled_extensions,
    )
    assert list_to_set(extensions) == set(["foo", "foo-bar"])


def test_halt_execution_default_label():
    error = HaltExecution()
    assert error.continue_button_label == "Continue"
    assert str(error) == "Continue"


def test_halt_execution_custom_label():
    error = HaltExecution(continue_button_label="Load more results")
    assert error.continue_button_label == "Load more results"
    assert str(error) == "Load more results"


def test_halt_execution_from_cell_execution_error():
    class FakeCellExecutionError:
        ename = "HaltExecution"
        evalue = "Resume execution"

    error = HaltExecution.from_cell_execution_error(FakeCellExecutionError())
    assert error.continue_button_label == "Resume execution"


def test_halt_execution_from_cell_execution_error_default():
    class FakeCellExecutionError:
        ename = "HaltExecution"
        evalue = ""

    error = HaltExecution.from_cell_execution_error(FakeCellExecutionError())
    assert error.continue_button_label == "Continue"
