import json

import pytest

import prompt_center as pc
from conftest import cfg


def index(proj):
    return pc.scan(cfg(proj))


def test_resolve_full_path(proj):
    text = "Run `docs/Prompts/funnels/Funnels-B12-3-Step-Analytics-CSV.md` please"
    assert pc.resolve_prompt(text, cfg(proj), index(proj)) == "docs/Prompts/funnels/Funnels-B12-3-Step-Analytics-CSV.md"


def test_resolve_absolute_path_under_repo(proj):
    text = f"Run {proj}/docs/Prompts/history/History-H8b-Paid-Paths-Without-Rows.md"
    assert pc.resolve_prompt(text, cfg(proj), index(proj)) == "docs/Prompts/history/History-H8b-Paid-Paths-Without-Rows.md"


def test_resolve_old_path_through_moved_map(proj):
    (proj / "docs/Prompts/agents").mkdir()
    (proj / "docs/Prompts/agents/Agents-A07-Agent-Home.md").write_text("# A07\n")
    (proj / "docs/Prompts/_moved.json").write_text(json.dumps(
        {"docs/Old-2026-10-29/Agents-A07-Agent-Home.md": "docs/Prompts/agents/Agents-A07-Agent-Home.md"}))
    text = "Run docs/Old-2026-10-29/Agents-A07-Agent-Home.md"
    assert pc.resolve_prompt(text, cfg(proj), index(proj)) == "docs/Prompts/agents/Agents-A07-Agent-Home.md"


def test_resolve_bare_file_name_with_spaces(proj):
    text = "run and execute from /docs Funnels-B4-Publish-Wiring-Guarantee-Sent - 10-28.md now"
    assert pc.resolve_prompt(text, cfg(proj), index(proj)).endswith("/Funnels-B4-Publish-Wiring-Guarantee-Sent - 10-28.md")


def test_resolve_prefers_the_longest_name(proj):
    (proj / "docs/Prompts/funnels/Funnels-B4.md").write_text("# short\n")
    text = "Run Funnels-B4-Publish-Wiring-Guarantee-Sent - 10-28.md"
    assert pc.resolve_prompt(text, cfg(proj), index(proj)).endswith("Guarantee-Sent - 10-28.md")


def test_resolve_unknown_md_is_none(proj):
    assert pc.resolve_prompt("Read docs/Other/Thing.md and fix it", cfg(proj), index(proj)) is None
    assert pc.resolve_prompt("no file here", cfg(proj), index(proj)) is None


def test_legacy_path_resolves_to_its_own_rel(proj):
    text = "Run docs/Old-2026-10-27/F3-Planner-Cap.md"
    assert pc.resolve_prompt(text, cfg(proj), index(proj)) == "docs/Old-2026-10-27/F3-Planner-Cap.md"


@pytest.mark.parametrize("text,claims", [
    ("Run `docs/Prompts/funnels/X.md`", True),
    ("run and execute from /docs X.md", True),
    ("Execute docs/Prompts/funnels/X.md", True),
    ("Start docs/Prompts/funnels/X.md", True),
    ("Please run docs/Prompts/funnels/X.md", True),
    ("Look at docs/Prompts/funnels/X.md and fix the typo", False),
    ("<command-name>/sessions</command-name> run X.md", False),
    # real first messages (CC3, #605)
    ("analyze and execute Documentation/Prompts/Security/Pending/Security-S4-Modules-Sweep.md", True),
    ("/opt/neurogenserver/Documentation/Prompts/ops/Ops-CC2-Command-Center-Follow-Ups.md", True),
    ("`Documentation/Prompts/Ops/Pending/Ops-CC3-Command-Center-Improvements.md`", True),
    ("  Ops-CC3-Command-Center-Improvements.md\n", True),
    ("run and execute from /Documentation Funnels-B12-4-Webinar-Honesty.md", True),
    ("Run plan and execute Connections-C6-Lead-Extractor-Billing.md", True),
    ("plan and execute docs/Prompts/funnels/X.md", True),
    ("look at Documentation/Prompts/History/Pending/History-H8d-Last-Paid-Paths.md", False),
    ("why are there 63 app files?", False),
    ("why is docs/Prompts/funnels/X.md stalled, did the run die", False),
    ("review docs/Prompts/funnels/X.md", False),
    ("do not run docs/Prompts/funnels/X.md yet", False),
    ("read the notes and the log first then run X.md", False),  # the verb is the 9th word
    ("docs/Prompts/funnels/X.md has a typo", False),
])
def test_claims_prompt(text, claims):
    assert pc.claims_prompt(text, ["run", "execute", "start"]) is claims
