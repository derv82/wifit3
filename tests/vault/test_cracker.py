from wifit3.vault.tools.cracker import WordlistQueue, expand_wordlists


# ----- wordlist expansion ---------------------------------------------------

def test_expand_wordlists_lists_the_files_inside_a_directory(tmp_path):
    directory = tmp_path / "dictionary folder"
    directory.mkdir()
    (directory / "second.txt").write_text("two\n")
    (directory / "first.txt").write_text("one\n")
    (directory / "nested").mkdir()

    assert expand_wordlists([str(directory)]) == [
        str((directory / "first.txt").resolve()), str((directory / "second.txt").resolve())]


def test_expand_wordlists_keeps_the_requested_order_of_files(tmp_path):
    second, first = tmp_path / "z.txt", tmp_path / "a.txt"
    second.write_text("two\n")
    first.write_text("one\n")

    assert expand_wordlists([str(second), str(first)]) == [str(second.resolve()),
                                                           str(first.resolve())]


def test_expand_wordlists_drops_what_does_not_exist(tmp_path):
    assert expand_wordlists([str(tmp_path / "missing.txt")]) == []


def test_queue_label_is_empty_without_wordlists():
    assert WordlistQueue().label == ""
