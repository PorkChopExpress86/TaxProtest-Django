"""Exact identity-count policy, independent of county readiness and ETL."""

from dataclasses import dataclass, field


@dataclass
class OutcomePopulation:
    eligible: set[str]
    supported: bool = True
    reason: str = ""
    exclusions: dict[str, list[str]] = field(default_factory=dict)
    deliberately_absent: bool = False


def compare_coverage(
    previous: dict[str, OutcomePopulation], candidate: dict[str, OutcomePopulation]
) -> dict:
    outcomes, hard_failures, review_reasons = {}, [], []
    for name, current in candidate.items():
        prior = previous[name].eligible
        retained, additions = prior & current.eligible, current.eligible - prior
        lost = prior - current.eligible
        # County callers must supply affirmative source proof before removals
        # could be excluded. Mere absence always remains unexplained here.
        material_loss = bool(prior and len(lost) * 100 > len(prior))
        if current.supported and not current.eligible:
            hard_failures.append(f"{name}: zero eligible records for a supported outcome")
        if current.supported and not prior:
            review_reasons.append(f"{name}: eligible comparison baseline unavailable")
        if current.supported and material_loss and not current.deliberately_absent:
            review_reasons.append(f"{name}: unexplained loss exceeds 1%")
        outcomes[name] = {
            "supported": current.supported,
            "unavailable_reason": current.reason or None,
            "deliberately_absent": current.deliberately_absent,
            "total_ready": len(current.eligible),
            "prior_ready": len(prior),
            "retained_ready": len(retained),
            "additions": len(additions),
            "added_identities": sorted(additions),
            "verified_removals": [],
            "unexplained_loss": len(lost),
            "lost_identities": sorted(lost),
            "loss_fraction": {"numerator": len(lost), "denominator": len(prior)} if prior else None,
            "material_loss": material_loss,
            "exclusion_reasons": current.exclusions,
        }
    return {
        "outcomes": outcomes,
        "hard_failures": hard_failures,
        "requires_review": bool(review_reasons),
        "review_reasons": review_reasons,
        "automatic_publication_allowed": not hard_failures and not review_reasons,
    }
