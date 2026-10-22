---
inclusion: always
---
# This project is a Resilience Demo

**Purpose:** demonstrate a product, solution, or pattern as a reference for what good looks like.

**How to build it:** move fast by reusing what already works.

- Use the vetted construct library (`@cre/constructs`; what is in it and when to use each:
  `cre-constructs.md`) and the provided CI/CD and deploy tasks. Write a new construct only when nothing fits, and say why in
  the design.
- When the demo recreates an existing pattern or sample (e.g. from the template's `patterns/`),
  the fidelity rules apply to it.

**Failure injection** is how the demo shows the solution working. Use it for any of:

- the pattern absorbing a fault with no customer impact;
- a failure followed by a successful recovery;
- a failure made visible by the observability.

**Dashboards are required.** The design names each dashboard and what it shows, and the demo is not
finished without them. They are not a build check: the build has to pass in the phases before
observability is built.