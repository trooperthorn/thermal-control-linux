from pathlib import Path

import pytest

from thermalctl.hwmon import HwmonError, resolve_ref


def chip(root, directory, name, files=("pwm2",)):
    d = Path(root) / directory
    d.mkdir(parents=True)
    (d / "name").write_text(name + "\n", encoding="ascii", newline="\n")
    for f in files:
        (d / f).write_text("0\n", encoding="ascii", newline="\n")


def test_chip_and_channel_resolve_to_the_current_directory(tmp_path):
    chip(tmp_path, "hwmon0", "acpitz", files=())
    chip(tmp_path, "hwmon5", "nct6779")
    assert resolve_ref("nct6779:pwm2", tmp_path) == (tmp_path / "hwmon5" / "pwm2").as_posix()


def test_index_move_changes_the_result_not_the_reference(tmp_path):
    first, second = tmp_path / "boot1", tmp_path / "boot2"
    chip(first, "hwmon1", "nct6779")
    chip(second, "hwmon3", "nct6779")
    assert resolve_ref("nct6779:pwm2", first).endswith("hwmon1/pwm2")
    assert resolve_ref("nct6779:pwm2", second).endswith("hwmon3/pwm2")


def test_plain_paths_pass_through(tmp_path):
    assert resolve_ref("/sys/class/hwmon/hwmon1/pwm1", tmp_path) == "/sys/class/hwmon/hwmon1/pwm1"


def test_missing_chip_raises(tmp_path):
    chip(tmp_path, "hwmon0", "asus")
    with pytest.raises(HwmonError, match="nct6779"):
        resolve_ref("nct6779:pwm2", tmp_path)


def test_missing_root_raises(tmp_path):
    with pytest.raises(HwmonError):
        resolve_ref("nct6779:pwm2", tmp_path / "absent")


def test_ambiguous_chip_raises(tmp_path):
    chip(tmp_path, "hwmon0", "nct6779")
    chip(tmp_path, "hwmon1", "nct6779")
    with pytest.raises(HwmonError, match="refusing to guess"):
        resolve_ref("nct6779:pwm2", tmp_path)


def test_missing_channel_raises(tmp_path):
    chip(tmp_path, "hwmon0", "nct6779", files=("pwm1",))
    with pytest.raises(HwmonError):
        resolve_ref("nct6779:pwm2", tmp_path)
