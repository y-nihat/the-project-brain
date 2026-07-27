"""Pure workflow mechanics: manifests, sharding, normalization, ordering."""
from __future__ import annotations

from brain import config, workflow
from brain.models import StageSpec


# --- manifests -------------------------------------------------------------- #
def test_parse_manifest_takes_last_valid_block():
    text = (
        "I found some things.\n"
        "```manifest\n[\"old\", \"stale\"]\n```\n"
        "Wait, the full list is:\n"
        "```manifest\n[\"a\", \"b\", {\"url\": \"http://x\"}]\n```\n"
    )
    assert workflow.parse_manifest(text) == ["a", "b", {"url": "http://x"}]


def test_parse_manifest_absent_or_broken():
    assert workflow.parse_manifest("no fences here") is None
    assert workflow.parse_manifest("```manifest\nnot json\n```") is None
    assert workflow.parse_manifest("") is None
    # An empty list is a valid, honest "nothing found" — distinct from None.
    assert workflow.parse_manifest("```manifest\n[]\n```") == []


def test_render_item():
    assert workflow.render_item("plain") == "plain"
    assert workflow.render_item({"u": "x"}) == '{"u": "x"}'


# --- sharding ---------------------------------------------------------------- #
def test_shard_even_groups():
    shards = workflow.shard(list(range(12)), 5, max_workers=12)
    assert [len(s) for s in shards] == [5, 5, 2]


def test_shard_widens_instead_of_exceeding_worker_cap():
    shards = workflow.shard(list(range(100)), 1, max_workers=4)
    assert len(shards) == 4
    assert sum(len(s) for s in shards) == 100


def test_shard_empty_and_degenerate():
    assert workflow.shard([], 5, 10) == []
    assert workflow.shard(["only"], 5, 10) == [["only"]]


# --- normalization ----------------------------------------------------------- #
def test_normalize_degrades_and_repairs():
    stages, warnings = workflow.normalize_stages([
        {"id": "gather", "pattern": "single", "role": "scout", "goal": "find items"},
        {"id": "work", "pattern": "map", "role": "analyst", "goal": "analyze",
         "depends_on": ["gather", "ghost"]},                      # dangling dep dropped
        {"id": "merge", "pattern": "reduce", "role": "editor", "goal": "merge"},  # no deps -> chained
        {"id": "merge", "pattern": "wibble", "role": "dup", "goal": "?"},         # dup id + bad pattern
    ])
    by_id = {s.id: s for s in stages}
    assert by_id["work"].depends_on == ["gather"]
    assert by_id["work"].shard_from == "gather"
    assert by_id["work"].shard_size == config.MAP_DEFAULT_SHARD_SIZE
    assert by_id["merge"].depends_on == ["work"]                  # chained to previous
    assert [s for s in stages if s.pattern == "single" and s.id != "gather"]  # degraded dup
    assert len({s.id for s in stages}) == len(stages)
    assert warnings


def test_normalize_map_with_nothing_upstream_degrades_to_single():
    stages, warnings = workflow.normalize_stages([
        {"id": "solo", "pattern": "map", "role": "r", "goal": "g"},
    ])
    assert stages[0].pattern == "single"
    assert warnings


def test_normalize_clamps_review_rounds(monkeypatch):
    monkeypatch.setattr(config, "REVIEW_MAX_ROUNDS", 5)
    stages, _ = workflow.normalize_stages([
        {"id": "a", "pattern": "single", "role": "r", "goal": "g"},
        {"id": "check", "pattern": "review", "role": "critic", "goal": "verify",
         "depends_on": ["a"], "rounds": 99},
    ])
    assert stages[1].rounds == config.REVIEW_MAX_ROUNDS


def test_normalize_review_rounds_default_allows_a_fix_cycle(monkeypatch):
    """Unspecified rounds on a review default to 2 — a 1-round review could
    only complain, never fix.

    The ceiling is raised first, because with REVIEW_MAX_ROUNDS at its default
    of 2 the floor forces every review to 2 anyway: the default could be
    deleted entirely and this test still passed, asserting the same number as
    the clamp test next to it.
    """
    monkeypatch.setattr(config, "REVIEW_MAX_ROUNDS", 5)
    stages, _ = workflow.normalize_stages([
        {"id": "a", "pattern": "single", "role": "r", "goal": "g"},
        {"id": "check", "pattern": "review", "role": "critic", "goal": "verify",
         "depends_on": ["a"]},
    ])
    assert stages[1].rounds == 2            # the DEFAULT, not the ceiling


# --- ordering / topology ------------------------------------------------------ #
def test_topo_order_and_feeds_a_map():
    stages, _ = workflow.normalize_stages([
        {"id": "merge", "pattern": "reduce", "role": "e", "goal": "g", "depends_on": ["work"]},
        {"id": "work", "pattern": "map", "role": "a", "goal": "g", "depends_on": ["gather"]},
        {"id": "gather", "pattern": "single", "role": "s", "goal": "g"},
    ])
    order = [s.id for s in workflow.topo_order(stages)]
    assert order == ["gather", "work", "merge"]
    assert workflow.feeds_a_map("gather", stages)
    assert not workflow.feeds_a_map("merge", stages)


def test_stage_id_is_forced_to_a_usable_filename_and_edges_follow():
    """A stage id becomes outputs/<id>.md. The planner emitted it, so it is not
    allowed to be path syntax: '../../VICTIM/evil' wrote outside the run
    directory, and under compose the repo is mounted read-write, so it landed
    on the host. Renaming must carry the DAG with it — slugging one id while
    leaving a dependant pointing at the old one severs the edge silently."""
    stages, warnings = workflow.normalize_stages([
        {"id": "../../VICTIM/evil", "pattern": "single", "role": "r", "goal": "g"},
        {"id": "next", "pattern": "reduce", "role": "r", "goal": "g",
         "depends_on": ["../../VICTIM/evil"]},
    ])
    assert stages[0].id == "VICTIM-evil"
    assert "/" not in stages[0].id and ".." not in stages[0].id
    assert stages[1].depends_on == ["VICTIM-evil"]      # edge survived the rename
    assert any("not a usable name" in w for w in warnings)


def test_stage_id_slug_falls_back_when_nothing_usable_remains():
    stages, warnings = workflow.normalize_stages([
        {"id": "../..", "pattern": "single", "role": "r", "goal": "g"},
        {"id": "/", "pattern": "single", "role": "r", "goal": "g"},
    ])
    assert [s.id for s in stages] == ["stage1", "stage2"]
    assert len(warnings) >= 2


def test_ordinary_stage_ids_are_left_alone():
    stages, warnings = workflow.normalize_stages([
        {"id": "collect_sources", "pattern": "single", "role": "r", "goal": "g"},
        {"id": "stage-2.fix", "pattern": "single", "role": "r", "goal": "g"},
    ])
    assert [s.id for s in stages] == ["collect_sources", "stage-2.fix"]
    assert not [w for w in warnings if "usable name" in w]
