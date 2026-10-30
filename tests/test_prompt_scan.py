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
