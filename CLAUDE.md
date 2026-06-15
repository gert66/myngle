# Claude Code Instructions for gert66/myngle

## Default language
Respond in Dutch unless the user explicitly asks for another language.

## Repository and branch
- Repository: gert66/myngle
- Default working branch: work
- Main is stable.
- Do not work on main unless the user explicitly asks.
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
