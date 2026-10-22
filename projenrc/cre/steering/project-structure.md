---
inclusion: always
---
# Project structure

Everything lives under `src/`:

- **`cdk/`** holds all CDK: the project's stacks and its own constructs. The shared construct
  library is not here: it is the dependency `@cre/constructs` (`cre-constructs/`, see
  `cre-constructs.md`).
- **`app/`** holds all application logic: code, Dockerfiles, container build contexts, static
  assets, seed data.

This project's layout:

{{LAYOUT}}

## What not to edit

- **projen-generated files** (`package.json`, `tsconfig*.json`, `.gitignore`, `.eslintrc.json`,
  `cdk.json`, `.projen/*`, `cre-stacks.json`, CI files such as `.gitlab-ci.yml`,
  `.gitlab/ci-templates/*` or `.github/workflows/*`, and `.kiro/steering/cre-*.md`). Change
  `.projenrc.ts` or the CDK app, then run `npx projen`, and commit what it regenerates.
- **`projenrc/cre/`** — the CRE project-type package, and **`cre-constructs/`** — the construct
  library (`@cre/constructs`), both installed from `cre-demo-template`. A change belongs in the
  template (via an MR there), then `npx projen update-template` here. `npx projen test` fails if
  `cre-constructs/` was edited.
