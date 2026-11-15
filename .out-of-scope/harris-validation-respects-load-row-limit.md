# Harris source validation that stops at a load's row limit

Harris source validation always inspects and translates the whole retained
property file. It does not shorten its reading to match the row limit of a
single-file real-account load, and its stored counts are not adjusted to the
rows the load actually wrote.

## Why this is out of scope

Validation and the load answer different questions.

Validation answers "is this retained source sound?". ADR-0009 defines a
Validated import preview as full source validation of the exact retained
files, and ADR-0013 keeps those files so the evidence can be checked against
them later. The retained file is the whole file whether or not the load that
follows is limited. A validation that stopped after N rows would be stored
evidence vouching for only part of a file that is retained in full.

The row limit is a write option. It exists on one operator command, described
there as being for testing, and it tells the staged load to stop early. It
says nothing about which bytes were acquired, retained or trusted.

The resulting difference is real: for a limited load, validation's per-dataset
counts and its residential account map describe the whole file, while the
load's counts describe the first N rows. That difference is already explained
by stored evidence. The Import operation records the request, including the
limit and append options, and Harris recovery reads them back to refuse
automatic recovery for an append or limited publication. Anyone reading the
operation can see the load was limited.

Making validation respect the limit would also change stored validation
evidence for limited loads, which ADR-0024 treats as a contract, in order to
serve a testing option.

What would change this decision: the row limit becoming a routine production
feature whose partial result is published and relied on. At that point what
validation vouches for would need its own design, not a truncated read.

## Related

- A check that validation's files and counts equal what the staged load loaded
  applies to unlimited loads only.
- The raw layout inspection's separate numeric rules are a different question
  and are tracked on their own.

## Prior requests

- #181: "Harris source validation ignores the row limit of a single-file real-account load"
