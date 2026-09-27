"""
blockchain/identity.py — Decentralized Identity (DID) for SDN controllers.

No Ryu imports. Pure Python + cryptography.

Implements the "Distributed Identity Management" column of Fig. 2:
  - Each controller has an Ed25519 keypair and a DID document.
  - The DID document is the thing written to the ledger on registration
    (not a separate store) — `DIDRegistry.publish()` returns the document
    and the caller appends it to the chain.
  - Verification uses real Ed25519 signatures, so tampering with a public
    key in the registry causes subsequent MAC verification to fail.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


# Default algorithm label written to the DID document.
# Ed25519 is used for both signatures (MAC) and "verification tools" in the
# paper's terminology — the encryption_algorithm field is kept generic so a
# future swap to e.g. Ed448 or secp256k1 only needs a new label here.
DEFAULT_ENCRYPTION_ALGORITHM = "Ed25519"


class ControllerIdentity:
    """
    A logical SDN controller's identity.

    Attributes
    ----------
    did : str
        Decentralized identifier — a short human-readable string
        (e.g. "alpha", "beta") unique within the in-process simulation.
    managed_dpids : set[int]
        Set of switch dpids this controller is authoritative for.
        Matches the paper's Nc ⊆ N model (controller domain ⊆ network).
    t_reg : float
        Registration timestamp (Unix epoch seconds). Set when the
        identity is first registered via `DIDRegistry.publish`.
    """

    def __init__(
        self,
        did: str,
        managed_dpids: set[int],
        t_reg: Optional[float] = None,
        private_key: Optional[Ed25519PrivateKey] = None,
    ):
        self.did = did
        self.managed_dpids = set(managed_dpids)
        self.t_reg = t_reg if t_reg is not None else time.time()
        self._private_key = private_key or Ed25519PrivateKey.generate()
        self._public_key = self._private_key.public_key()

    # ── Key access ──────────────────────────────────────────────────────────

    @property
    def public_key_bytes(self) -> bytes:
        """32-byte raw Ed25519 public key."""
        return self._public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

    @property
    def public_key_hex(self) -> str:
        return self.public_key_bytes.hex()

    # ── Signing ─────────────────────────────────────────────────────────────

    def sign(self, payload: bytes) -> bytes:
        """Sign an arbitrary byte payload with this controller's private key."""
        return self._private_key.sign(payload)

    @staticmethod
    def verify(
        public_key_bytes: bytes, payload: bytes, signature: bytes
    ) -> bool:
        """
        Verify an Ed25519 signature. Returns False on any verification
        failure (wrong key, tampered payload, malformed signature) rather
        than raising, so callers can treat "verify failed" as ordinary
        control flow.
        """
        try:
            pk = Ed25519PublicKey.from_public_bytes(public_key_bytes)
            pk.verify(signature, payload)
            return True
        except Exception:
            return False

    def __repr__(self) -> str:
        return (
            f"ControllerIdentity(did={self.did!r}, "
            f"managed_dpids={sorted(self.managed_dpids)}, "
            f"pub={self.public_key_hex[:12]}…)"
        )


class DIDRegistry:
    """
    In-memory DID document registry.

    Maps `did -> DID document`. The DID document is what gets written to the
    ledger on registration (not a separate store) — `publish()` returns the
    document and the caller appends it to the chain.

    Thread-safety: an internal threading.Lock guards all mutations, mirroring
    the locking pattern in `sdn/topology_manager.py`.
    """

    def __init__(self):
        self._docs: dict[str, dict] = {}
        self._lock = threading.Lock()

    # ── Distributed Identity Management (Fig. 2, left column) ──────────────

    def publish(
        self,
        identity: ControllerIdentity,
        transaction_endpoint: str,
        encryption_algorithm: str = DEFAULT_ENCRYPTION_ALGORITHM,
    ) -> dict:
        """
        Publish (or re-publish) a DID document for `identity`.

        Returns the DID document that should be appended to the ledger by
        the caller (so the ledger stays the single source of truth for
        order, while this registry is the live lookup index).
        """
        doc = {
            "did": identity.did,
            "public_key": identity.public_key_hex,
            "encryption_algorithm": encryption_algorithm,
            "transaction_endpoint": transaction_endpoint,
            "t_reg": identity.t_reg,
            "managed_dpids": sorted(identity.managed_dpids),
        }
        with self._lock:
            self._docs[identity.did] = doc
        return doc

    def update(
        self,
        did: str,
        new_public_key_hex: str,
        new_encryption_algorithm: str = DEFAULT_ENCRYPTION_ALGORITHM,
    ) -> Optional[dict]:
        """Rotate the public key / algorithm for an existing DID."""
        with self._lock:
            doc = self._docs.get(did)
            if doc is None:
                return None
            doc["public_key"] = new_public_key_hex
            doc["encryption_algorithm"] = new_encryption_algorithm
            return dict(doc)

    def withdraw(self, did: str) -> bool:
        """Remove a DID document — subsequent lookups will return None."""
        with self._lock:
            return self._docs.pop(did, None) is not None

    # ── Lookups ─────────────────────────────────────────────────────────────

    def lookup(self, did: str) -> Optional[dict]:
        with self._lock:
            doc = self._docs.get(did)
            return dict(doc) if doc is not None else None

    def all_dids(self) -> list[str]:
        with self._lock:
            return list(self._docs.keys())

    def find_controller_for_dpid(self, dpid: int) -> Optional[str]:
        """Return the DID of the controller that manages `dpid`, or None."""
        with self._lock:
            for did, doc in self._docs.items():
                if dpid in doc.get("managed_dpids", []):
                    return did
        return None
