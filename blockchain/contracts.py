"""
blockchain/contracts.py — Smart-contract-style model trading.

No Ryu imports. Pure Python.

Mirrors the three contract categories from Fig. 2 of the paper.  Every
method call appends a transaction to the shared `Ledger` so the chain is
the single source of truth for who-traded-what-when.

┌──────────────────────────────────────────────────────────────────────────┐
│ Distributed Identity Management                                          │
│   publish_verifi_tools(public_key, encryption_algorithm)                 │
│   update_verifi_tools(original_public_key, new_public_key,               │
│                       new_encryption_algorithm)                          │
│   withdraw_verifi_tools(public_key)                                      │
├──────────────────────────────────────────────────────────────────────────┤
│ Distributed Assets Management                                            │
│   create_model(owner, model_descriptions) -> model_id                   │
│     model_descriptions = {owner, hash, timestamp, reputation}            │
│   update_model_descriptions(model_id, hash)                              │
├──────────────────────────────────────────────────────────────────────────┤
│ Distributed Model Trading                                                │
│   authorize_access_token(model_id, provider, user) -> AccessToken        │
│   withdraw_model(model_id)                                               │
│                                                                            │
│ Every call writes a "Transaction Record" {model_id, provider, user}      │
│ to the ledger (Fig. 2, bottom-right box).                                │
└──────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from blockchain.ledger import Ledger


@dataclass
class AccessToken:
    """
    Authorization token minted by `authorize_access_token`.

    A successful model trade returns one of these.  The holder (user) can
    present it to the IPFS-style model store to retrieve the model payload.
    """
    token_id: str
    model_id: str
    provider: str
    user: str
    issued_at: float
    expires_at: float = 0.0  # 0 = no expiry (in-process simulation)


@dataclass
class ModelRecord:
    """Internal record of a registered model."""
    model_id: str
    owner: str
    hash: str
    timestamp: float
    reputation: float
    withdrawn: bool = False
    description_history: list = field(default_factory=list)


class ModelTradingContract:
    """
    Implements the three contract categories from Fig. 2. Every method
    that mutates state appends a corresponding transaction to the shared
    `Ledger`.
    """

    def __init__(self, ledger: Ledger):
        self.ledger = ledger
        self._models: dict[str, ModelRecord] = {}
        self._tokens: dict[str, AccessToken] = {}
        # identity-side registry mirror: public_key_hex -> {algorithm, owner_did}
        self._verifi_tools: dict[str, dict] = {}
        self._lock = threading.Lock()

    # ── Distributed Identity Management ────────────────────────────────────
    # (Mirror of DIDRegistry's identity methods, expressed as contract calls
    #  so they land on the chain with a "Transaction Record".)

    def publish_verifi_tools(
        self,
        public_key: str,
        encryption_algorithm: str,
        owner_did: str = "",
    ) -> dict:
        payload = {
            "public_key": public_key,
            "encryption_algorithm": encryption_algorithm,
            "owner_did": owner_did,
        }
        with self._lock:
            self._verifi_tools[public_key] = {
                "algorithm": encryption_algorithm,
                "owner_did": owner_did,
            }
        self.ledger.append("publish_verifi_tools", payload)
        return payload

    def update_verifi_tools(
        self,
        original_public_key: str,
        new_public_key: str,
        new_encryption_algorithm: str,
    ) -> dict:
        payload = {
            "original_public_key": original_public_key,
            "new_public_key": new_public_key,
            "new_encryption_algorithm": new_encryption_algorithm,
        }
        with self._lock:
            existing = self._verifi_tools.pop(original_public_key, None)
            owner_did = existing["owner_did"] if existing else ""
            self._verifi_tools[new_public_key] = {
                "algorithm": new_encryption_algorithm,
                "owner_did": owner_did,
            }
        self.ledger.append("update_verifi_tools", payload)
        return payload

    def withdraw_verifi_tools(self, public_key: str) -> bool:
        with self._lock:
            removed = self._verifi_tools.pop(public_key, None)
        self.ledger.append(
            "withdraw_verifi_tools", {"public_key": public_key, "existed": removed is not None}
        )
        return removed is not None

    # ── Distributed Assets Management ──────────────────────────────────────

    def create_model(
        self,
        owner: str,
        model_descriptions: dict,
    ) -> str:
        """
        Register a new model.  `model_descriptions` should contain
        {hash, timestamp, reputation} (matching Fig. 2's "Model
        descriptions" box); missing fields are defaulted.

        Returns the new model_id.
        """
        model_id = f"model_{uuid.uuid4().hex[:12]}"
        record = ModelRecord(
            model_id=model_id,
            owner=owner,
            hash=model_descriptions.get("hash", ""),
            timestamp=model_descriptions.get("timestamp", time.time()),
            reputation=float(model_descriptions.get("reputation", 0.0)),
        )
        record.description_history.append({"hash": record.hash, "timestamp": record.timestamp})

        with self._lock:
            self._models[model_id] = record
        self.ledger.append(
            "create_model",
            {
                "model_id": model_id,
                "owner": owner,
                "descriptions": {
                    "owner": owner,
                    "hash": record.hash,
                    "timestamp": record.timestamp,
                    "reputation": record.reputation,
                },
            },
        )
        return model_id

    def update_model_descriptions(self, model_id: str, hash: str) -> bool:
        with self._lock:
            record = self._models.get(model_id)
            if record is None or record.withdrawn:
                return False
            record.hash = hash
            record.description_history.append({"hash": hash, "timestamp": time.time()})
        self.ledger.append(
            "update_model_descriptions",
            {"model_id": model_id, "hash": hash},
        )
        return True

    def get_model_descriptions(self, model_id: str) -> Optional[dict]:
        with self._lock:
            record = self._models.get(model_id)
            if record is None:
                return None
            return {
                "model_id": record.model_id,
                "owner": record.owner,
                "hash": record.hash,
                "timestamp": record.timestamp,
                "reputation": record.reputation,
                "withdrawn": record.withdrawn,
            }

    def is_model_available(self, model_id: str) -> bool:
        with self._lock:
            record = self._models.get(model_id)
            return record is not None and not record.withdrawn

    # ── Distributed Model Trading ──────────────────────────────────────────

    def authorize_access_token(
        self,
        model_id: str,
        provider: str,
        user: str,
    ) -> Optional[AccessToken]:
        """
        Mint an AccessToken for `user` to access `model_id` from `provider`.

        Returns None (and logs to ledger) if the model is missing or
        withdrawn — the caller decides whether to raise or fall back.
        """
        with self._lock:
            record = self._models.get(model_id)
            if record is None or record.withdrawn:
                self.ledger.append(
                    "authorize_access_token_failed",
                    {
                        "model_id": model_id,
                        "provider": provider,
                        "user": user,
                        "reason": "missing_or_withdrawn",
                    },
                )
                return None

        token = AccessToken(
            token_id=f"tok_{uuid.uuid4().hex[:12]}",
            model_id=model_id,
            provider=provider,
            user=user,
            issued_at=time.time(),
        )
        with self._lock:
            self._tokens[token.token_id] = token

        # The Transaction Record from Fig. 2's bottom-right box.
        self.ledger.append(
            "authorize_access_token",
            {
                "transaction_record": {
                    "model_id": model_id,
                    "provider": provider,
                    "user": user,
                },
                "token_id": token.token_id,
                "issued_at": token.issued_at,
            },
        )
        return token

    def withdraw_model(self, model_id: str) -> bool:
        with self._lock:
            record = self._models.get(model_id)
            if record is None or record.withdrawn:
                return False
            record.withdrawn = True
        self.ledger.append(
            "withdraw_model",
            {"model_id": model_id, "owner": record.owner},
        )
        return True

    # ── Accessors ──────────────────────────────────────────────────────────

    def all_model_ids(self) -> list[str]:
        with self._lock:
            return [mid for mid, r in self._models.items() if not r.withdrawn]

    def get_token(self, token_id: str) -> Optional[AccessToken]:
        with self._lock:
            return self._tokens.get(token_id)
