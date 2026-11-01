"""map-check: files a unit's process imports that no unit-map rule for that unit matches (the A04b gap)."""
import json

import pytest

import command_center as cc
import prompt_deploy as pd
import prompt_mapcheck as mc

ENTRY = {"app-channels": {"files": ["modules/listeners.py"], "roots": ["modules"]}}
UNIT_MAP = {
    "groups": {"web": ["app"]},
    "rules": [
        {"paths": ["modules/**"], "units": ["web"]},
        {"paths": ["modules/services/a.py", "modules/services/b.py", "modules/listeners.py"], "units": ["app-channels"]},
    ],
    "deploy": {"entry": ENTRY},
}

FILES = {
    # entry → a (top-level); a imports b lazily inside a function; b imports c: the map has a and b, not c
    "modules/listeners.py": "import os\nfrom services import a\n",
    "modules/services/__init__.py": "",
    "modules/services/a.py": "def reply():\n    from services.b import go\n    return go()\n",
    "modules/services/b.py": "import json\nfrom . import c\n",
    "modules/services/c.py": "import requests\n",
    "modules/services/unused.py": "",
    "modules/tests/test_x.py": "",
}


def tree(root, files=FILES, umap=UNIT_MAP):
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    (root / "scripts").mkdir(exist_ok=True)
    (root / "scripts/deploy_units.json").write_text(json.dumps(umap))
    return root


@pytest.fixture
def proj(tmp_path, monkeypatch):
    r = tree(tmp_path / "proj")
    (r / "docs/Prompts/agents").mkdir(parents=True)
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    monkeypatch.setenv("SESSION_TRACKER_DIR", str(tracker))
    c = {"prompts": {"repo": str(r), "root": "docs/Prompts", "unit_map": "scripts/deploy_units.json"}}
    (tmp_path / "config.json").write_text(json.dumps(c))
    monkeypatch.setenv("SESSION_TRACKER_CONFIG", str(tmp_path / "config.json"))
    return r


def run(*args):
    return cc.main(["--now", "2026-11-01T12:00:00", *args])


def load(r):
    return pd.load_map({"repo": str(r), "unit_map": "scripts/deploy_units.json"})


def test_lazy_import_chain_reaches_an_unmapped_file(proj):
    got = mc.check(proj, load(proj))["app-channels"]
    assert [g["file"] for g in got["gaps"]] == ["modules/services/__init__.py", "modules/services/c.py"]
    c = next(g for g in got["gaps"] if g["file"] == "modules/services/c.py")
    assert c["chain"] == ["modules/listeners.py", "modules/services/a.py", "modules/services/b.py", "modules/services/c.py"]
    assert got["reached"] == 5 and got["missing"] == []


def test_mapped_tree_has_no_gaps_and_never_reaches_unused_or_tests(proj):
    umap = json.loads((proj / "scripts/deploy_units.json").read_text())
    umap["rules"][1]["paths"] += ["modules/services/c.py", "modules/services/__init__.py"]
    (proj / "scripts/deploy_units.json").write_text(json.dumps(umap))
    got = mc.check(proj, load(proj))["app-channels"]
    assert got["gaps"] == []
    reached = mc.Walker(proj).walk(["modules/listeners.py"], ["modules"])
    assert "modules/services/unused.py" not in reached and not any("tests/" in f for f in reached)


def test_submodule_relative_and_import_module_forms(tmp_path):
    r = tree(tmp_path / "p", files={
        "src/main.py": "import importlib\nfrom pkg import sub\nimportlib.import_module('pkg.dyn')\n",
        "src/pkg/__init__.py": "",
        "src/pkg/sub.py": "from .deep import leaf\n",
        "src/pkg/dyn.py": "",
        "src/pkg/deep/__init__.py": "",
        "src/pkg/deep/leaf.py": "from ..sub import x\nimport os.path\n",
    })
    reached = mc.Walker(r).walk(["src/main.py"], ["src"])
    assert set(reached) == {"src/main.py", "src/pkg/__init__.py", "src/pkg/sub.py", "src/pkg/dyn.py",
                            "src/pkg/deep/__init__.py", "src/pkg/deep/leaf.py"}
    assert mc.chain(reached, "src/pkg/deep/leaf.py") == ["src/main.py", "src/pkg/sub.py", "src/pkg/deep/leaf.py"]


def test_cli_exit_codes_and_json(proj, capsys):
    assert run("map-check") == 1
    out = capsys.readouterr().out
    assert "modules/services/c.py" in out
    assert "modules/listeners.py → modules/services/a.py → modules/services/b.py → modules/services/c.py" in out
    assert run("map-check", "--json") == 1
    data = json.loads(capsys.readouterr().out)
    assert [g["file"] for g in data["units"]["app-channels"]["gaps"]][-1] == "modules/services/c.py"
    assert run("map-check", "--unit", "nope") == 2
    capsys.readouterr()
    umap = json.loads((proj / "scripts/deploy_units.json").read_text())
    umap["rules"][1]["paths"] = ["modules/**"]
    (proj / "scripts/deploy_units.json").write_text(json.dumps(umap))
    assert run("map-check") == 0
    assert "no gaps" in capsys.readouterr().out


def test_cli_no_entry_block_or_unreadable_map(proj, capsys):
    umap = json.loads((proj / "scripts/deploy_units.json").read_text())
    del umap["deploy"]
    (proj / "scripts/deploy_units.json").write_text(json.dumps(umap))
    assert run("map-check") == 2
    (proj / "scripts/deploy_units.json").write_text("{not json")
    assert run("map-check") == 2


def test_map_override_flag(proj, tmp_path, capsys):
    umap = json.loads((proj / "scripts/deploy_units.json").read_text())
    del umap["deploy"]
    (proj / "scripts/deploy_units.json").write_text(json.dumps(umap))
    alt = tmp_path / "alt.json"
    alt.write_text(json.dumps(UNIT_MAP))
    assert run("map-check", "--map", str(alt)) == 1
    assert "modules/services/c.py" in capsys.readouterr().out


def test_audit_names_the_gap(proj, capsys):
    assert run("audit") == 1
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if ln.startswith("map-check:"))
    assert line == ("map-check: app-channels reaches 2 file(s) no rule maps: "
                    "modules/services/__init__.py, modules/services/c.py")


def test_missing_entry_file_is_reported(proj, capsys):
    (proj / "modules/listeners.py").unlink()
    assert run("map-check") == 1
    assert "entry file missing" in capsys.readouterr().out


def test_statement_forms_the_line_scanner_must_cut(tmp_path):
    r = tree(tmp_path / "p", files={
        "src/main.py": ("import os; import one  # (comment\n"
                        "def f():\n    from pkg import (  # why\n        two,\n        three,\n    )\n"
                        "    from pkg.four import \\\n        x\n"
                        "x = 'from pkg import nothing'\n"),
        "src/one.py": "", "src/pkg/__init__.py": "", "src/pkg/two.py": "", "src/pkg/three.py": "",
        "src/pkg/four.py": "", "src/pkg/nothing.py": "",
    })
    reached = mc.Walker(r).walk(["src/main.py"], ["src"])
    assert set(reached) == {"src/main.py", "src/one.py", "src/pkg/__init__.py", "src/pkg/two.py",
                            "src/pkg/three.py", "src/pkg/four.py"}


def test_an_about_note_in_the_entry_block_is_not_a_unit(proj):
    """Every block of deploy_units.json carries an "about" string; map-check must not read it as a unit."""
    umap = load(proj)
    umap["deploy"]["entry"] = {"about": "what this block is", **umap["deploy"]["entry"]}
    assert list(mc.check(proj, umap)) == ["app-channels"]
