#!/usr/bin/env python3
"""
blockchain_demo.py — Standalone demonstration of the blockchain layer.

No Ryu / Mininet required. Exercises the full 7-step model-trading
protocol between two ControllerIdentity instances (alpha and beta),
prints every ledger block, then flips one byte in a stored block and
shows verify_chain() catching the tamper.

Run
---
    python3 blockchain_demo.py

Acceptance criteria covered
---------------------------
- Everything in `blockchain/` is independently testable with no
  Ryu/Mininet running.
- Runs the full 7-step trade between two ControllerIdentity's.
- Prints each ledger block.
- Flips one byte in a stored block and shows verify_chain() catching it.
"""

from __future__ import annotations

import sys

# Make sure the project root is on sys.path so `from blockchain...` works
# regardless of where this script is invoked from.
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from blockchain.audit import compute_ciu, log_cycle  # noqa: E402
from blockchain.contracts import ModelTradingContract  # noqa: E402
from blockchain.identity import ControllerIdentity, DIDRegistry  # noqa: E402
from blockchain.ledger import Ledger  # noqa: E402
from blockchain.model_store import ModelStore  # noqa: E402
from blockchain.trading import trade_model, register_did  # noqa: E402

# Import the belief-snapshot serializer so the traded "model" is a real
# PathBelief snapshot, not a fake blob.
from ai.belief import (  # noqa: E402
    PathBelief,
    serialize_belief_snapshot,
    deserialize_belief_snapshot,
)


# ───────────────────────── helpers ─────────────────────────


def _line(char: str = "─", width: int = 72) -> str:
    return char * width


def _section(title: str) -> None:
    print()
    print(_line("═"))
    print(f"  {title}")
    print(_line("═"))


def _print_chain(ledger: Ledger) -> None:
    print(_line())
    print(f"  Ledger chain (length={ledger.length()})")
    print(_line())
    for block in ledger.chain:
        print(f"  #{block.index:>3}  {block.tx_type:<28}  hash={block.hash[:16]}…")
        print(f"        prev={block.prev_hash[:16]}…")
        print(f"        payload={block.payload}")
    print(_line())


# ───────────────────────── main demo ─────────────────────────


def main() -> int:
    _section("Step 0 — Bootstrap shared infrastructure")

    # Two logical controllers in one process. Partition matches the paper's
    # Nc ⊆ N model: c1 (alpha) manages {s1, s2}, c2 (beta) manages {s3, s4}.
    alpha = ControllerIdentity(did="alpha", managed_dpids={1, 2})
    beta = ControllerIdentity(did="beta", managed_dpids={3, 4})
    print(f"  alpha: {alpha}")
    print(f"  beta : {beta}")

    ledger = Ledger()
    registry = DIDRegistry()
    store = ModelStore(node_count=3)
    contract = ModelTradingContract(ledger)

    print(f"  Ledger genesis: {ledger.chain[0]}")
    print(f"  Initial verify_chain(): {ledger.verify_chain()}")

    # ── Step 1 — register both DIDs ───────────────────────────────────────
    _section("Step 1 — Register DIDs (publish to registry + ledger)")
    register_did(alpha, registry, ledger)
    register_did(beta, registry, ledger)

    # ── Build a "model" = a serialized PathBelief snapshot ────────────────
    _section("Build the model payload — a real PathBelief snapshot")

    # Pretend alpha has been training beliefs for path 0 of some flow
    # (10.0.0.1 -> 10.0.0.2). The snapshot is what beta will fetch when it
    # cold-starts the same flow.
    trained_beliefs = {
        0: PathBelief(prior=0.2, sigma_prior=0.15, sigma_obs=0.08, alpha=0.3),
        1: PathBelief(prior=0.2, sigma_prior=0.15, sigma_obs=0.12, alpha=0.3),
    }
    # Simulate a few perception updates so the snapshot is non-trivial.
    trained_beliefs[0].update(0.45)
    trained_beliefs[0].update(0.50)
    trained_beliefs[1].update(0.30)

    snapshot_str = serialize_belief_snapshot(
        trained_beliefs,
        flow_key=("10.0.0.1", "10.0.0.2"),
        efe_hyperparameters={
            "EFE_TEMPERATURE": 8.0,
            "PREFERRED_UTIL": 0.2,
            "MULTIPATH_CONGESTION_THRESHOLD": 0.45,
        },
    )
    payload_bytes = snapshot_str.encode("utf-8")
    print(f"  Snapshot JSON ({len(payload_bytes)} bytes):")
    print(f"    {snapshot_str}")
    # Parse it back for the timestamp field used below.
    import json as _json
    snapshot = _json.loads(snapshot_str)

    # ── Step 2 — alpha stores the model + registers it on the contract ────
    _section("Step 2 — Provider (alpha) stores the model + creates contract entry")

    # Register the model on the contract first to get a deterministic model_id,
    # then store the payload under that same id so trade_model can find it.
    import time as _time
    model_id = contract.create_model(
        owner=alpha.did,
        model_descriptions={
            "owner": alpha.did,
            "hash": "",  # filled in after sharding
            "timestamp": _time.time(),
            "reputation": 0.92,
        },
    )
    print(f"  Contract created model_id = {model_id}")

    shard_hashes = store.store_model(model_id, payload_bytes, num_shards=4)
    print(f"  Stored as {len(shard_hashes)} shards:")
    for i, h in enumerate(shard_hashes):
        print(f"    shard[{i}] {h[:24]}…")
    print(f"  Node distribution: {store.node_distribution()}")

    # Update the contract entry with the first shard's hash (Fig. 2's
    # "Model descriptions" box stores a content-addressable hash).
    contract.update_model_descriptions(model_id, shard_hashes[0])

    # ── Steps 1–7 — beta trades for the model ────────────────────────────
    _section("Steps 1–7 — beta trades for the model from alpha")
    print(f"  Requester: {beta.did}")
    print(f"  Provider:  {alpha.did}")
    print(f"  Model:     {model_id}")

    token = trade_model(
        requesting=beta,
        providing=alpha,
        model_id=model_id,
        registry=registry,
        store=store,
        contract=contract,
        ledger=ledger,
    )

    if token is None:
        print("  TRADE FAILED — see log above")
        return 1

    print(f"  AccessToken: token_id={token.token_id}")
    print(f"                provider={token.provider}, user={token.user}")

    # ── Beta retrieves the model and deserializes it ─────────────────────
    _section("Beta retrieves the model from the store and deserializes it")
    retrieved = store.retrieve_model(model_id)
    if retrieved is None:
        print("  RETRIEVE FAILED")
        return 1
    retrieved_snapshot = retrieved.decode("utf-8")
    print(f"  Retrieved {len(retrieved)} bytes — round-trips to original: "
          f"{retrieved == payload_bytes}")

    imported_beliefs = deserialize_belief_snapshot(retrieved_snapshot)
    print(f"  Imported beliefs:")
    for idx, b in imported_beliefs.items():
        print(f"    path{idx}: {b}")

    # ── Audit-log a fake routing cycle ───────────────────────────────────
    _section("Audit-log a routing cycle (metrics_log block)")
    ciu = compute_ciu(load=0.55, loss_fraction=0.02)
    print(f"  compute_ciu(load=0.55, loss=0.02) = {ciu:.4f}")
    log_cycle(
        ledger=ledger,
        controller_id=beta.did,
        src_ip="10.0.0.1",
        dst_ip="10.0.0.2",
        path=[1, 2, 4],
        G=0.1234,
        load_estimate=0.55,
        link_utils={(1, 2): 0.45, (2, 4): 0.55},
        link_losses={(1, 2): 0.01, (2, 4): 0.02},
        ciu=ciu,
    )

    # ── Print full chain ─────────────────────────────────────────────────
    _section("Final ledger state")
    _print_chain(ledger)

    # ── Verify chain integrity ───────────────────────────────────────────
    _section("Verify chain integrity")
    print(f"  verify_chain() before tamper: {ledger.verify_chain()}")

    # ── Tamper with one block — flip one byte of one payload field ───────
    _section("Tamper test — flip one byte in a stored block's payload")
    tamper_block = ledger.get_block(3)  # somewhere in the middle
    if tamper_block is None:
        print("  No block to tamper with — unexpected.")
        return 1

    print(f"  Target block #{tamper_block.index} ({tamper_block.tx_type})")
    print(f"  Original payload: {tamper_block.payload}")

    # Mutate the payload in place. verify_chain() will recompute this
    # block's hash from its (now-mutated) fields and find it no longer
    # matches the recorded `hash`.
    if "did" in tamper_block.payload:
        original = tamper_block.payload["did"]
        # Flip one character in the DID string.
        flipped = original[:-1] + ("X" if original[-1] != "X" else "Y")
        tamper_block.payload["did"] = flipped
        print(f"  Mutated payload['did']: {original!r} -> {flipped!r}")
    elif "owner" in tamper_block.payload:
        original = tamper_block.payload["owner"]
        flipped = original + "_tampered"
        tamper_block.payload["owner"] = flipped
        print(f"  Mutated payload['owner']: {original!r} -> {flipped!r}")
    else:
        # Fallback: append a tamper key
        tamper_block.payload["_tamper"] = "FLIPPED"
        print("  Added payload['_tamper'] = 'FLIPPED'")

    print(f"  Mutated payload:  {tamper_block.payload}")

    after_tamper = ledger.verify_chain()
    print(f"  verify_chain() after tamper:  {after_tamper}")
    if after_tamper:
        print("  !!! TAMPER NOT DETECTED — verify_chain should have returned False")
        return 1
    else:
        print("  TAMPER DETECTED — verify_chain correctly returned False ✓")

    _section("Demo complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
