import prompt_center as pc
from conftest import cfg


def by_name(prompts):
    return {p.path.name: p for p in prompts}


def test_scan_lists_module_prompts_and_skips_indexes(proj):
    names = sorted(by_name(pc.scan(cfg(proj))))
    assert "Funnels-00-README.md" not in names
    assert "README.md" not in names and "2026-10-26-00-README.md" not in names
    assert "Security-Audit-00-README.md" not in names
    assert "Funnels-B12-3-Step-Analytics-CSV.md" in names and "F3-Planner-Cap.md" in names


def test_module_and_id_inference(proj):
    got = {name: (p.module, p.id, p.legacy) for name, p in by_name(pc.scan(cfg(proj))).items()}
    assert got["Funnels-B12-3-Step-Analytics-CSV.md"] == ("funnels", "B12-3", False)
    assert got["Funnels-B4-Publish-Wiring-Guarantee-Sent - 10-28.md"] == ("funnels", "B4", False)
    assert got["01-Sock-PDF-Security-Land.md"] == ("security", "01", True)
    assert got["F3-Planner-Cap.md"] == ("engine", "F3", True)
    assert got["Comms-3-Dialer-Walk.md"] == ("comms", "3", True)
    assert got["Websites-List-Server-Time.md"] == ("websites", None, True)
    assert got["Uddd2-05-Booking-Add-Step.md"] == ("misc", "05", True)
    assert got["02-Org-Roles-P2-Finish.md"] == ("org-roles", "02", True)


def test_status_title_and_order(proj):
    p = by_name(pc.scan(cfg(proj)))["Funnels-B12-3-Step-Analytics-CSV.md"]
    assert (p.status, p.order, p.title, p.has_frontmatter) == ("on-main", "9c", "per-step conversions", True)
    bare = by_name(pc.scan(cfg(proj)))["History-H8b-Paid-Paths-Without-Rows.md"]
    assert (bare.status, bare.order, bare.has_frontmatter) == ("pending", "H8b", False)


def test_duplicate_name_prefers_the_root_copy(proj):
    p = by_name(pc.scan(cfg(proj)))["History-H8b-Paid-Paths-Without-Rows.md"]
    assert p.rel == "docs/Prompts/history/History-H8b-Paid-Paths-Without-Rows.md" and not p.legacy


def test_symlinks_are_skipped(proj):
    (proj / "docs/Prompts/history/Link.md").symlink_to(proj / "docs/Prompts/history/History-H8b-Paid-Paths-Without-Rows.md")
    assert "Link.md" not in by_name(pc.scan(cfg(proj)))


def test_scan_without_prompts_config_is_empty(proj):
    assert pc.scan({}) == []


def test_cased_folder_twin_collapses_onto_the_frontmatter_copy(proj):
    # the same prompt committed twice: `funnels/<name>.md` (annotated) and `Funnels/<name>-10-28.md` (a bare copy)
    (proj / "docs/Prompts/funnels/Funnels-B6-Builder-UX-Standard.md").write_text("---\nstatus: deployed\n---\n# B6\n")
    cased = proj / "docs/Prompts/Funnels"
    cased.mkdir()
    (cased / "Funnels-B6-Builder-UX-Standard-10-28.md").write_text("# B6\n")
    (cased / "Funnels-B4-Publish-Wiring-Guarantee-Sent - 10-28.md").write_text("# B4 publish wiring\n")
    got = [p for p in pc.scan(cfg(proj)) if p.id in ("B6", "B4")]
    assert sorted((p.module, p.id, p.status) for p in got) == [("funnels", "B4", "pending"), ("funnels", "B6", "deployed")]
    assert all(p.rel.startswith("docs/Prompts/funnels/") for p in got)


def test_folder_module_is_case_insensitive_and_aliased(proj):
    (proj / "docs/Prompts/Organization").mkdir()
    (proj / "docs/Prompts/Organization/08-Org-Roles-P1-Leftovers.md").write_text("# P1\n")
    (proj / "docs/Prompts/Agents").mkdir()
    (proj / "docs/Prompts/Agents/Agents-A03-Org-Scope.md").write_text("# A03\n")
    c = cfg(proj)
    c["prompts"]["module_aliases"]["Organization"] = "org-roles"
    got = {p.path.name: p.module for p in pc.scan(c)}
    assert got["08-Org-Roles-P1-Leftovers.md"] == "org-roles" and got["Agents-A03-Org-Scope.md"] == "agents"


def test_subfolder_prompts_belong_to_the_module(proj):
    done = proj / "docs/Prompts/funnels/Completed"
    done.mkdir()
    (done / "Funnels-B1-Old.md").write_text("# B1\n")
    (proj / "docs/Prompts/funnels/_drafts").mkdir()
    (proj / "docs/Prompts/funnels/_drafts/Funnels-B99-Draft.md").write_text("# B99\n")
    got = {p.path.name: p.module for p in pc.scan(cfg(proj))}
    assert got["Funnels-B1-Old.md"] == "funnels" and "Funnels-B99-Draft.md" not in got


def test_distinct_prompts_sharing_an_id_are_both_kept(proj):
    for name in ("Comms-4-Consent-Followups-10-28.md", "Comms-4-Verify-Header-Fix-10-28.md"):
        (proj / "docs/Prompts/funnels" / name).write_text("# comms 4\n")
    names = by_name(pc.scan(cfg(proj)))
    assert "Comms-4-Consent-Followups-10-28.md" in names and "Comms-4-Verify-Header-Fix-10-28.md" in names
