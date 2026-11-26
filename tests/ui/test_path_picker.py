"""The file/directory picker (ui/path_picker.py): its path decisions."""
from pathlib import Path

import pytest

from wifit3.ui.path_picker import (
    _DirectoriesOnlyTree, _nearest_dir, picker_roots,
    relative_to_cwd,
)


@pytest.fixture
def tree_fixture(tmp_path):
    (tmp_path / "wordlists").mkdir()
    (tmp_path / "wordlists" / "rockyou.txt").write_text("password\n", encoding="utf-8")
    (tmp_path / "captures").mkdir()
    return tmp_path


# --- path logic (no app) -----------------------------------------------------

def test_relative_to_cwd_only_rewrites_paths_inside_the_tree(tmp_path):
    assert relative_to_cwd(Path.cwd() / "captures" / "loot.hc22000") == Path("captures/loot.hc22000")
    # a sibling tree must stay absolute rather than grow a chain of ".."
    assert relative_to_cwd(tmp_path / "elsewhere") == tmp_path / "elsewhere"


def test_nearest_dir_climbs_to_something_that_exists(tree_fixture):
    assert _nearest_dir(tree_fixture / "wordlists") == tree_fixture / "wordlists"
    assert _nearest_dir(tree_fixture / "wordlists" / "rockyou.txt") == tree_fixture / "wordlists"
    assert _nearest_dir(tree_fixture / "gone" / "deeper" / "still") == tree_fixture


def test_directory_mode_filters_files_out(tree_fixture):
    entries = [tree_fixture / "wordlists", tree_fixture / "wordlists" / "rockyou.txt"]
    kept = _DirectoriesOnlyTree.filter_paths(None, entries)     # never touches self
    assert [p.name for p in kept] == ["wordlists"]


def test_roots_are_real_directories_and_include_home():
    labelled = picker_roots()
    assert labelled, "the strip should never be empty"
    assert all(path.is_dir() for _, path in labelled)
    assert Path.home() in [path for _, path in labelled]


# --- in a running app --------------------------------------------------------


