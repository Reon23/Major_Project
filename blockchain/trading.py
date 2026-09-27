"""
blockchain/trading.py — Orchestrates the 7-step model-trading protocol
from Section III.A of the paper.

No Ryu imports. Pure Python + cryptography + threading.

Protocol (one entry point: `trade_model`)
─────────────────────────────────────────
  1. register_did(controller)        publish DID document to registry + ledger
  2. issue_mac(controller, model_id) provider signs Model Authorization
                                      Certificate (MAC) over the model metadata
  3. request_model(requesting, mid)  requester checks availability in store
  4. forward_to_tvn(mac, requester)  hand MAC + requester to a Trusted
                                      Verification Node (TVN)
  5. tvn_verify(mac)                 TVN looks up issuer DID, cross-checks
                                      public key + algorithm against MAC sig
  6. authorize_access_token(...)     on success: contract mints AccessToken
  7. trade recorded on the ledger    (implicit in step 6 — contract appends)

Returns the AccessToken on success, or None (with a logged reason) on the
expected "verification failed" / "model not found" cases.  Unexpected
errors still raise.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

from blockchain.contracts import AccessToken, ModelTradingContract
from blockchain.identity import ControllerIdentity, DIDRegistry
from blockchain.ledger import Ledger
from blockchain.model_store import ModelStore


_log = logging.getLogger(__name__)


# ── Data classes ────────────────────────────────────────────────────────────


@dataclass
class ModelAuthorizationCertificate:
    """
    Model Authorization Certificate (MAC) issued by a provider controller
    to authorize a single model trade.

    Fields
    ------
    model_id        : str           the model being authorized
    issuer_did      : str           DID of the issuing (provider) controller
    model_metadata  : dict          model description from the contract
    t_mac           : float         issuance timestamp
    signature       : bytes         Ed25519 signature over the canonical
                                    payload (model_id, issuer_did,
                                    model_metadata, t_mac)
    """
    model_id: str
    issuer_did: str
    model_metadata: dict
    t_mac: float
    signature: bytes

    def canonical_payload(self) -> bytes:
        """Deterministic byte payload that the signature covers."""
        return json.dumps(
            {
                "model_id": self.model_id,
                "issuer_did": self.issuer_did,
                "model_metadata": self.model_metadata,
                "t_mac": self.t_mac,
            },
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        ).encode("utf-8")


# ── Trusted Verification Node ──────────────────────────────────────────────


class TrustedVerificationNode:
    """
    Trusted Verification Node (TVN) from Section III.A.

    The TVN is the neutral third party that cross-checks a MAC: it looks
    up the issuer's DID document in the registry and verifies the MAC
    signature against the public key + algorithm recorded there.

    Verification genuinely fails on:
      - unknown issuer DID
      - mismatched encryption_algorithm
      - tampered signature / payload
      - rotated public key (after `DIDRegistry.update`)
    """

    def __init__(self, registry: DIDRegistry):
        self.registry = registry

    def verify(self, mac: ModelAuthorizationCertificate) -> bool:
        doc = self.registry.lookup(mac.issuer_did)
        if doc is None:
            _log.warning("TVN: unknown issuer DID %s", mac.issuer_did)
            return False

        # Cross-check the encryption algorithm recorded in the DID document.
        algorithm = doc.get("encryption_algorithm", "")
        if algorithm != "Ed25519":
            _log.warning(
                "TVN: unsupported encryption_algorithm %r for DID %s",
                algorithm,
                mac.issuer_did,
            )
            return False

        try:
            public_key_bytes = bytes.fromhex(doc["public_key"])
        except (KeyError, ValueError):
            _log.warning("TVN: malformed public_key in DID document for %s", mac.issuer_did)
            return False

        return ControllerIdentity.verify(
            public_key_bytes, mac.canonical_payload(), mac.signature
        )


# ── Protocol step helpers ──────────────────────────────────────────────────


def register_did(
    controller: ControllerIdentity,
    registry: DIDRegistry,
    ledger: Ledger,
    transaction_endpoint: str = "",
) -> dict:
    """Step 1 — publish the controller's DID document to registry + ledger."""
    if not transaction_endpoint:
        transaction_endpoint = f"in-process://{controller.did}"
    doc = registry.publish(controller, transaction_endpoint=transaction_endpoint)
    ledger.append("did_register", doc)
    _log.info("DID registered: %s -> %s", controller.did, doc["public_key"][:16] + "…")
    return doc


def issue_mac(
    controller: ControllerIdentity,
    model_id: str,
    model_metadata: dict,
) -> ModelAuthorizationCertificate:
    """Step 2 — provider signs a MAC over the model metadata."""
    t_mac = time.time()
    mac = ModelAuthorizationCertificate(
        model_id=model_id,
        issuer_did=controller.did,
        model_metadata=dict(model_metadata),
        t_mac=t_mac,
        signature=b"",
    )
    mac.signature = controller.sign(mac.canonical_payload())
    return mac


def request_model(
    requesting: ControllerIdentity,
    model_id: str,
    store: ModelStore,
) -> bool:
    """Step 3 — requester checks that the model is available in the store."""
    available = store.has_model(model_id)
    if not available:
        _log.info(
            "request_model: %s — model %s not found in store",
            requesting.did,
            model_id,
        )
    return available


def forward_to_tvn(
    mac: ModelAuthorizationCertificate,
    requesting: ControllerIdentity,
    tvn: TrustedVerificationNode,
) -> bool:
    """Step 4 + 5 — forward MAC to TVN and let it verify."""
    return tvn.verify(mac)


# ── Single entry point ─────────────────────────────────────────────────────


def trade_model(
    requesting: ControllerIdentity,
    providing: ControllerIdentity,
    model_id: str,
    registry: DIDRegistry,
    store: ModelStore,
    contract: ModelTradingContract,
    ledger: Ledger,
    logger: Optional[logging.Logger] = None,
) -> Optional[AccessToken]:
    """
    Run the full 7-step trading protocol for `model_id` from `providing`
    to `requesting`.

    Returns the AccessToken on success, or None on:
      - model not in store (step 3)
      - model not registered on contract (step 6)
      - TVN verification failure (step 5)
      - model withdrawn

    Does NOT raise for these expected failure cases — only for unexpected
    internal errors.
    """
    log = logger or _log

    # Step 3 — availability check
    if not request_model(requesting, model_id, store):
        log.warning(
            "trade_model: model %s not available — aborting trade %s -> %s",
            model_id, providing.did, requesting.did,
        )
        return None

    # Fetch model metadata from the contract (used as the MAC payload).
    model_metadata = contract.get_model_descriptions(model_id)
    if model_metadata is None:
        log.warning(
            "trade_model: model %s not registered on contract — aborting",
            model_id,
        )
        return None

    # Step 2 — provider issues the MAC
    mac = issue_mac(providing, model_id, model_metadata)

    # Steps 4 + 5 — forward to TVN and verify
    tvn = TrustedVerificationNode(registry)
    if not forward_to_tvn(mac, requesting, tvn):
        log.warning(
            "trade_model: TVN verification failed for %s (issuer=%s) — aborting",
            model_id, providing.did,
        )
        ledger.append(
            "trade_failed",
            {
                "model_id": model_id,
                "provider": providing.did,
                "user": requesting.did,
                "reason": "tvn_verification_failed",
            },
        )
        return None

    # Step 6 — contract mints an access token
    token = contract.authorize_access_token(
        model_id=model_id,
        provider=providing.did,
        user=requesting.did,
    )
    if token is None:
        log.warning(
            "trade_model: contract refused to authorize %s for %s — aborting",
            model_id, requesting.did,
        )
        return None

    # Step 7 — trade already recorded by step 6's authorize_access_token call.
    log.info(
        "trade_model: SUCCESS  %s -> %s  model=%s  token=%s",
        providing.did, requesting.did, model_id, token.token_id,
    )
    return token
