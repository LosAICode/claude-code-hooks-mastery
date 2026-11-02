# ruff: noqa: F811  (the tests take fixtures imported by name)
"""`prompts pending|completed`: every prompt filed in that folder with its full path, and any prompt whose
status points at the other folder flagged (exit 1) so `sort` can move it."""
from conftest import commit_file
from test_prompt_status_folders import ROOT, run, tree  # noqa: F401  (tree: fixture)


def test_pending_lists_full_paths_of_pending_folder_and_unfiled_open_prompts(tree, capsys):
    assert run("prompts", "pending") == 1  # the fixture has unfiled prompts
    out = capsys.readouterr().out
    assert f"{tree / ROOT / 'Alpha/Pending/Alpha-A3-Third.md'}" in out
    assert f"{tree / ROOT / 'Alpha/Alpha-A1-First.md'}" in out and "not filed" in out
    assert "Alpha-A4-Bare.md" not in out and "Alpha-A2-Second.md" not in out


def test_completed_lists_closed_prompts_only(tree, capsys):
    run("prompts", "completed")
    out = capsys.readouterr().out
    assert f"{tree / ROOT / 'Alpha/Completed/Alpha-A4-Bare.md'}" in out
    assert "Alpha-A5-Skip.md" in out and "Alpha-A2-Second.md" in out
    assert "Alpha-A3-Third.md" not in out and "Alpha-A1-First.md" not in out


def test_a_done_prompt_left_in_pending_is_flagged(tree, capsys):
    run("sort", "--no-commit")
    capsys.readouterr()
    assert run("prompts", "pending") == 0
    capsys.readouterr()
    commit_file(tree, f"{ROOT}/Alpha/Pending/Alpha-A7-Late.md", "---\nstatus: done\norder: 7\n---\n# A7\n", "a7",
                when="2020-01-02T00:00:00")
    assert run("prompts", "pending") == 1
    out = capsys.readouterr().out
    assert "Alpha-A7-Late.md" in out and "belongs in Completed/" in out and "sort" in out


def test_module_and_status_narrow_the_list(tree, capsys):
    run("prompts", "pending", "--module", "comms")
    out = capsys.readouterr().out
    assert "Comms-C1-Dial.md" in out and "Alpha-" not in out
    run("prompts", "pending", "--status", "on-main")
    out = capsys.readouterr().out
    assert "Alpha-A3-Third.md" in out and "Alpha-A1-First.md" not in out


def test_an_unknown_module_is_an_error(tree, capsys):
    assert run("prompts", "pending", "--module", "nosuch") == 2
    assert "modules: " in capsys.readouterr().err
