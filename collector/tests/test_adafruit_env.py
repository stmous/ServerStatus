import os

from serverroom_monitor.sensors import adafruit


def test_blinka_dependency_prompt_disabled() -> None:
    # Importing the driver module must disable Blinka's interactive
    # dependency installer before `import board` can ever run.
    assert adafruit.__name__
    assert os.environ.get("BLINKA_SKIP_DEPENDENCY_CHECK") == "1"
