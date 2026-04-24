"""
ai/belief.py — Gaussian generative model over path utilisation.

No Ryu imports. Pure Python + math.
"""


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
        self.mu = prior          # belief mean, initialised at prior
        self._last_obs = prior

    # ── Perception ───────────────────────────────────────────────────────────

    def update(self, observation: float) -> None:
        """Gradient descent on variational free energy."""
        self._last_obs = max(0.0, min(1.0, observation))
        self.mu = max(0.0, min(1.0,
                      self.mu + self._alpha * (self._last_obs - self.mu)))

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
        pe = (self._last_obs - self.mu) ** 2 / (2 * self.sigma_obs ** 2)
        kl = (self.mu - self.prior) ** 2 / (2 * self.sigma_prior ** 2)
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
