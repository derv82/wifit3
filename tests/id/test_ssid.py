import pytest

from wifit3.id.ssid import strip_suffix


@pytest.mark.parametrize("ssid, expected", [
    ("MyRouter 2.4", "MyRouter"),
    ("MyRouter-2.4G", "MyRouter"),
    ("MyRouter_5GHz", "MyRouter"),
    ("MyRouter-6g", "MyRouter"),
    ("MyRouter-Guest 5G", "MyRouter"),
    ("MyRouter-IoT", "MyRouter"),
    ("MyRouter", "MyRouter"),
    ("Lab5", "Lab5"),
    ("5G", "5G"),
])
def test_strip_suffix(ssid, expected):
    assert strip_suffix(ssid) == expected
