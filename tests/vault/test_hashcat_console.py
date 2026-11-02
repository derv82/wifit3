import pytest

hashcat_console = pytest.importorskip("wifit3.vault.tools.hashcat_console",
                                      reason="a console input buffer is Windows-only")


def test_launch_prints_the_pid_of_the_process_it_started(mocker, capsys):
    launch = mocker.patch.object(hashcat_console, "launch", return_value=4242)

    code = hashcat_console.main(["launch", "C:/hashcat", "C:/job.log",
                                 "hashcat.exe", "-m", "22000"])

    assert (code, capsys.readouterr().out.strip()) == (0, "4242")
    assert launch.call_args.args == ("C:/hashcat", "C:/job.log",
                                     ["hashcat.exe", "-m", "22000"])


def test_keypress_reaches_the_requested_pid_as_a_number(mocker):
    keypress = mocker.patch.object(hashcat_console, "keypress")

    assert hashcat_console.main(["keypress", "4242", "q"]) == 0
    assert keypress.call_args.args == (4242, "q")


def test_an_incomplete_command_line_is_refused(capsys):
    assert hashcat_console.main(["keypress", "4242"]) == 2
    assert hashcat_console.main(["launch", "C:/hashcat"]) == 2
    assert hashcat_console.main([]) == 2
    assert "usage" in capsys.readouterr().err
