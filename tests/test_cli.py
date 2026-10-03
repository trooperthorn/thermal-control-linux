from thermalctl import __version__
from thermalctl.__main__ import main


def test_version(capsys):
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == __version__
