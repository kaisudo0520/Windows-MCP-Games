"""Compact view of a CommunicationMod state for token-frugal advisors.

The mod pushes 8-13 KB per state (full deck, every relic/potion field, the whole map,
draw/discard piles with UUIDs). An advisor needs a fraction of that per decision. This
module derives a small, readable projection; it never mutates the input and never
raises on missing fields (the mod's shape varies by screen).

Indexes (``i``) are 1-based and follow screen order left to right, which is how the
human operator will be told what to play. Dead monsters (``is_gone``) are omitted so
the indexes match what is visible.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

MAX_TEXT = 600  # event body text is kept, but capped
#: CommunicationMod does not report the Watcher's stance (verified in GameStateConverter:
#: the player object has hp/block/energy/powers/orbs only), so the compact view says so
#: explicitly instead of letting the advisor assume Neutral.
STANCE_UNKNOWN = "unknown (not reported by the mod; ask the player)"


def _card(card: dict[str, Any], index: int | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if index is not None:
        out["i"] = index
    out["name"] = card.get("name")
    out["id"] = card.get("id")
    out["cost"] = card.get("cost")
    out["type"] = card.get("type")
    if card.get("has_target"):
        out["needs_target"] = True
    if card.get("upgrades"):
        out["upgrades"] = card["upgrades"]
    if card.get("is_playable") is False and "is_playable" in card:
        out["playable"] = False
    for flag in ("exhausts", "ethereal"):
        if card.get(flag):
            out[flag] = True
    return out


def _card_label(card: dict[str, Any]) -> str:
    name = str(card.get("name") or card.get("id") or "?")
    if card.get("upgrades"):
        name += "+" + str(card["upgrades"])
    return name


def _pile(cards: Any) -> dict[str, Any]:
    """``{"count": n, "cards": ["Strike x3", "Bash"]}`` for a card pile."""
    if not isinstance(cards, list):
        return {"count": 0, "cards": []}
    counts = Counter(_card_label(c) for c in cards if isinstance(c, dict))
    return {"count": len(cards), "cards": [f"{n} x{k}" if k > 1 else n for n, k in counts.items()]}


def _powers(powers: Any) -> list[str]:
    if not isinstance(powers, list):
        return []
    return [f"{p.get('name') or p.get('id')} {p.get('amount')}" if p.get("amount") is not None else str(p.get("name") or p.get("id")) for p in powers if isinstance(p, dict)]


def _monster(m: dict[str, Any], index: int) -> dict[str, Any]:
    out: dict[str, Any] = {
        "i": index,
        "name": m.get("name"),
        "id": m.get("id"),
        "hp": f"{m.get('current_hp')}/{m.get('max_hp')}",
        "block": m.get("block", 0),
        "intent": m.get("intent"),
    }
    dmg = m.get("move_adjusted_damage")
    if isinstance(dmg, int) and dmg >= 0:
        hits = m.get("move_hits") or 1
        out["damage"] = f"{dmg}x{hits}" if hits > 1 else dmg
    if m.get("half_dead"):
        out["half_dead"] = True
    powers = _powers(m.get("powers"))
    if powers:
        out["powers"] = powers
    return out


def _combat(cs: dict[str, Any], player_class: Any = None) -> dict[str, Any]:
    player = cs.get("player") if isinstance(cs.get("player"), dict) else {}
    out: dict[str, Any] = {
        "turn": cs.get("turn"),
        "player": {
            "hp": f"{player.get('current_hp')}/{player.get('max_hp')}",
            "block": player.get("block", 0),
            "energy": player.get("energy"),
        },
    }
    powers = _powers(player.get("powers"))
    if powers:
        out["player"]["powers"] = powers
    if player.get("orbs"):
        out["player"]["orbs"] = player["orbs"]
    if player_class == "WATCHER":
        out["player"]["stance"] = STANCE_UNKNOWN
    hand = cs.get("hand") if isinstance(cs.get("hand"), list) else []
    out["hand"] = [_card(c, i) for i, c in enumerate(hand, 1) if isinstance(c, dict)]
    monsters = cs.get("monsters") if isinstance(cs.get("monsters"), list) else []
    living = [m for m in monsters if isinstance(m, dict) and not m.get("is_gone")]
    out["monsters"] = [_monster(m, i) for i, m in enumerate(living, 1)]
    out["draw_pile"] = _pile(cs.get("draw_pile"))
    out["discard_pile"] = _pile(cs.get("discard_pile"))
    exhaust = _pile(cs.get("exhaust_pile"))
    if exhaust["count"]:
        out["exhaust_pile"] = exhaust
    return out


def _screen_state(screen_type: Any, ss: Any) -> Any:
    """Per-screen projection; unknown screens pass through with card lists compacted."""
    if not isinstance(ss, dict):
        return ss if ss else None
    if screen_type == "MAP":
        nodes = ss.get("next_nodes") if isinstance(ss.get("next_nodes"), list) else []
        return {
            "next_nodes": [{"i": i, "symbol": n.get("symbol"), "x": n.get("x"), "y": n.get("y")} for i, n in enumerate(nodes, 1) if isinstance(n, dict)],
            "boss_available": ss.get("boss_available"),
        }
    if screen_type == "EVENT":
        options = ss.get("options") if isinstance(ss.get("options"), list) else []
        return {
            "event_name": ss.get("event_name"),
            "body_text": str(ss.get("body_text") or "")[:MAX_TEXT],
            "options": [
                {"i": (o.get("choice_index", i - 1) + 1), "text": o.get("text"), **({"disabled": True} if o.get("disabled") else {})}
                for i, o in enumerate(options, 1)
                if isinstance(o, dict)
            ],
        }
    return {k: _generic(v) for k, v in ss.items()}


def _generic(value: Any) -> Any:
    """Compact any list of card-like dicts; leave everything else untouched."""
    if isinstance(value, list) and value and all(isinstance(v, dict) and "uuid" in v for v in value):
        return [_card(c, i) for i, c in enumerate(value, 1)]
    if isinstance(value, list) and value and all(isinstance(v, dict) and set(v) >= {"id", "name"} for v in value):
        return [v.get("name") or v.get("id") for v in value]
    return value


def compact_state(state: Any) -> Any:
    """Return the compact projection of a full mod state (or the input if unknown)."""
    if not isinstance(state, dict):
        return state
    gs = state.get("game_state")
    if not isinstance(gs, dict):
        return {"in_game": state.get("in_game"), "ready_for_command": state.get("ready_for_command")}
    out: dict[str, Any] = {
        "in_game": state.get("in_game"),
        "ready_for_command": state.get("ready_for_command"),
        "screen": gs.get("screen_type"),
        "room_type": gs.get("room_type"),
        "act": gs.get("act"),
        "floor": gs.get("floor"),
        "ascension": gs.get("ascension_level"),
        "class": gs.get("class"),
        "hp": f"{gs.get('current_hp')}/{gs.get('max_hp')}",
        "gold": gs.get("gold"),
        "act_boss": gs.get("act_boss"),
        "relics": [
            f"{r.get('name') or r.get('id')} ({r['counter']})" if isinstance(r.get("counter"), int) and r["counter"] >= 0 else (r.get("name") or r.get("id"))
            for r in (gs.get("relics") or [])
            if isinstance(r, dict)
        ],
        "potions": [p.get("name") or p.get("id") for p in (gs.get("potions") or []) if isinstance(p, dict) and p.get("id") != "Potion Slot"],
    }
    deck = gs.get("deck") if isinstance(gs.get("deck"), list) else []
    out["deck_size"] = len(deck)
    combat = gs.get("combat_state")
    if isinstance(combat, dict):
        out["combat"] = _combat(combat, gs.get("class"))
    else:
        out["deck"] = _pile(deck)["cards"]
    screen_state = _screen_state(gs.get("screen_type"), gs.get("screen_state"))
    if screen_state:
        out["screen_state"] = screen_state
    return out
