# CLAUDE.md

Instructions for Claude when working in this project.

## Before starting work

- **Show approaches first.** Before any significant task, present 2-3 possible approaches. Wait for my choice before proceeding.
- **Read MEMORY.md first.** At the start of every session, read `MEMORY.md` before doing anything else.

## Project context

Apply this context to every task. When something doesn't fit this picture, flag it before proceeding.

- **Project:** Options Extractor
- **Goal:** Extract the options data from filling report and save it in excel 
- **Audience:** equity analyst
- **Tone:** professional polite
- **What to avoid:** very older data and dont change the mathematical values

## Scope discipline

- Only change what I specifically asked you to change. Do not rewrite, rephrase, refactor, rename, or "improve" anything I did not explicitly ask about — even if you think it would be better.
- Only modify files, functions, and lines of code directly related to the current task.
- If you notice something worth fixing or improving elsewhere, mention it at the end. Do not touch it unless I explicitly ask. Ever.

## Response style

- Match response length to task complexity. Simple questions get short, direct answers. Complex tasks get full, detailed responses.
- Never pad responses with restatements or closing sentences that repeat what you just said.

## End-of-task summaries

**After any writing/editing task**, end with a brief status update (not a recap):
- What was changed
- What was left untouched
- What needs my attention

**After any coding task**, end with a brief status update (not a recap):
- Files changed — what was modified, one line per file
- Files intentionally not touched
- Follow-up needed

## MEMORY.md

Maintain a file called `MEMORY.md`.

- After any significant decision, add an entry: what was decided, why, and what was rejected.
- When I say **"session end"** or **"let's stop here"**, write a session summary to `MEMORY.md`:
  - What we worked on
  - What's completed
  - What's in progress
  - What decisions were made
  - What to pick up next session
