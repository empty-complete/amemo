"""records.revert — отмена пакета изменений целиком (приём ветки omnigraph: работу агента выбрасывают разом)
и её вызов через api: только человек."""
import uuid

import pytest

from amemo import api, db, lookup, records
from amemo.ids import uuid7
from amemo.records import Item, commit

MODEL = "model:writer"
PERSON = "person:owner"
OBJ_ID = uuid7()
OBJ = f"obj:{OBJ_ID}"
DIGEST = "sha256:" + "cd" * 32


@pytest.fixture
def kb(pg):
    db.prepare(pg)
    pg.execute("INSERT INTO object (id, kind, original_name, created_at) VALUES (%s, 'file', 'catalogue.pdf', now())",
               (OBJ_ID,))
    pg.execute("INSERT INTO object_version (object_id, kind, n, digest, size, recorded_at) "
               "VALUES (%s, 'file', 1, %s, 0, now())", (OBJ_ID, DIGEST))
    return pg


def part() -> dict:
    return {"ref": "@part", "entity_class": "product", "label": {"ru": "Кронштейн К-1"}}


def fact(ref="@a", label="Кронштейн К-1 держит 40 кг", about="@part") -> dict:
    return {"ref": ref, "kind": "fact", "label": {"ru": label}, "summary_plain": {"ru": label}, "about": [about],
            "sources": [{"object": OBJ, "object_version": 1, "digest": DIGEST, "quote": "Таблица 2",
                         "selectors": [{"type": "page", "value": "2"}]}]}


def edit(label: str, about: str) -> dict:
    return {k: v for k, v in fact(label=label, about=about).items() if k != "ref"}


def status(kb, ref: str) -> str:
    target = "atom" if ref.startswith("atm:") else "entity"
    return kb.execute(f"SELECT status FROM {target} WHERE id = %s", (ref.split(":", 1)[1],)).fetchone()[0]


def test_revert_retires_what_the_batch_created_and_its_links(kb):
    made = commit(kb, [Item("entity", part()), Item("atom", fact()), Item("atom", fact("@b", "Кронштейн К-1 на анкере"))],
                  [{"rel": "depends_on", "src": "@b", "dst": "@a"}], agent=MODEL)
    done = records.revert(kb, made.changeset_id, agent=PERSON)
    assert sorted(done["retired"]) == sorted(made.refs.values())
    assert done["restored"] == done["declined"] == done["skipped"] == [] and done["links"] == 1
    assert {status(kb, ref) for ref in made.refs.values()} == {"deprecated"}
    retired_by = kb.execute("SELECT retired_by FROM link WHERE changeset_id = %s", (made.changeset_id,)).fetchone()[0]
    assert retired_by == uuid.UUID(done["changeset_id"])
    agent, reason = kb.execute("SELECT agent, reason FROM changeset WHERE id = %s", (done["changeset_id"],)).fetchone()
    assert agent == PERSON and reason == f"revert of {made.changeset_id}"
    again = records.revert(kb, made.changeset_id, agent=PERSON)       # второй раз — уже выведено, ничего не трогает
    assert again["retired"] == [] and {s["why"] for s in again["skipped"]} == {"retired"}


def test_revert_of_an_edit_brings_back_the_previous_body_and_status(kb):
    first = commit(kb, [Item("entity", part()), Item("atom", fact())], agent=PERSON)
    ref = first.refs["@a"]
    records.set_status(kb, "atom", ref.split(":", 1)[1], "verified", agent=PERSON)
    before = lookup.get(kb, "atom", ref)["content_hash"]
    edited = commit(kb, [Item("atom", edit("Кронштейн К-1 держит 60 кг", first.refs["@part"]), ref.split(":", 1)[1],
                               status="needs_review")], agent=MODEL)
    assert status(kb, ref) == "needs_review"
    done = records.revert(kb, edited.changeset_id, agent=PERSON)
    assert done["restored"] == [ref] and done["retired"] == []
    now = lookup.get(kb, "atom", ref)
    assert now["content_hash"] == before and now["revision"] == 3     # история не стёрта: возврат — новая ревизия
    assert status(kb, ref) == "verified"


def test_revert_leaves_a_record_changed_after_the_batch(kb):
    made = commit(kb, [Item("entity", part()), Item("atom", fact())], agent=MODEL)
    ref = made.refs["@a"]
    commit(kb, [Item("atom", edit("Кронштейн К-1 держит 50 кг", made.refs["@part"]), ref.split(":", 1)[1])],
           agent=PERSON)
    done = records.revert(kb, made.changeset_id, agent=PERSON)
    assert done["skipped"] == [{"ref": ref, "why": "changed_later"}]
    assert done["retired"] == [made.refs["@part"]]
    assert status(kb, ref) != "deprecated"


def test_revert_declines_a_revision_waiting_for_review(kb):
    first = commit(kb, [Item("entity", part()), Item("atom", fact())], agent=PERSON)
    ref = first.refs["@a"]
    pending = commit(kb, [Item("atom", edit("Кронштейн К-1 держит 70 кг", first.refs["@part"]), ref.split(":", 1)[1],
                                review_state="pending")], agent=MODEL)
    done = records.revert(kb, pending.changeset_id, agent=PERSON)
    assert done["declined"] == [ref]
    state = kb.execute("SELECT review_state FROM atom_revision WHERE id = %s",
                       (pending.revisions[0].revision_id,)).fetchone()[0]
    assert state == "declined"


def test_revert_refuses_unknown_and_rejected_batches_and_models(kb):
    with pytest.raises(records.RecordNotFound):
        records.revert(kb, uuid7(), agent=PERSON)
    rejected = records.reject(kb, agent=MODEL, check_report={"format": "amem.check/1"})
    with pytest.raises(records.RecordError, match="rejected by the check"):
        records.revert(kb, rejected, agent=PERSON)
    made = commit(kb, [Item("entity", part())], agent=MODEL)
    with pytest.raises(api.Forbidden):
        api.revert_changeset(kb, made.changeset_id, agent=MODEL)
    with pytest.raises(api.BadRequest):
        api.revert_changeset(kb, "not-a-uuid", agent=PERSON)
    assert api.revert_changeset(kb, str(made.changeset_id), agent=PERSON)["retired"] == [made.refs["@part"]]
