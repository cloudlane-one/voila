import os
import voila.app
import pytest


@pytest.fixture
def reactive_rendering_mode():
    return True


@pytest.fixture
def voila_notebook(notebook_directory):
    return os.path.join(notebook_directory, "preheat", "pre_heat.ipynb")


class VoilaTest(voila.app.Voila):
    def listen(self):
        pass


def start_voila(
    voila_args, voila_config, preheat_config, progressive_rendering_config, reactive_rendering_config
):
    voila_app = VoilaTest.instance()
    try:
        voila_app.initialize(
            [
                *voila_args,
                "--no-browser",
                preheat_config,
                progressive_rendering_config,
                reactive_rendering_config,
            ]
        )
        voila_config(voila_app)
        voila_app.start()
    finally:
        VoilaTest.clear_instance()


def test_reactive_and_progressive_are_mutually_exclusive(
    voila_args, voila_config, preheat_config, progressive_rendering_config, reactive_rendering_config
):
    progressive_rendering_config = "--progressive_rendering=True"
    with pytest.raises(Exception) as e_info:
        start_voila(
            voila_args,
            voila_config,
            preheat_config,
            progressive_rendering_config,
            reactive_rendering_config,
        )
    assert (
        str(e_info.value)
        == "`progressive_rendering` and `reactive_rendering` are mutually exclusive"
    )


def test_reactive_and_preheat_are_incompatible(
    voila_args, voila_config, preheat_config, progressive_rendering_config, reactive_rendering_config
):
    preheat_config = "--preheat_kernel=True"
    with pytest.raises(Exception) as e_info:
        start_voila(
            voila_args,
            voila_config,
            preheat_config,
            progressive_rendering_config,
            reactive_rendering_config,
        )
    assert str(e_info.value) == "`preheat_kernel` and `reactive_rendering` are incompatible"
