# Blast-radius & contract safety scan

Fourteen dimensions. Walk them against **this PR's diff** plus whatever evidence Step 4
produced — not the whole repo. Skip the ones the diff can't reach and say so; for each one
that applies, either produce a finding or record that you checked it.

A pre-existing issue in untouched code is out of scope: at most a "pre-existing, not from
this PR" note, never a finding against the author. Ground every gap in a `file:line` (or a
test name, CI line, plan address) and the specific rule it misses.

## 1. Blast radius

What else does this touch that the PR didn't mention? Shared or common modules, base
classes, generated code, a default value, a template, a symlinked or vendored file,
anything on the hot path of every request or record. A change here hits every consumer — if
the PR frames a fan-out change as "small", **that gap is the finding**.

## 2. Irreversibility

If this is wrong, how do we get back? Rank by that. Data migrations and deletions,
destroyed or replaced live resources, consumed queue offsets, published messages, emails
sent, money moved, released artifacts and tags, overwritten state — none of these roll back
with a revert. A deploy that flips a revision does. Say which kind this is.

## 3. Contracts with the outside

Anything renamed, removed, or re-shaped that something outside this diff depends on:

- API request/response shapes, status codes, error formats
- DB schema, table and column names, index names
- environment variables, config keys, secret names
- queue / topic / stream / channel names
- CLI flags and their defaults, exit codes
- exported package symbols, public types
- wire and serialization formats, file layouts
- metric names, log field names, trace span names
- image tags, chart values, resource names

A rename is only safe if every consumer changes with it — and consumers in *other* repos
are invisible from here, so name the risk explicitly rather than assuming it's contained.

## 4. Secrets & log hygiene

No credentials, tokens, keys, connection strings, or certificates in code, config, tests,
or fixtures — and none *logged* either. Check that new log lines don't emit a secret, a
token, a full auth header, or a whole payload containing personal data.

```sh
git diff "origin/$BASE...origin/$HEAD" \
  | grep -niE 'password|secret|key|token|credential|conn|bearer|api[-_]?key|private[-_]?key'
```

Eyeball every hit — most are false positives (variable names), which is why this is a read,
not a gate. Also check whether a real secret was committed and then "removed" in a later
commit: it's still in history, and that's a High finding with a rotation implication.

## 5. Failure handling

New error paths: is anything swallowed (catch + log + carry on), retried forever, retried
when it should fail fast, or failed fast when it should retry? Any new early
`return`/`continue`/`break` that silently skips work is a data-path change even when it
looks like a guard. Ask what the caller sees when this fails, and whether a partial failure
leaves consistent state.

## 6. Config & startup

A new setting must be: actually bound and read; sensibly defaulted so existing deployments
keep working; documented wherever the repo documents config; and validated at **startup**
rather than blowing up on first use. Secrets must never carry a real default. A
required-but-absent value should refuse to start, loudly. Watch for a key added to one
environment's config but not its siblings — and for list/map values, which many config
systems replace wholesale rather than merging.

## 7. Backwards compatibility

Changed defaults that shift existing behavior. Removed or renamed things still consumed.
Data written by the old version still readable by the new one — and vice versa, because a
rolling deploy runs both at once. Old clients still served. Check the repo's own stated
compatibility promise if it has one.

## 8. Enforcement vs. intent

Is the obligation actually *enforced* — by code, a schema, a type, a CI gate — or only
stated in the PR body, a comment, or a doc? Stated obligations decay as soon as the author
moves on. **This is where the most valuable findings usually live.**

## 9. Tests as enforcement

From Step 4: behavior pinned by an assertion that would fail without the change, at the
right level, with independently derived expectations, and nothing quietly skipped,
disabled, loosened, or deleted.

## 10. Docs & adjacent artifacts

Whatever the repo's definition of done requires: docs, README, changelog, migration notes,
API spec, config table, ADR. Behavior changes that are operational contracts should be
called out in the PR body, not just in code. Also: does the PR delete documentation the
repo explicitly protects?

## 11. Dependencies & pins

A new dependency: needed? maintained? license compatible? transitive weight? does it drag
in a banned or duplicate library? Loosened version constraints (`>=`, `^`, a floating tag
where a pin existed) turn a reproducible build into a moving target. Lockfile consistent
with the manifest. Pinned action or image digests not swapped for mutable tags.

## 12. CI/CD & permissions

Only if the pipeline is touched:

- least-privilege token permissions, per job rather than workflow-wide
- no secrets reachable from an untrusted trigger, and no trigger that runs PR-authored
  code with write access
- OIDC / federated subjects still matching what the cloud side trusts
- deploys pinning immutable digests, not mutable tags
- concurrency groups that can't drop a run which must happen
- gates not silently removed or made unreachable
- third-party actions pinned to a SHA

## 13. The repo's own hard rules

From the Step 2 profile: check the diff mechanically against each documented MUST/NEVER —
banned imports or APIs, forbidden patterns, dependency direction, protected files or docs.
**Cite the doc line.** This is the highest-credibility category available to you, because
it's the repo's standard rather than your taste.

## 14. Ownership

Does the diff trigger code-owner paths whose reviewers weren't requested? For sensitive
paths — production config, auth, pipelines, shared modules — say so explicitly.
