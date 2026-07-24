"""Scratch: render every cap-gap template branch with realistic CapGapResult objects."""

import django

django.setup()

from decimal import Decimal  # noqa: E402

from django.template.loader import render_to_string  # noqa: E402

from data.cap_analysis import evaluate_cap_gap  # noqa: E402
from data.models import AssessmentHistory  # noqa: E402


def entry(**kw):
    defaults = dict(account_number="X", tax_year=2026, cap_account="Y")
    defaults.update(kw)
    return AssessmentHistory(**defaults)


cases = {
    # scenario -> (entry, target, rate)
    "direct": (
        entry(
            market_value=Decimal("400000"),
            appraised_value=Decimal("400000"),
            assessed_value=Decimal("400000"),
        ),
        Decimal("360000"),
        Decimal("0.022"),
    ),
    "direct_no_rate": (
        entry(
            market_value=Decimal("400000"),
            appraised_value=Decimal("400000"),
            assessed_value=Decimal("400000"),
        ),
        Decimal("360000"),
        None,
    ),
    "effective": (
        entry(market_value=Decimal("462603"), appraised_value=Decimal("308092")),
        Decimal("290000"),
        Decimal("0.02"),
    ),
    "blocked": (
        entry(market_value=Decimal("462603"), appraised_value=Decimal("308092")),
        Decimal("330000"),
        Decimal("0.02"),
    ),
    "blocked_no_rate": (
        entry(market_value=Decimal("462603"), appraised_value=Decimal("308092")),
        Decimal("330000"),
        None,
    ),
    "cosmetic": (
        entry(market_value=Decimal("462603"), appraised_value=Decimal("308092")),
        Decimal("400000"),
        None,
    ),
    "already_below": (
        entry(market_value=Decimal("300000"), appraised_value=Decimal("300000")),
        Decimal("310000"),
        None,
    ),
    "no_target_engaged": (
        entry(market_value=Decimal("462603"), appraised_value=Decimal("308092")),
        None,
        None,
    ),
    "no_target_not_engaged": (
        entry(market_value=Decimal("300000"), appraised_value=Decimal("300000")),
        None,
        None,
    ),
    "legacy_stale_row": (
        entry(cap_account="", assessed_value=Decimal("350000")),
        Decimal("320000"),
        None,
    ),
    "missing": (None, Decimal("300000"), None),
}

import re  # noqa: E402

for name, (e, target, rate) in cases.items():
    result = evaluate_cap_gap(e, target, combined_rate=rate)
    from data.models import PropertyRecord

    stub = PropertyRecord(
        account_number="STUB001", street_number="1", street_name="Main", zipcode="77000"
    )
    html = render_to_string(
        "protest_analysis.html",
        {"cap_gap": result, "comps": [], "min_score": 70.0, "target_property": stub},
    )
    # Extract the cap-gap card region for inspection
    m = re.search(r"Cap Gap — Will a Protest", html)
    print(f"=== {name}: scenario={result.scenario} card_rendered={bool(m)}")
    if m:
        segment = html[m.start() : m.start() + 6000]
        # pull the narrative body div text
        body = re.search(
            r'<div class="px-6 py-4 text-sm[^"]*">(.*?)</div>', segment, re.S
        )
        if body:
            text = re.sub(r"<[^>]+>", "", body.group(1))
            text = re.sub(r"\s+", " ", text).strip()
            print("   BODY:", text)
        # bare-dollar check across the whole card
        card_end = segment.find("{% if tax_impact %}")
        bare = re.findall(r"\$\s*[<,.]|\$\s*</", re.sub(r"\s+", " ", segment))
        stats = re.findall(
            r'<p class="text-lg font-semibold[^"]*">(.*?)</p>', segment, re.S
        )
        stats = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s)).strip() for s in stats]
        print("   STATS:", stats)
        if bare:
            print("   !! BARE DOLLAR SIGNS:", bare)
print("done")
