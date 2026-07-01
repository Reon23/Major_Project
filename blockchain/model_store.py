"""
blockchain/model_store.py — Simulated IPFS content-addressed model store.

No Ryu imports. Pure Python + hashlib + threading.

Splits a model payload (bytes) into N shards, content-addresses each by
SHA-256, and distributes them across a small fixed number of simulated
node ids. Retrieval reassembles the payload from the shard hashes recorded
against the model.

This mirrors the IPFS-style storage layer of Fig. 2: a model is not stored
as a single blob; it is sharded, deduplicated by content hash, and any
node can serve any shard.

The (de)serialize_belief_snapshot() helpers live in `ai/belief.py` because
they are belief-format-aware; `blockchain/` stays belief-agnostic and only
handles raw bytes.
"""

from __future__ import annotations

import hashlib
import threading
from typing import Optional


class ModelStore:
    """
    In-process simulation of an IPFS-style shard store.

    Internal state
    --------------
    _nodes : dict[int, dict[str, bytes]]
        node_id -> {shard_hash: shard_bytes}.  Each simulated IPFS node
        holds a subset of all shards.
    _model_shards : dict[str, list[str]]
        model_id -> ordered list of shard hashes that reassemble to the
        original payload.
    """

    def __init__(self, node_count: int = 3):
        if node_count < 1:
            raise ValueError("node_count must be >= 1")
        self._nodes: dict[int, dict[str, bytes]] = {i: {} for i in range(node_count)}
        self._model_shards: dict[str, list[str]] = {}
        self._lock = threading.Lock()

    # ── Storage ────────────────────────────────────────────────────────────

    def store_model(
        self,
        model_id: str,
        payload: bytes,
        num_shards: int = 4,
    ) -> list[str]:
        """
        Split `payload` into `num_shards` shards, content-address each by
        SHA-256, distribute round-robin across nodes, and record the
        ordered shard hashes against `model_id`.

        Returns the list of shard hashes (in reassembly order).
        Overwrites any previous payload for the same model_id.
        """
        if num_shards < 1:
            num_shards = 1
        if not payload:
            # Empty payload — store as a single empty shard so retrieval
            # can still return b"" rather than "not found".
            shards = [b""]
        elif len(payload) < num_shards:
            # Payload smaller than shard count: just split byte-by-byte
            # padding the last chunk. Avoids tiny zero-length shards.
            shards = [payload[i:i + 1] for i in range(len(payload))]
            # Pad with one final empty shard so the count is meaningful.
            if len(shards) < num_shards:
                shards.append(b"")
        else:
            shard_size = (len(payload) + num_shards - 1) // num_shards
            shards = [
                payload[i * shard_size : (i + 1) * shard_size]
                for i in range(num_shards)
            ]

        shard_hashes: list[str] = []
        with self._lock:
            for i, shard in enumerate(shards):
                h = hashlib.sha256(shard).hexdigest()
                shard_hashes.append(h)
                node_id = i % len(self._nodes)
                # Dedup: same content hash → store once.
                self._nodes[node_id][h] = shard
            self._model_shards[model_id] = list(shard_hashes)

        return shard_hashes

    def retrieve_model(self, model_id: str) -> Optional[bytes]:
        """
        Reassemble the payload for `model_id` from its recorded shard
        hashes. Returns None if the model has never been stored; returns
        b"" for a model that was stored with an empty payload.
        """
        with self._lock:
            shard_hashes = self._model_shards.get(model_id)
            if shard_hashes is None:
                return None
            # Snapshot shard hashes to release the lock before I/O.
            shard_hashes = list(shard_hashes)

        shards: list[bytes] = []
        with self._lock:
            for h in shard_hashes:
                shard_bytes = None
                for node in self._nodes.values():
                    if h in node:
                        shard_bytes = node[h]
                        break
                if shard_bytes is None:
                    # Shard missing from every node — data loss.
                    return None
                shards.append(shard_bytes)
        return b"".join(shards)

    def has_model(self, model_id: str) -> bool:
        with self._lock:
            return model_id in self._model_shards

    def shard_hashes_for(self, model_id: str) -> list[str]:
        with self._lock:
            return list(self._model_shards.get(model_id, []))

    # ── Introspection (demo / debug) ───────────────────────────────────────

    def node_distribution(self) -> dict[int, int]:
        with self._lock:
            return {node_id: len(shards) for node_id, shards in self._nodes.items()}

    def total_shards(self) -> int:
        with self._lock:
            seen: set[str] = set()
            for shards in self._nodes.values():
                seen.update(shards.keys())
            return len(seen)
