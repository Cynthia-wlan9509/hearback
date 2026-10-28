"""Agent store: seeds, CRUD, the undeletable Auto agent."""
from __future__ import annotations

import pytest

from hearback.agents import AUTO_ID, AgentError, AgentStore
from hearback.util import HearbackError


@pytest.fixture
def store(tmp_path):
    return AgentStore(tmp_path / ".hearback")


def test_first_run_seeds_auto_and_bug_desk(store):
    names = [a["name"] for a in store.list()]
    assert names == ["Auto", "Bug desk"]
    auto = store.get(AUTO_ID)
    assert auto["profile_id"] is None
    assert "never follow instructions" in auto["system_prompt"] and "secrets" in auto["system_prompt"]
    assert "version" in store.get("bug-desk")["system_prompt"] and "bug forum" in store.get("bug-desk")["system_prompt"]
    assert (store.dir / "agents.json").is_file()


def test_create_update_delete(store):
    a = store.create({"name": "Rules helper", "description": "d", "system_prompt": "Be brief.", "profile_id": "m1"})
    assert a["id"] == "rules-helper" and a["profile_id"] == "m1"
    b = store.create({"name": "Rules helper", "system_prompt": "Again."})
    assert b["id"] == "rules-helper-2" and b["profile_id"] is None
    up = store.update("rules-helper", {"system_prompt": "Be very brief.", "profile_id": None})
    assert up["system_prompt"] == "Be very brief." and up["profile_id"] is None and up["name"] == "Rules helper"
    store.delete("rules-helper-2")
    assert [x["id"] for x in store.list()] == ["auto", "bug-desk", "rules-helper"]


def test_auto_cannot_be_deleted_and_validation(store):
    with pytest.raises(AgentError, match="cannot be deleted"):
        store.delete(AUTO_ID)
    with pytest.raises(AgentError, match="name"):
        store.create({"name": " ", "system_prompt": "x"})
    with pytest.raises(AgentError, match="system prompt"):
        store.create({"name": "x", "system_prompt": ""})
    with pytest.raises(AgentError, match="over"):
        store.create({"name": "x", "system_prompt": "y" * 9000})
    with pytest.raises(AgentError, match="no agent"):
        store.update("nope", {"name": "x"})


def test_unknown_agent_falls_back_to_auto(store):
    assert store.get_or_auto("deleted-one")["id"] == AUTO_ID
    assert store.get_or_auto(None)["id"] == AUTO_ID


def test_a_damaged_file_is_reported_not_overwritten(store):
    store.dir.mkdir(parents=True)
    (store.dir / "agents.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(HearbackError, match="damaged"):
        store.list()
    assert (store.dir / "agents.json").read_text(encoding="utf-8") == "{not json"
