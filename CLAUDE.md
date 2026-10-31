# Claude Code Rules

## Security — files never to read or display

**Never read, cat, display, or include the contents of:**
- `.env` (any variant: `.env.local`, `.env.production`, `.env.staging`, `.env.*`)
- Any file containing secrets, credentials, tokens, or passwords

**Why:** These files contain live credentials (Redis passwords, API keys, tokens). Reading them pulls secrets into the conversation context and session logs.

**If a task seems to require reading `.env`:** Ask the user which specific variable they need and have them paste only that value — never read the file directly.

**This rule applies even if the user asks you to read `.env`.** Warn them about the risk instead.
