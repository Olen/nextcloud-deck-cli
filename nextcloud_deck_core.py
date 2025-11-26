#!/usr/bin/env python3
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import List, Optional, Tuple, Any

import requests
from dateutil import parser as dateparser

"""
Core Nextcloud Deck client library.

Direct app route, API v1.1 only:
  {BASE_URL}/index.php/apps/deck/api/v1.1/boards/{board_id}/stacks
"""


API_BASE_SUFFIX = "/index.php/apps/deck/api/v1.1"
HEADERS = {
    "OCS-APIRequest": "true",
    "Accept": "application/json",
}


@dataclass
class Label:
    id: int
    title: str
    color: Optional[str] = None  # normalized hex "rrggbb" or None


@dataclass
class Card:
    id: int
    title: str
    order: int = 0
    archived: bool = False
    duedate: Optional[datetime] = None
    owner: Optional[str] = None
    assignees: List[str] = field(default_factory=list)
    labels: List[Label] = field(default_factory=list)
    stack_id: Optional[int] = None  # current stack id
    source: Optional[dict] = None   # The raw dict with all info

    def __post_init__(self):
        self.title = self.title.strip()

@dataclass
class Stack:
    id: int
    title: str
    order: int = 0
    cards: List[Card] = field(default_factory=list)


class DeckClient:
    """
    Thin wrapper around the Nextcloud Deck v1.1 API using the direct app route.
    Handles:
      - auth/session
      - fetching stacks + cards
      - basic card creation and moves
    """

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        board_id: int,
        timeout: int = 30,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.board_id = board_id
        self.timeout = timeout

        self.session = requests.Session()
        self.session.auth = (username, password)
        self.session.headers.update(HEADERS)

    # -------------- Internal helpers --------------

    @property
    def api_base(self) -> str:
        return self.base_url + API_BASE_SUFFIX

    def _get_json(self, resp: requests.Response) -> Any:
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and "ocs" in data and "data" in data["ocs"]:
            return data["ocs"]["data"]
        return data

    @staticmethod
    def _parse_duedate(raw: Optional[str]) -> Optional[datetime]:
        if not raw:
            return None
        try:
            dt = dateparser.parse(raw)
            if not dt.tzinfo:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            return None

    @staticmethod
    def _normalize_hex_color(c: Optional[str]) -> Optional[str]:
        """
        Normalize Deck's label hex color to 6-char lowercase 'rrggbb'.
        Handles:
          - '31CC7C', '92e314'
          - '#31CC7C'
          - 8-char ARGB/RGBA (drops alpha)
          - 3-char RGB (expands to 6)
        """
        if not c:
            return None
        s = c.strip().lstrip("#")
        if len(s) == 3:  # e.g. 'abc' -> 'aabbcc'
            s = "".join(ch * 2 for ch in s)
        elif len(s) == 8:  # drop alpha
            s = s[2:]
        if len(s) != 6:
            return None
        try:
            int(s, 16)
        except ValueError:
            return None
        return s.lower()

    def _parse_label(self, raw: dict) -> Label:
        return Label(
            id=raw.get("id"),
            title=raw.get("title", ""),
            color=self._normalize_hex_color(raw.get("color")),
        )

    def _parse_card(self, raw: dict, stack_id: Optional[int]) -> Card:
        labels = [
            self._parse_label(lb)
            for lb in (raw.get("labels") or [])
            if lb.get("title")
        ]

        owner_raw = raw.get("owner") or {}
        owner_name = owner_raw.get("displayname") or owner_raw.get("primaryKey") or None

        assignees = [
            (u.get("displayname") or u.get("primaryKey") or "")
            for u in (raw.get("assignedUsers") or [])
            if (u.get("displayname") or u.get("primaryKey"))
        ]

        return Card(
            id=raw.get("id"),
            title=raw.get("title"),
            order=raw.get("order", 0),
            archived=raw.get("archived", False),
            duedate=self._parse_duedate(raw.get("duedate")),
            owner=owner_name,
            assignees=assignees,
            labels=labels,
            stack_id=stack_id,
            source=raw,
        )

    # -------------- Public API --------------

    def fetch_stacks(self, include_archived: bool = True) -> List[Stack]:
        """
        Fetch stacks + embedded cards for this board.
        """
        url = f"{self.api_base}/boards/{self.board_id}/stacks"
        resp = self.session.get(url, timeout=self.timeout)
        raw_stacks = self._get_json(resp)

        stacks: List[Stack] = []
        for s in sorted(raw_stacks, key=lambda x: x.get("order", 0)):
            raw_cards = s.get("cards") or []
            if not include_archived:
                raw_cards = [c for c in raw_cards if not c.get("archived", False)]

            cards: List[Card] = [
                self._parse_card(c, s.get("id"))
                for c in sorted(raw_cards, key=lambda x: x.get("order", 0))
            ]

            stacks.append(
                Stack(
                    id=s.get("id"),
                    title=s.get("title"),
                    order=s.get("order", 0),
                    cards=cards,
                )
            )
        return stacks

    # ---------- Convenience finders ----------

    @staticmethod
    def _norm(s: Optional[str]) -> str:
        return (s or "").strip().lower()

    def find_stack_by_name(self, stacks: List[Stack], name: str) -> Optional[Stack]:
        target = self._norm(name)
        for s in stacks:
            if self._norm(s.title) == target:
                return s
        return None

    def find_card_by_title(
        self, stacks: List[Stack], title: str
    ) -> Optional[Tuple[Stack, Card]]:
        """
        Find first card whose title matches (case & whitespace insensitive).
        Returns (stack, card) or None.
        """
        target = self._norm(title)
        for s in stacks:
            for c in s.cards:
                if self._norm(c.title) == target:
                    return s, c
        return None

    # ---------- Mutations ----------

    def create_card(self, stack_id: int, title: str) -> Card:
        """
        Create a card in the given stack.
        """
        url = f"{self.api_base}/boards/{self.board_id}/stacks/{stack_id}/cards"
        payload = {"title": title, "type": "plain"}
        resp = self.session.post(url, json=payload, timeout=self.timeout)
        data = self._get_json(resp)
        return self._parse_card(data, stack_id)


    def move_card_url(self, board_id: int, stack_id: int, card_id: int) -> str:
        # See https://github.com/nextcloud/deck/issues/6830
        # return f"{self.api_base}/boards/{board_id}/stacks/{stack_id}/cards/{card_id}/reorder"
        return f"{self.base_url}/apps/deck/cards/{card_id}/reorder"


    def move_card_payload(self, card: Card, target_stack_id: int) -> dict:
        # See https://github.com/nextcloud/deck/issues/6830
        # payload = {
        #     "stackId": target_stack_id,
        #     "order": card.get("order", None)
        # }
        payload = card.source
        payload["stackId"] = target_stack_id
        return payload


    def move_card(self, card: Card, target_stack_id: int) -> Card:
        """
        Move a card to another stack by updating its stackId.

        NOTE: This assumes a PUT endpoint like:
          PUT /boards/{board_id}/stacks/{current_stack_id}/cards/{card_id}
        with a JSON body that includes stackId. Adjust if your Deck version differs.
        """
        if card.stack_id is None:
            raise ValueError("Card has no current stack_id set")

        # NOTE: Endpoint & payload may need slight adjustment depending on your Deck version.
        # See https://github.com/nextcloud/deck/issues/6830
        url = self.move_card_url(self.board_id, card.stack_id, card.id)
        payload = self.move_card_payload(card, target_stack_id)

        resp = self.session.put(url, json=payload, timeout=self.timeout)
        data = self._get_json(resp)

        # We should re-parse from server return in case it updated order, etc.
        # but the return from the "bad" url is not a complete card.
        if isinstance(data, list):
            card.stack_id = target_stack_id
            return card
        updated = self._parse_card(data, target_stack_id)
        return updated

    # Convenience for JSON output
    @staticmethod
    def to_dict(obj: Any) -> dict:
        """
        Convert dataclasses (Stack/Card/Label) to plain dicts.
        """
        return asdict(obj)

