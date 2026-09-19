"""Snapshot projection from a CommunicationMod message."""

from sts_bot.protocol import Message
from sts_bot.state import Snapshot


def test_combat_snapshot_fields(combat_state_dict):
    snap = Snapshot.from_message(Message(combat_state_dict, "utf-8"))
    assert snap.in_game and snap.ready_for_command and snap.in_combat
    assert (snap.act, snap.floor, snap.hp, snap.max_hp, snap.gold) == (1, 1, 68, 80, 99)
    assert snap.player_class == "IRONCLAD"
    assert snap.relics == ("Burning Blood",)
    assert snap.combat is not None
    assert (snap.combat.turn, snap.combat.energy, snap.combat.block) == (1, 3, 0)
    assert snap.combat.draw_pile == 2 and snap.combat.discard_pile == 0


def test_hand_indices_are_one_based_like_play_command(combat_state_dict):
    snap = Snapshot.from_message(Message(combat_state_dict, "utf-8"))
    hand = snap.combat.hand
    assert [c.index for c in hand] == [1, 2, 3, 4]
    assert hand[2].name == "Bash" and hand[2].cost == 2 and hand[2].has_target
    assert hand[1].upgrades == 1 and "Defend+" in hand[1].label()
    assert not hand[3].is_playable and "不可出" in hand[3].label()


def test_monster_target_index_is_zero_based_and_gone_is_kept(combat_state_dict):
    snap = Snapshot.from_message(Message(combat_state_dict, "utf-8"))
    monsters = snap.combat.monsters
    assert [m.target_index for m in monsters] == [0, 1]
    assert monsters[0].name == "Cultist" and monsters[0].hp == 54 and monsters[0].intent == "BUFF"
    assert monsters[1].is_gone and "已離場" in monsters[1].label()


def test_headline_and_lines_mention_the_essentials(combat_state_dict):
    snap = Snapshot.from_message(Message(combat_state_dict, "utf-8"))
    head = snap.headline()
    assert "第1幕 第1層 MonsterRoom" in head and "HP 68/80" in head and "能量 3" in head
    lines = snap.lines()
    assert any(line.startswith("手牌：[1] Strike") for line in lines)
    assert any("Cultist 54/54" in line for line in lines)
    assert lines[-1].startswith("可用指令：play、end")


def test_out_of_run_message_yields_minimal_snapshot():
    msg = Message({"in_game": False, "ready_for_command": True, "available_commands": ["START", "STATE"]}, "utf-8")
    snap = Snapshot.from_message(msg)
    assert not snap.in_game and snap.combat is None
    assert snap.available_commands == ("start", "state")
    assert snap.headline() == "不在遊戲局中"


def test_error_message_is_surfaced():
    snap = Snapshot.from_message(Message({"error": "bad index", "ready_for_command": True}, "utf-8"))
    assert snap.error == "bad index"
    assert "模組錯誤：bad index" in snap.lines()


def test_missing_or_malformed_fields_do_not_raise():
    raw = {
        "in_game": True,
        "ready_for_command": False,
        "game_state": {
            "floor": "not-a-number",
            "combat_state": {"player": None, "hand": [None, "x", {"name": "Strike"}], "monsters": "nope"},
            "choice_list": ["a", "b"],
        },
    }
    snap = Snapshot.from_message(Message(raw, "utf-8"))
    assert snap.floor == 0
    assert snap.combat is not None and len(snap.combat.hand) == 1 and snap.combat.monsters == ()
    assert snap.choices == ("a", "b")
    assert snap.to_compact()["choices"] == ["a", "b"]
