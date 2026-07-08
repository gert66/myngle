# Claude Code Instructions for gert66/myngle

## Default language
Respond in Dutch unless the user explicitly asks for another language.

## Repository and branch
- Repository: gert66/myngle
- Default working branch: main (the `work` branch no longer exists — it was
  merged into `main` and deleted; `main` is now the single trunk).
- Do not create a new branch unless the user explicitly asks.
- Do not rename branches.
- Do not open pull requests unless the user explicitly asks.
- Do not merge anything unless the user explicitly asks.

## Startup checks
Before starting any task, run:

```bash
git remote -v
git branch --show-current
git status
