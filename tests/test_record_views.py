"""M1.4: current view and file history."""

from dataclasses import replace

from conftest import entry
from eridi.core.models import Link, Subject
from eridi.core.record.views import current, history, resolve

ROOT = "root_01JC8W0000000000000000000A"
PROTOCOL = Subject(ROOT, "Protocols/WB_v7.docx")
OTHER = Subject(ROOT, "Notes/other.md")


def test_plain_entries_are_current() -> None:
    a, b = entry(text="a"), entry(text="b")
    assert current([b, a]) == [a, b]


def test_supersede_chain_of_three() -> None:
    a = entry(text="v1", subjects=[PROTOCOL])
    b = entry(text="v2", subjects=[PROTOCOL], supersedes=a.id)
    c = entry(text="v3", subjects=[PROTOCOL], supersedes=b.id)
    assert current([a, b, c]) == [c]
    states = resolve([a, b, c])
    assert states[a.id].state == "superseded"
    assert states[a.id].superseded_by == b.id
    assert states[b.id].superseded_by == c.id
    assert states[c.id].state == "current"
    assert [(i.entry.text, i.state) for i in history([c, a, b], ROOT, PROTOCOL.path)] == [
        ("v1", "superseded"),
        ("v2", "superseded"),
        ("v3", "current"),
    ]


def test_newest_of_competing_corrections_wins() -> None:
    a = entry(text="original")
    b1 = entry(text="fix from laptop A", supersedes=a.id)
    b2 = entry(text="fix from laptop B", supersedes=a.id)
    assert current([a, b1, b2]) == [b2]
    assert resolve([a, b1, b2])[b1.id].superseded_by == b2.id


def test_tombstone_hides_target_and_itself() -> None:
    a = entry(text="mistake", subjects=[PROTOCOL])
    keep = entry(text="keep")
    t = entry("tombstone", "", target_id=a.id)
    assert current([a, keep, t]) == [keep]
    items = history([a, keep, t], ROOT, PROTOCOL.path)
    assert [(i.entry.id, i.state) for i in items] == [(a.id, "retracted"), (t.id, "current")]
    assert items[0].retracted_by == t.id


def test_tombstone_of_a_superseded_entry() -> None:
    a = entry(text="v1", subjects=[PROTOCOL])
    b = entry(text="v2", subjects=[PROTOCOL], supersedes=a.id)
    t = entry("tombstone", "", target_id=a.id)
    assert current([a, b, t]) == [b]
    items = {i.entry.id: i for i in history([a, b, t], ROOT, PROTOCOL.path)}
    assert items[a.id].state == "retracted"
    assert items[a.id].superseded_by == b.id
    assert items[b.id].state == "current"
    assert t.id in items


def test_retracting_a_correction_does_not_restore_the_original() -> None:
    a = entry(text="v1")
    b = entry(text="v2", supersedes=a.id)
    t = entry("tombstone", "", target_id=b.id)
    assert current([a, b, t]) == []


def test_retracting_a_tombstone_restores_the_entry() -> None:
    a = entry(text="v1")
    t1 = entry("tombstone", "", target_id=a.id)
    t2 = entry("tombstone", "", target_id=t1.id)
    assert current([a, t1, t2]) == [a]
    t3 = entry("tombstone", "", target_id=t2.id)
    assert current([a, t1, t2, t3]) == []


def test_tombstone_of_missing_id_is_harmless() -> None:
    a = entry(text="still here")
    t = entry("tombstone", "", target_id="01JC8Z9K3V5W6X7Y8Z9A0B1C2D")
    assert current([a, t]) == [a]
    assert resolve([a, t])[t.id].state == "current"


def test_references_to_newer_entries_are_ignored() -> None:
    # Hand-edited or broken data must not create cycles or hide entries.
    a = entry(text="a")
    b = entry(text="b", supersedes=a.id)
    weird = entry(text="c")
    forward = entry(text="d")
    a_cycle = replace(a, supersedes=b.id)
    assert current([a_cycle, b]) == [b]
    pointing_forward = replace(weird, supersedes=forward.id)
    assert current([pointing_forward, forward]) == [pointing_forward, forward]


def test_history_includes_outcomes_links_and_earlier_versions() -> None:
    note = entry(text="why", subjects=[PROTOCOL])
    outcome = entry("outcome", "", target_id=note.id, outcome="worked")
    other_note = entry(text="see the protocol")
    link = entry(
        "link",
        "",
        links=[
            Link(
                from_=other_note.id, to={"root_id": ROOT, "path": PROTOCOL.path}, relation="relates"
            )
        ],
    )
    unrelated = entry(text="other file", subjects=[OTHER])
    earlier = entry(text="earlier version without subject")
    fix = entry(text="fixed", subjects=[PROTOCOL], supersedes=earlier.id)
    got = [
        i.entry.id
        for i in history([note, outcome, link, unrelated, earlier, fix], ROOT, PROTOCOL.path)
    ]
    assert got == sorted([note.id, outcome.id, link.id, earlier.id, fix.id])


def test_unknown_kinds_stay_in_view() -> None:
    future = replace(entry(text="x"), kind="experiment_run")
    assert current([future]) == [future]
