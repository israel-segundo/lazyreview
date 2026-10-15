# Repo profile (Step 2)

Save to `$WORK/repo-profile.md`. Keep it to about a page — it's a working note, not a
report. Its purpose is to replace your generic assumptions with the repo's actual rules
before you judge anything.

## What to extract

**Hard rules.** The repo's own MUST/NEVER list: banned imports, APIs, or libraries;
forbidden patterns; layering or dependency direction; style policy; protected files and
docs. These become checkable items in the Step 6 scan, cited by doc line — the strongest
findings you can make.

**Definition of done.** What the repo says a PR must include: green checks, docs updated,
tests at a stated level, changelog entry, migration notes, ticket link.

**Architecture invariants and their reasons.** The constraints the repo says not to break,
*and why*. The reason tells you what a violation actually costs — and a documented reason
that still holds means the convention is not a finding, however odd it looks.

**External contracts.** Names the outside world depends on (see `safety-scan.md` §3). A doc
saying "this is a contract, don't rename" is gold — quote it.

**Ownership.** Code-owner rules the diff triggers, and whether those reviewers were
actually requested on this PR.

**The verification story.** Fill in: full-check command; single-test command; language
toolchain and any version pin; whether checks need external services or credentials; which
CI checks gate merge. If nothing runs locally, record that explicitly.

## Where to look

```sh
ls
cat CLAUDE.md AGENTS.md 2>/dev/null            # agent instructions: highest-authority rules
cat CONTRIBUTING.md docs/CONTRIBUTING.md 2>/dev/null
ls docs/ 2>/dev/null; head -60 README.md
cat .github/pull_request_template.md .github/PULL_REQUEST_TEMPLATE.md 2>/dev/null
cat .github/CODEOWNERS 2>/dev/null
ls .github/workflows/ 2>/dev/null
find . -maxdepth 3 \( -name 'CLAUDE.md' -o -name 'AGENTS.md' \) | grep -v node_modules
ls .claude/skills/ .agents/skills/ 2>/dev/null  # a repo-local skill beats your inference
```

Nested instruction files matter: a `CLAUDE.md` or `AGENTS.md` deeper in the tree usually
carries the rules for the subsystem the PR actually touches, and often contradicts the
root file on purpose.

## Detecting the verification story

**The repo's documented command always beats your inference.** Check its instruction files
and README first; only fall back to markers. Also honor prohibitions — if the docs say
never to invoke the test runner directly, or never to start a local service, don't.

| Marker | Full check | Single test |
|---|---|---|
| `package.json` | `npm test`, or the repo's `verify`/`check` script | `npm test -- --testPathPatterns=<x>` (Jest 30+ is plural; older is `--testPathPattern`) |
| `pom.xml` | `mvn -B verify` | `mvn -B test -Dtest=<Class>` |
| `build.gradle`, `build.gradle.kts` | `./gradlew check` | `./gradlew test --tests '<Class>'` |
| `Cargo.toml` | `cargo test` | `cargo test <name>` |
| `go.mod` | `go test ./...` | `go test ./pkg -run <Name>` |
| `pyproject.toml`, `tox.ini` | `pytest`, `uv run pytest`, `tox` | `pytest path::test_name` |
| `Gemfile` | `bundle exec rspec` | `bundle exec rspec path:LINE` |
| `mix.exs` | `mix test` | `mix test path:LINE` |
| `*.csproj`, `*.sln` | `dotnet test` | `dotnet test --filter <Name>` |
| `composer.json` | `composer test`, `vendor/bin/phpunit` | `phpunit --filter <Name>` |
| `Makefile`, `justfile` | `make test`, `make verify`, `just test` | per the target |
| `*.tf`, `*.tofu`, charts, k8s manifests | **often nothing runnable locally** — CI is the only evidence | — |

Note the toolchain pin too (`.nvmrc`, `.tool-versions`, `java.version`, `rust-toolchain`,
`python-version`). A wrong runtime version is the single most common cause of a false red,
and reporting one as a finding damages the whole review's credibility.
