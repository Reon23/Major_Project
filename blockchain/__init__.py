"""
blockchain/__init__.py — Blockchain layer for the Active-Inference SDN controller.

Pure Python, no Ryu imports.  Simulates:
  - Ed25519-based controller identity (DID)
  - SHA-256-linked append-only ledger (single shared chain)
  - Smart-contract-style model trading (Fig. 2 of the paper)
  - IPFS-style content-addressed shard store
  - Per-cycle congestion-metric audit logging (Section III.A)
  - The 7-step trading protocol from Section III.A

A real Solidity + web3.py backend can be slotted in by implementing the
`LedgerBackend` interface (see ledger.py) without touching any SDN-side code.
"""

from blockchain.identity import ControllerIdentity, DIDRegistry  # noqa: F401
from blockchain.ledger import Block, Ledger, LedgerBackend  # noqa: F401
from blockchain.contracts import (  # noqa: F401
    ModelTradingContract,
    AccessToken,
)
from blockchain.model_store import ModelStore  # noqa: F401
from blockchain.trading import (  # noqa: F401
    ModelAuthorizationCertificate,
    TrustedVerificationNode,
    register_did,
    issue_mac,
    trade_model,
)
from blockchain.audit import log_cycle, compute_ciu  # noqa: F401
