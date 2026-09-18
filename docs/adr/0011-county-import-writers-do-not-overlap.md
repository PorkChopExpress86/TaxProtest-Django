---
status: accepted
---

# County import writers do not overlap

An overlapping writer for the same county is rejected immediately with the active
operation's identity rather than queued or allowed to compete. Different counties
may write independently. This favors an explicit operator retry over an operation
waiting and later applying against unexpectedly changed county state.

Reservations release automatically only when the owning writer has definitively
ended. Uncertain ownership blocks new writers; elapsed time alone does not
authorize competition. An audited operator recovery action must first verify
that the previous writer cannot continue.

The shared Django admin import area displays each county's persisted writer
status, active operation identity, and a visible recovery-required state. It
provides access to the operator recovery action and its audit evidence.

Implementation of county-wide coordination is pending; Harris persistence
documents one authoritative writer but does not itself coordinate writers.
