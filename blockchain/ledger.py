"""
blockchain/ledger.py — Append-only SHA-256-linked ledger.

No Ryu imports. Pure Python + hashlib + threading.

Single shared chain: every transaction in the system — DID registration,
contract calls, audit-log entries — lands on this one chain as a Block with
a distinct `tx_type`. This is the "smart contract continuously logs key
network metrics on the blockchain" behaviour from Section III.A.

A real Solidity + web3.py backend can be slotted in by implementing the
`LedgerBackend` interface; no SDN-side code would need to change.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Optional


def _canonical_json(obj: Any) -> str:
    """
    Deterministic JSON encoding used for hashing.  sort_keys ensures the
    hash is independent of dict insertion order; default=str handles any
    unexpected non-JSON types (e.g. bytes) by stringifying them.
    """
    return json.dumps(obj, sort_keys=True, default=str, separators=(",", ":"))


class Block:
    """
    One block in the chain.

    Fields
    ------
    index       : int            block height (genesis = 0)
    timestamp   : float          Unix epoch seconds at creation
    prev_hash   : str            SHA-256 hex of the previous block
    tx_type     : str            e.g. "genesis", "did_register",
                                  "create_model", "authorize_access_token",
                                  "metrics_log"
    payload     : dict           transaction-specific data
    hash        : str            SHA-256 hex over (index, timestamp,
                                  prev_hash, tx_type, payload) — recomputed
                                  by `verify_chain()` so tampering with any
                                  field is detected
    """

    def __init__(
        self,
        index: int,
        timestamp: float,
        prev_hash: str,
        tx_type: str,
        payload: dict,
    ):
        self.index = index
        self.timestamp = timestamp
        self.prev_hash = prev_hash
        self.tx_type = tx_type
        self.payload = payload
        self.hash = self._compute_hash()

    def _compute_hash(self) -> str:
        canonical = _canonical_json(
            {
                "index": self.index,
                "timestamp": self.timestamp,
                "prev_hash": self.prev_hash,
                "tx_type": self.tx_type,
                "payload": self.payload,
            }
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        """Serializable view (for state.json export / display)."""
        return {
            "index": self.index,
            "timestamp": self.timestamp,
            "prev_hash": self.prev_hash,
            "tx_type": self.tx_type,
            "payload": self.payload,
            "hash": self.hash,
        }

    def __repr__(self) -> str:
        return (
            f"Block(#{self.index} {self.tx_type} "
            f"hash={self.hash[:10]}…)"
        )


class LedgerBackend(ABC):
    """
    Interface a real-chain backend (Solidity + web3.py against
    Ganache/Hardhat) would implement. The SDN side never calls anything
    outside this interface, so a swap is localized to one new module.
    """

    @abstractmethod
    def append(self, tx_type: str, payload: dict) -> Block:
        """Append a new block. Returns the appended block."""

    @abstractmethod
    def verify_chain(self) -> bool:
        """Recompute every block hash + linkage. False on any mismatch."""

    @abstractmethod
    def tail(self, n: int) -> list[Block]:
        """Return the last n blocks (newest last)."""

    @abstractmethod
    def length(self) -> int:
        """Total block count including genesis."""


class Ledger(LedgerBackend):
    """
    In-process simulated chain. Thread-safe via a single threading.Lock
    around mutation (same pattern as `TopologyManager`).

    Note on the genesis block: index=0, prev_hash = "0"*64 (zero hash), and
    an empty payload. It is created in __init__ and never re-appended.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._chain: list[Block] = [self._genesis()]

    @staticmethod
    def _genesis() -> Block:
        return Block(
            index=0,
            timestamp=time.time(),
            prev_hash="0" * 64,
            tx_type="genesis",
            payload={"note": "Active-Inference SDN blockchain layer genesis"},
        )

    # ── LedgerBackend implementation ────────────────────────────────────────

    def append(self, tx_type: str, payload: dict) -> Block:
        with self._lock:
            prev = self._chain[-1]
            block = Block(
                index=len(self._chain),
                timestamp=time.time(),
                prev_hash=prev.hash,
                tx_type=tx_type,
                payload=dict(payload),
            )
            self._chain.append(block)
            return block

    def verify_chain(self) -> bool:
        """
        Recompute every block's hash from its fields and check that the
        prev_hash pointer chain is intact. Returns False if any block has
        been mutated or any linkage is broken.
        """
        with self._lock:
            for i in range(len(self._chain)):
                cur = self._chain[i]
                # Recompute this block's hash from its current fields.
                if cur.hash != cur._compute_hash():
                    return False
                # Check prev_hash linkage.
                if i == 0:
                    if cur.prev_hash != "0" * 64:
                        return False
                else:
                    prev = self._chain[i - 1]
                    if cur.prev_hash != prev.hash:
                        return False
                    # Also verify prev.hash itself is consistent.
                    if prev.hash != prev._compute_hash():
                        return False
            return True

    def tail(self, n: int) -> list[Block]:
        with self._lock:
            if n <= 0:
                return []
            return list(self._chain[-n:])

    def length(self) -> int:
        with self._lock:
            return len(self._chain)

    # ── Convenience accessors ───────────────────────────────────────────────

    @property
    def head_hash(self) -> str:
        with self._lock:
            return self._chain[-1].hash

    @property
    def chain(self) -> list[Block]:
        """Snapshot of the whole chain (for the demo / debugging)."""
        with self._lock:
            return list(self._chain)

    def get_block(self, index: int) -> Optional[Block]:
        with self._lock:
            if 0 <= index < len(self._chain):
                return self._chain[index]
            return None

    def summary_for_state(self, last_n: int = 10) -> dict:
        """
        Compact summary for `state.json`'s optional "ledger" key.
        Existing readers that ignore unknown keys keep working unchanged.
        """
        with self._lock:
            tail_blocks = self._chain[-last_n:]
            return {
                "chain_length": len(self._chain),
                "latest_block_hash": self._chain[-1].hash,
                "latest_block_index": self._chain[-1].index,
                "last_transactions": [
                    {
                        "index": b.index,
                        "tx_type": b.tx_type,
                        "timestamp": b.timestamp,
                        "payload": b.payload,
                        "hash": b.hash,
                    }
                    for b in tail_blocks
                ],
            }
