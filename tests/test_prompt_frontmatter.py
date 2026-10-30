import os
import stat

import pytest

import prompt_center as pc


def test_parse_without_frontmatter_returns_body_unchanged():
    text = "# Title\n\nbody\n"
    meta, body, had = pc.parse_frontmatter(text)
    assert (meta, body, had) == ({}, text, False)


def test_parse_flat_keys_lists_and_quotes():
    text = '---\nmodule: funnels\nid: B12-3\ndepends_on: [B12-2, B11]\nnotes: "deploy: held"\nempty:\n---\n# T\n'
    meta, body, had = pc.parse_frontmatter(text)
    assert had is True
    assert meta == {"module": "funnels", "id": "B12-3", "depends_on": ["B12-2", "B11"], "notes": "deploy: held", "empty": ""}
    assert body == "# T\n"


def test_unclosed_frontmatter_is_not_frontmatter():
    text = "---\nmodule: x\n" + "line\n" * 70
    assert pc.parse_frontmatter(text) == ({}, text, False)


def test_body_may_contain_a_rule():
    text = "---\nstatus: pending\n---\nintro\n---\nmore\n"
    meta, body, _ = pc.parse_frontmatter(text)
    assert meta == {"status": "pending"} and body == "intro\n---\nmore\n"


def test_dump_orders_known_keys_first_and_keeps_unknown():
    meta = {"zzz": "1", "status": "done", "module": "agents", "notes": "a: b", "depends_on": ["A1"]}
    out = pc.dump_frontmatter(meta)
    assert out.splitlines() == ["---", "module: agents", "status: done", "depends_on: [A1]", 'notes: "a: b"', 'zzz: "1"', "---"]


def test_dump_quoting_agrees_with_yaml():
    yaml = pytest.importorskip("yaml")
    meta = {"notes": "x: y #z", "needs_owner": "[maybe]", "title": "it's fine", "commit": "0123456", "order": "123e4567", "cost": "no", "deployed": "2026-10-30T08:34:00-04:00"}
    parsed = yaml.safe_load(pc.dump_frontmatter(meta).strip().strip("-"))
    assert parsed == meta


def test_write_meta_keeps_body_bytes_and_mode(tmp_path):
    p = tmp_path / "P.md"
    body = b"# T\r\nno final newline\r\n---\r\nx"
    p.write_bytes(b"---\nstatus: pending\n---\n" + body)
    os.chmod(p, 0o640)
    new = pc.write_meta(p, {"status": "running", "session_id": "abc"})
    assert new == {"status": "running", "session_id": "abc"}
    assert p.read_bytes() == b"---\nstatus: running\nsession_id: abc\n---\n" + body
    assert stat.S_IMODE(p.stat().st_mode) == 0o640
    assert list(tmp_path.iterdir()) == [p]


def test_write_meta_adds_frontmatter_to_a_bare_file(tmp_path):
    p = tmp_path / "P.md"
    p.write_text("# T\n")
    pc.write_meta(p, {"status": "running"})
    assert p.read_text() == "---\nstatus: running\n---\n# T\n"


def test_write_meta_expect_mismatch_writes_nothing(tmp_path):
    p = tmp_path / "P.md"
    p.write_text("---\nstatus: done\n---\n# T\n")
    assert pc.write_meta(p, {"status": "running"}, expect={"status": ("pending", "blocked")}) is None
    assert p.read_text() == "---\nstatus: done\n---\n# T\n"


def test_write_meta_aborts_when_file_changed_underneath(tmp_path, monkeypatch):
    p = tmp_path / "P.md"
    p.write_text("---\nstatus: pending\n---\n# T\n")

    ticks = iter([1, 2])

    def changed(path):  # simulate another writer between read and replace
        p.write_text("---\nstatus: done\n---\n# T\n")
        return next(ticks)

    monkeypatch.setattr(pc, "_mtime_ns", changed)
    assert pc.write_meta(p, {"status": "running"}) is None
    assert "status: done" in p.read_text()
