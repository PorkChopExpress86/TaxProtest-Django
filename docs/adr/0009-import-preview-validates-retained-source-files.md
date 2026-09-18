---
status: accepted
---

# Import preview validates retained source files

For both counties, a validated import preview must fully validate the exact
retained source files intended for application without publishing database
changes. This requires more acquisition and validation work than source
discovery, but gives operators evidence tied to the intended inputs; it does not
prove that later database publication will succeed.

Implementation is pending: Harris preview translates rows without persistence,
while the Brazos property-import dry run currently omits full CAD preflight.
Neither behavior alone establishes this complete preview contract.
