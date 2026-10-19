from wifit3.ui import system_notifications


def test_linux_notification_uses_notify_send_without_blocking(mocker, monkeypatch):
    monkeypatch.setattr(system_notifications.sys, "platform", "linux")
    mocker.patch.object(system_notifications.shutil, "which", return_value="/usr/bin/notify-send")
    popen = mocker.patch.object(system_notifications.subprocess, "Popen")

    assert system_notifications.send_system_notification("Captured", "HomeNet") is True

    command = popen.call_args.args[0]
    assert command == [
        "/usr/bin/notify-send", "--app-name=wifit3", "--icon=network-wireless",
        "wifit3", "Captured\nHomeNet",
    ]
    assert popen.call_args.kwargs["start_new_session"] is True


def test_windows_notification_escapes_xml(mocker, monkeypatch):
    monkeypatch.setattr(system_notifications.sys, "platform", "win32")
    mocker.patch.object(system_notifications.shutil, "which", return_value="powershell.exe")
    popen = mocker.patch.object(system_notifications.subprocess, "Popen")

    assert system_notifications.send_system_notification("A&B", "<captured>") is True

    command = popen.call_args.args[0]
    assert command[:4] == ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"]
    assert "<text>wifit3</text>" in command[4]
    assert "A&amp;B\n&lt;captured&gt;" in command[4]
    assert popen.call_args.kwargs["start_new_session"] is False


def test_notification_returns_false_without_platform_backend(mocker, monkeypatch):
    monkeypatch.setattr(system_notifications.sys, "platform", "linux")
    mocker.patch.object(system_notifications.shutil, "which", return_value=None)
    popen = mocker.patch.object(system_notifications.subprocess, "Popen")

    assert system_notifications.send_system_notification("Title", "Body") is False
    popen.assert_not_called()
