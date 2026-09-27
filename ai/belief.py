"""
ai/belief.py — Gaussian generative model over path utilisation.

No Ryu imports. Pure Python + math.
"""

import json
import time
from typing import Optional


class PathBelief:
    """
    Gaussian belief over link utilisation for one candidate path.

    Generative model
    ----------------
        hidden state  s  ~ N(prior, sigma_prior²)   preferred utilisation
        observation   o  ~ N(s, sigma_obs²)          measurement noise

    Perception (Laplace approximation)
    -----------------------------------
        mu  ←  mu + α·(obs − mu)   (gradient descent on variational free energy F)

    Full variational free energy
    ----------------------------
        F = (obs − mu)² / (2·σ_obs²)      prediction error / likelihood
          + (mu − prior)² / (2·σ_prior²)  KL / complexity

    Confidence tracking
    -------------------
        σ_obs grows when path is idle  (less certain)
        σ_obs shrinks when active      (observations → more certain)
    """

    SIGMA_OBS_MIN: float = 0.04
    SIGMA_OBS_MAX: float = 0.35

    def __init__(
        self,
        prior: float = 0.2,
        sigma_prior: float = 0.15,
        sigma_obs: float = 0.10,
        alpha: float = 0.3,
    ):
        self.prior = prior
        self.sigma_prior = sigma_prior
        self.sigma_obs = sigma_obs
        self._alpha = alpha
        self.mu = prior  # belief mean, initialised at prior
        self._last_obs = prior

    # ── Perception ───────────────────────────────────────────────────────────

    def update(self, observation: float) -> None:
        """Gradient descent on variational free energy."""
        self._last_obs = max(0.0, min(1.0, observation))
        self.mu = max(0.0, min(1.0, self.mu + self._alpha * (self._last_obs - self.mu)))

    # ── Confidence tracking ──────────────────────────────────────────────────

    def reinforce_confidence(self, rate: float = 0.02) -> None:
        """Active path: observations reduce measurement uncertainty."""
        self.sigma_obs = max(self.SIGMA_OBS_MIN, self.sigma_obs - rate)

    def decay_confidence(self, rate: float = 0.05) -> None:
        """Idle path: lack of observations inflates uncertainty."""
        self.sigma_obs = min(self.SIGMA_OBS_MAX, self.sigma_obs + rate)

    # ── Free energy ──────────────────────────────────────────────────────────

    @property
    def free_energy(self) -> float:
        """Full Laplace variational free energy F = prediction_error + KL."""
        pe = (self._last_obs - self.mu) ** 2 / (2 * self.sigma_obs**2)
        kl = (self.mu - self.prior) ** 2 / (2 * self.sigma_prior**2)
        return pe + kl

    # ── Transition model ─────────────────────────────────────────────────────

    def predict_after_load_added(self, delta: float) -> float:
        return min(1.0, self.mu + delta)

    def predict_after_load_removed(self, delta: float) -> float:
        return max(0.0, self.mu - delta)

    # ── Accessors ────────────────────────────────────────────────────────────

    @property
    def utilisation(self) -> float:
        return self.mu

    def __repr__(self) -> str:
        return (
            f"PathBelief(mu={self.mu:.3f}, F={self.free_energy:.4f}, "
            f"sigma_obs={self.sigma_obs:.3f})"
        )

    # ── Snapshot (de)serialization for the blockchain model-trading layer ────
    #
    # The "model" that controllers trade (Section III of the paper) is a
    # serialized PathBelief snapshot — one controller's trained {prior,
    # sigma_prior, sigma_obs, alpha, mu} parameters for a given path,
    # plus the EFE hyperparameters it was tuned under. A controller
    # cold-starting a flow it has no history for can fetch a peer's
    # trained belief instead of starting from the flat default prior.
    #
    # These helpers live here (not in blockchain/) because they are
    # belief-format-aware; blockchain/ stays belief-agnostic and only
    # handles raw bytes.

    def to_snapshot(self) -> dict:
        """Return a JSON-serializable view of this belief's parameters."""
        return {
            "prior": self.prior,
            "sigma_prior": self.sigma_prior,
            "sigma_obs": self.sigma_obs,
            "alpha": self._alpha,
            "mu": self.mu,
        }

    @classmethod
    def from_snapshot(cls, snap: dict) -> "PathBelief":
        """
        Reconstruct a PathBelief from a snapshot. Unknown keys are
        ignored; missing keys fall back to __init__ defaults.
        """
        b = cls(
            prior=float(snap.get("prior", 0.2)),
            sigma_prior=float(snap.get("sigma_prior", 0.15)),
            sigma_obs=float(snap.get("sigma_obs", 0.10)),
            alpha=float(snap.get("alpha", 0.3)),
        )
        b.mu = float(snap.get("mu", b.prior))
        return b


def serialize_belief_snapshot(
    beliefs: dict,
    flow_key: Optional[tuple] = None,
    efe_hyperparameters: Optional[dict] = None,
) -> str:
    """
    Serialize a {idx: PathBelief} dict into a JSON string suitable for
    storing in the blockchain model store.

    The snapshot contains:
      - flow_key: optional (src_ip, dst_ip) tuple for traceability
      - efe_hyperparameters: the EFE tuning the beliefs were trained under
      - beliefs: per-path parameter dict
      - timestamp: snapshot creation time

    Returns
    -------
    str — JSON-encoded snapshot. Use `.encode("utf-8")` to get bytes
    for `ModelStore.store_model()`.
    """
    snapshot = {
        "flow_key": list(flow_key) if flow_key is not None else None,
        "efe_hyperparameters": dict(efe_hyperparameters) if efe_hyperparameters else {},
        "beliefs": {
            str(idx): b.to_snapshot() if hasattr(b, "to_snapshot") else dict(b)
            for idx, b in beliefs.items()
        },
        "timestamp": time.time(),
    }
    return json.dumps(snapshot, sort_keys=True)


def deserialize_belief_snapshot(payload) -> dict:
    """
    Inverse of `serialize_belief_snapshot`.

    Parameters
    ----------
    payload : str | bytes | dict
        JSON string / bytes, or an already-parsed dict.

    Returns
    -------
    dict[int, PathBelief] — ready to drop into a flow's `beliefs` slot.
    """
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8")
    if isinstance(payload, str):
        snapshot = json.loads(payload)
    elif isinstance(payload, dict):
        snapshot = payload
    else:
        raise TypeError(
            f"deserialize_belief_snapshot: unsupported payload type {type(payload)}"
        )

    beliefs = {}
    for idx_str, snap in snapshot.get("beliefs", {}).items():
        beliefs[int(idx_str)] = PathBelief.from_snapshot(snap)
    return beliefs
