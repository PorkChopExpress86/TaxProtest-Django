"""County-neutral web layer.

Each county's ETL is its own thing — different source files, different record
layouts, different models. What a homeowner *does* with the result is not: search
for a property, look at comparable properties, and see whether the assessment
supports a protest. This package holds that second half once, so every county
renders the same three pages from the same code.

A county participates by supplying a :class:`~counties.common.contracts.CountyAdapter`:
a description of how it wants its pages labelled and columned, plus methods that
turn its own models into the county-neutral :class:`~counties.common.contracts.Subject`
and :class:`~counties.common.contracts.Comp` records the shared views and templates
understand. Nothing here imports a county app.
"""
