---
inclusion: always
---
# This project is a Customer Sandbox

**Purpose:** recreate a customer's environment from what we have (meeting transcripts, notes,
architecture diagrams), so we can explore how their system works, troubleshoot it, and identify
challenges or improvements against something real. **The deployed environment is the deliverable.**

## How to build it

- Build what the customer described, including their suboptimal choices. Don't fix it toward best
  practice: seeing their system as it is, is the point.
- The fidelity rules apply, with the customer's material as the source. Where it is silent on a
  mechanism, ask; where it is silent on a deployment-only detail, use a default and record it as a
  deviation in `docs/fidelity.md`.

## Example

A customer said they couldn't set up metrics for a specific use case, and that this caused a
failure in their system. That didn't sound right. The sandbox recreates their setup, so we can
establish against the live account which metrics and logs actually exist, and how to use them.
