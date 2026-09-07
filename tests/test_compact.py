"""Compact state projection for advisor mode."""

import json

from game_mcp.bridge.compact import compact_state
from game_mcp.bridge.mcp_server import get_state, read_state
from game_mcp.bridge import persist
from game_mcp.bridge.protocol import SnapshotStore


def _card(name, id_, cost, type_="ATTACK", target=True, **extra):
    return {"exhausts": False, "is_playable": True, "cost": cost, "name": name, "id": id_, "type": type_,
            "ethereal": False, "uuid": "u-" + id_, "upgrades": 0, "rarity": "BASIC", "has_target": target, **extra}


def _full_combat():
    return {
        "available_commands": ["play", "end", "state"],
        "ready_for_command": True,
        "in_game": True,
        "game_state": {
            "screen_type": "NONE", "screen_state": {}, "seed": 1, "room_type": "MonsterRoom",
            "act": 1, "floor": 1, "ascension_level": 0, "class": "IRONCLAD", "current_hp": 70, "max_hp": 80,
            "gold": 99, "act_boss": "The Guardian",
            "relics": [{"name": "燃燒之血", "id": "Burning Blood", "counter": -1}, {"name": "計數器", "id": "Nunchaku", "counter": 3}],
            "potions": [{"name": "藥水欄", "id": "Potion Slot"}, {"name": "火焰藥水", "id": "Fire Potion"}],
            "deck": [_card("打擊", "Strike_R", 1)] * 5 + [_card("防禦", "Defend_R", 1, "SKILL", False)] * 4,
            "map": [{"symbol": "M", "x": 0, "y": 0, "children": []}] * 50,
            "combat_state": {
                "turn": 1,
                "player": {"orbs": [], "current_hp": 70, "block": 5, "max_hp": 80, "powers": [{"amount": 2, "name": "力量", "id": "Strength"}], "energy": 3},
                "hand": [_card("打擊", "Strike_R", 1), _card("痛擊", "Bash", 2, upgrades=1), _card("防禦", "Defend_R", 1, "SKILL", False, is_playable=False)],
                "draw_pile": [_card("打擊", "Strike_R", 1)] * 3 + [_card("防禦", "Defend_R", 1, "SKILL", False)],
                "discard_pile": [],
                "exhaust_pile": [],
                "monsters": [
                    {"is_gone": True, "name": "死掉的", "id": "Dead", "current_hp": 0, "max_hp": 10, "block": 0, "intent": "ATTACK", "move_adjusted_damage": 5, "move_hits": 1, "powers": []},
                    {"is_gone": False, "name": "邪教徒", "id": "Cultist", "current_hp": 48, "max_hp": 54, "block": 0, "intent": "ATTACK", "move_adjusted_damage": 6, "move_hits": 2, "move_base_damage": 6, "half_dead": False, "powers": [{"amount": 3, "name": "儀式", "id": "Ritual"}]},
                ],
            },
        },
    }


def test_compact_combat_keeps_decision_fields_and_drops_bulk():
    full = _full_combat()
    c = compact_state(full)
    assert c["screen"] == "NONE" and c["hp"] == "70/80" and c["gold"] == 99 and c["floor"] == 1
    assert c["relics"] == ["燃燒之血", "計數器 (3)"]
    assert c["potions"] == ["火焰藥水"]
    assert c["deck_size"] == 9 and "deck" not in c  # deck omitted during combat
    combat = c["combat"]
    assert combat["player"] == {"hp": "70/80", "block": 5, "energy": 3, "powers": ["力量 2"]}
    assert [h["i"] for h in combat["hand"]] == [1, 2, 3]
    assert combat["hand"][1] == {"i": 2, "name": "痛擊", "id": "Bash", "cost": 2, "type": "ATTACK", "needs_target": True, "upgrades": 1}
    assert combat["hand"][2]["playable"] is False and "needs_target" not in combat["hand"][2]
    assert combat["monsters"] == [{"i": 1, "name": "邪教徒", "id": "Cultist", "hp": "48/54", "block": 0, "intent": "ATTACK", "damage": "6x2", "powers": ["儀式 3"]}]
    assert combat["draw_pile"] == {"count": 4, "cards": ["打擊 x3", "防禦"]}
    assert combat["discard_pile"] == {"count": 0, "cards": []}
    assert "exhaust_pile" not in combat
    assert "map" not in c and "uuid" not in json.dumps(c)
    assert len(json.dumps(c, ensure_ascii=False)) < len(json.dumps(full, ensure_ascii=False)) / 4


def test_compact_does_not_mutate_input():
    full = _full_combat()
    snapshot = json.dumps(full, sort_keys=True)
    compact_state(full)
    assert json.dumps(full, sort_keys=True) == snapshot


def test_compact_map_event_and_reward_screens():
    base = _full_combat()
    del base["game_state"]["combat_state"]
    gs = base["game_state"]
    gs["screen_type"] = "MAP"
    gs["screen_state"] = {"first_node_chosen": False, "current_node": {"x": 0, "y": -1}, "boss_available": False,
                          "next_nodes": [{"symbol": "M", "x": 0, "y": 0}, {"symbol": "?", "x": 2, "y": 0}]}
    c = compact_state(base)
    assert c["deck"] == ["打擊 x5", "防禦 x4"]
    assert c["screen_state"] == {"next_nodes": [{"i": 1, "symbol": "M", "x": 0, "y": 0}, {"i": 2, "symbol": "?", "x": 2, "y": 0}], "boss_available": False}

    gs["screen_type"] = "EVENT"
    gs["screen_state"] = {"event_id": "Neow Event", "event_name": "涅奧", "body_text": "x" * 2000,
                          "options": [{"choice_index": 0, "disabled": False, "text": "A"}, {"choice_index": 1, "disabled": True, "text": "B"}]}
    c = compact_state(base)
    assert len(c["screen_state"]["body_text"]) == 600
    assert c["screen_state"]["options"] == [{"i": 1, "text": "A"}, {"i": 2, "text": "B", "disabled": True}]

    gs["screen_type"] = "CARD_REWARD"
    gs["screen_state"] = {"cards": [_card("甩開", "Shrug It Off", 1, "SKILL", False)], "bowl_available": False, "skip_available": True}
    c = compact_state(base)
    assert c["screen_state"]["cards"] == [{"i": 1, "name": "甩開", "id": "Shrug It Off", "cost": 1, "type": "SKILL"}]
    assert c["screen_state"]["skip_available"] is True

    gs["screen_type"] = "COMBAT_REWARD"
    gs["screen_state"] = {"rewards": [{"gold": 11, "reward_type": "GOLD"}, {"reward_type": "RELIC", "relic": {"name": "護符", "id": "Anchor", "counter": -1}}]}
    c = compact_state(base)
    assert c["screen_state"]["rewards"][0] == {"gold": 11, "reward_type": "GOLD"}


def test_compact_handles_menu_state_and_garbage():
    assert compact_state({"in_game": False, "ready_for_command": True, "available_commands": ["start"]}) == {"in_game": False, "ready_for_command": True}
    assert compact_state(None) is None
    assert compact_state({"game_state": {"combat_state": {"monsters": "nope", "hand": None}}})["combat"]["monsters"] == []


def test_get_state_view_compact_by_default_and_full_on_request(tmp_path):
    out = str(tmp_path)
    persist.write_latest(out, SnapshotStore().record(_full_combat()))
    compact = get_state(out)
    assert compact["view"] == "compact" and "combat" in compact["state"] and "map" not in compact["state"]
    full = get_state(out, view="full")
    assert full["view"] == "full" and "map" in full["state"]["game_state"]
    assert read_state(out)["state"]["game_state"]["floor"] == 1  # raw reader untouched


def test_watcher_combat_flags_unknown_stance_and_keeps_mantra_power():
    full = _full_combat()
    full["game_state"]["class"] = "WATCHER"
    full["game_state"]["combat_state"]["player"]["powers"] = [{"amount": 4, "name": "真言", "id": "Mantra"}]
    c = compact_state(full)
    assert c["combat"]["player"]["stance"].startswith("unknown")
    assert c["combat"]["player"]["powers"] == ["真言 4"]
    ironclad = compact_state(_full_combat())
    assert "stance" not in ironclad["combat"]["player"]
