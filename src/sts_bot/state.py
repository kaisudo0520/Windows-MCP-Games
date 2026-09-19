"""把 CommunicationMod 的訊息投影成小而有型別的快照（Snapshot）。

所有欄位都用預設值讀取，因為模組的格式只有範例沒有正式規格。
快照是視窗顯示的內容、紀錄器隨原始訊息一起存的內容，
也是之後要餵給策略（Jev）的精簡「決策用 payload」的起點。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from sts_bot.protocol import Message


def _int(d: dict[str, Any], key: str, default: int = 0) -> int:
    value = d.get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _str(d: dict[str, Any], key: str, default: str = "") -> str:
    value = d.get(key)
    return default if value is None else str(value)


def _names(items: Any) -> tuple[str, ...]:
    if not isinstance(items, list):
        return ()
    return tuple(_str(i, "name") for i in items if isinstance(i, dict))


@dataclass(frozen=True)
class CardView:
    index: int  # 1-based，對應 PLAY 的 CardIndex
    name: str
    cost: int
    type: str
    is_playable: bool
    has_target: bool
    upgrades: int
    exhausts: bool

    @classmethod
    def from_json(cls, index: int, d: dict[str, Any]) -> CardView:
        return cls(
            index=index,
            name=_str(d, "name"),
            cost=_int(d, "cost", -1),
            type=_str(d, "type"),
            is_playable=bool(d.get("is_playable", False)),
            has_target=bool(d.get("has_target", False)),
            upgrades=_int(d, "upgrades"),
            exhausts=bool(d.get("exhausts", False)),
        )

    def label(self) -> str:
        plus = "+" if self.upgrades else ""
        flags = ("" if self.is_playable else " 不可出") + (" 需目標" if self.has_target else "")
        return f"[{self.index}] {self.name}{plus} 費{self.cost}{flags}"


@dataclass(frozen=True)
class MonsterView:
    index: int  # monsters 陣列中的 1-based 位置；模組的 TargetIndex 是 0-based，見 target_index()
    name: str
    hp: int
    max_hp: int
    block: int
    intent: str
    damage: int
    hits: int
    is_gone: bool

    @classmethod
    def from_json(cls, index: int, d: dict[str, Any]) -> MonsterView:
        return cls(
            index=index,
            name=_str(d, "name"),
            hp=_int(d, "current_hp"),
            max_hp=_int(d, "max_hp"),
            block=_int(d, "block"),
            intent=_str(d, "intent"),
            damage=_int(d, "move_adjusted_damage"),
            hits=_int(d, "move_hits"),
            is_gone=bool(d.get("is_gone", False)),
        )

    @property
    def target_index(self) -> int:
        """模組在 PLAY／POTION 目標參數中期待的 0-based 序號。"""
        return self.index - 1

    def label(self) -> str:
        if self.is_gone:
            return f"[{self.target_index}] {self.name}（已離場）"
        dmg = f" 傷害{self.damage}x{self.hits}" if self.hits > 1 else (f" 傷害{self.damage}" if self.damage else "")
        blk = f" 格擋{self.block}" if self.block else ""
        return f"[{self.target_index}] {self.name} {self.hp}/{self.max_hp}{blk} 意圖:{self.intent}{dmg}"


@dataclass(frozen=True)
class CombatView:
    turn: int
    energy: int
    block: int
    hand: tuple[CardView, ...]
    monsters: tuple[MonsterView, ...]
    draw_pile: int
    discard_pile: int
    exhaust_pile: int

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> CombatView:
        raw_player = d.get("player")
        player: dict[str, Any] = raw_player if isinstance(raw_player, dict) else {}
        hand = [c for c in d.get("hand") or [] if isinstance(c, dict)]
        monsters = [m for m in d.get("monsters") or [] if isinstance(m, dict)]
        return cls(
            turn=_int(d, "turn"),
            energy=_int(player, "energy"),
            block=_int(player, "block"),
            hand=tuple(CardView.from_json(i + 1, c) for i, c in enumerate(hand)),
            monsters=tuple(MonsterView.from_json(i + 1, m) for i, m in enumerate(monsters)),
            draw_pile=len(d.get("draw_pile", []) or []),
            discard_pile=len(d.get("discard_pile", []) or []),
            exhaust_pile=len(d.get("exhaust_pile", []) or []),
        )


@dataclass(frozen=True)
class Snapshot:
    in_game: bool
    ready_for_command: bool
    available_commands: tuple[str, ...]
    error: str | None
    screen_type: str
    room_type: str
    room_phase: str
    floor: int
    act: int
    hp: int
    max_hp: int
    gold: int
    player_class: str
    ascension: int
    choices: tuple[str, ...]
    potions: tuple[str, ...]
    relics: tuple[str, ...]
    combat: CombatView | None

    @classmethod
    def from_message(cls, msg: Message) -> Snapshot:
        gs = msg.game_state or {}
        combat = gs.get("combat_state")
        return cls(
            in_game=msg.in_game,
            ready_for_command=msg.ready_for_command,
            available_commands=msg.available_commands,
            error=msg.error,
            screen_type=_str(gs, "screen_type"),
            room_type=_str(gs, "room_type"),
            room_phase=_str(gs, "room_phase"),
            floor=_int(gs, "floor"),
            act=_int(gs, "act"),
            hp=_int(gs, "current_hp"),
            max_hp=_int(gs, "max_hp"),
            gold=_int(gs, "gold"),
            player_class=_str(gs, "class"),
            ascension=_int(gs, "ascension_level"),
            choices=tuple(str(c) for c in gs.get("choice_list", []) or []),
            potions=_names(gs.get("potions")),
            relics=_names(gs.get("relics")),
            combat=CombatView.from_json(combat) if isinstance(combat, dict) else None,
        )

    @property
    def in_combat(self) -> bool:
        return self.combat is not None

    def headline(self) -> str:
        if not self.in_game:
            return "不在遊戲局中" + (f"｜錯誤：{self.error}" if self.error else "")
        where = f"第{self.act}幕 第{self.floor}層 {self.room_type or '?'}"
        screen = self.screen_type or self.room_phase or "?"
        combat = ""
        if self.combat:
            combat = f"｜回合 {self.combat.turn} 能量 {self.combat.energy} 格擋 {self.combat.block}"
        return f"{where}｜畫面 {screen}｜HP {self.hp}/{self.max_hp} 金幣 {self.gold}{combat}"

    def lines(self) -> list[str]:
        """給日誌與視窗看的文字摘要。"""
        out = [self.headline()]
        if self.error:
            out.append(f"模組錯誤：{self.error}")
        if self.combat:
            out.append("手牌：" + "、".join(c.label() for c in self.combat.hand))
            out.extend("敵人 " + m.label() for m in self.combat.monsters)
        if self.choices:
            out.append("選項：" + "、".join(f"[{i}] {c}" for i, c in enumerate(self.choices)))
        out.append("可用指令：" + ("、".join(self.available_commands) or "無") + ("" if self.ready_for_command else "（尚未就緒）"))
        return out

    def to_compact(self) -> dict[str, Any]:
        """可直接轉 JSON 的字典（tuple 轉 list）；之後 Jev payload 的起點。"""
        return _jsonable(asdict(self))


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value
