---
inclusion: always
---
# Fidelity — recreate from the source, never invent

Applies whenever the task is to **recreate** something: existing source code, an AWS COE, a public
post-mortem, a blog post, an architecture diagram, meeting transcripts or notes, or the user's own
description of a scenario. Whatever was supplied is the source of truth.

## The rule

- **Mechanisms and behaviour come from the source.** That covers what calls what, timeouts,
  retries, caching, concurrency bounds, failure triggers and how a failure propagates, and also
  what the system *lacks*. Never invent a mechanism, process, or fix to fill a gap, however
  reasonable it looks.
- **Anything uncertain means research or a question, not an invention.** Read the source again,
  search for it, or ask the user. When the source is the user's own description, ask the user.
- **Deployment-only details the source is silent on** (instance sizes, CIDRs, retention, counts)
  may use a sensible default, recorded as a deviation. A conscious simplification ("fewer hosts
  than prod, to make the tier saturate") is fine; an invented mechanism is not.
- **Every deviation from the source is written down** in `docs/fidelity.md`.

An invention that "makes the failure work" is worse than useless: the recreation then teaches the
wrong lesson. One demo shipped a connection semaphore and a two-tier rate limiter that the real
system never had (the *absence* of a gate was the actual finding); both had to be ripped out.

## `docs/fidelity.md`

Keep it current as you work. Three sections:

- **Sources** — what was supplied, with links, IDs, commits, file paths, and dates.
- **Deviations** — for each: what differs, why, and what the source says.
- **Open questions** — what is unresolved and blocks a faithful recreation.

Cite the source next to each mechanism in the design, e.g. `Repo@<commit> path/File.java:120`, a
config default, a COE section, a post-mortem paragraph, or a transcript timestamp.

## Mechanism checklist

Resolve these from the source, with citations, before implementing (in PDD's research phase):

- **Timeout semantics** — total-call deadline vs read-idle vs connect; exact value and where it is
  set. A slow streaming response never trips a read-idle timeout.
- **Retry policy** — interval, backoff (fixed / exponential / jitter), attempt caps. The absence
  of backoff is often the amplifier, and is itself a finding.
- **Caching and dedup** — what is cached, TTL, invalidation scope (one key vs everything), whether
  concurrent misses are coalesced. Check it existed *at the time*: a post-incident fix is not
  faithful.
- **Concurrency bounds** — the real limit (thread pool, connection pool, none). Find it; don't add one.
- **Trigger and propagation** — what starts it and the exact path by which a local problem becomes
  a wide one.