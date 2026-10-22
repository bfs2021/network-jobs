---
name: intro-email-generator
description: Crafts compelling, forwardable introduction emails for intro requests. Use when user needs help writing an email asking a mutual connection to introduce them to someone at a company that's hiring. The skill produces emails that can be forwarded without editing.
license: MIT
metadata:
  version: "1.3.0"
---

# Intro Email Generator

## How Intro Requests Work

This skill helps with a specific scenario involving three people:

1. **The Job Seeker** — Looking for an opportunity at a target company
2. **The Forwarder** — Knows both the job seeker AND someone at the target company
3. **The Target Contact** — Works at the company that's hiring

**The flow:**
1. Job Seeker writes email addressed to The Forwarder
2. The Forwarder receives it, adds a quick note ("Hey, worth a look!"), and forwards to The Target Contact
3. The Target Contact sees a well-crafted pitch they can act on

**CRITICAL: The email is addressed to The Forwarder, NOT The Target Contact.** The magic is that it reads well when forwarded — The Forwarder doesn't need to rewrite anything.

## Common Mistake

People often write the email addressed to the wrong person. They write to the target contact instead of the forwarder.

**Wrong:** "Hi Sarah, I saw that Acme Corp is hiring and I'd love to chat about the PM role..."
(This is written to the target contact — the forwarder can't just forward this)

**Right:** "Hey Mike, I'm interested in the PM role at Acme Corp. Would you be open to connecting me with Sarah?"
(This is written to the forwarder — they can forward it directly to Sarah)

## Asking the user

Whenever the next step is picking from a known set, use the host's choice tool —
`AskUserQuestion` (Claude Code), `question` (opencode), `ask_user_choice`, or the
harness equivalent — instead of a prose list.

- **One question per call.** Ask it, wait for the answer, then ask the next
  (which role first, then which forwarder). Never bundle two questions into one
  prompt or one tool call, even when the tool accepts several.
- **Recommend, and say why.** Lead with the option you would pick and a one-line
  reason ("Recommended: Mike — he's the strongest tie you named and overlaps the
  PM title").
- Put the deciding data in the option label (fit score, company, title overlap),
  never in surrounding prose the user has to correlate.
- Always keep a free-text / "none of these" path.
- No such tool in this host? Use a short numbered list, recommendation first,
  never more than 4 options.

## Required Inputs

Gather before generating:
- **Resume** — prefer `~/.network-jobs/resume/text.md` (or `source.*`); else uploaded file, pasted text, or URL
- **Ranked intros** — run the helper first (joins **full** `connections.json`, not the 10-person sample):

```bash
network-jobs intros --k-roles 2 --k-forwarders 2
DATA="${NETWORK_JOBS_HOME:-$HOME/.network-jobs}"  # always resolve from the env; never export a literal DATA
cat "$DATA/search/intros.json"
```

  Use at most **1–2 roles** and **1–2 forwarders**. `fetchJdUrls` is the only set of job URLs to fetch. If a JD fetch fails, draft from `department` (and title) — do not scrape every posting.
- **Profile** — `~/.network-jobs/profile.json` for seeker name/email when present
- **Additional context** (optional) — key achievements, why they're interested, specific angles to emphasize

```bash
DATA="${NETWORK_JOBS_HOME:-$HOME/.network-jobs}"  # always resolve from the env; never export a literal DATA
cat "$DATA/profile.json"
cat "$DATA/resume/text.md" 2>/dev/null || ls -la "$DATA/resume"
```

If resume is missing, ask **one question**: run `network-jobs profile import <file>` (recommended) or paste the text now.

## Email Structure

```
Subject: Quick intro request - [Company] [Role]

Hey [Forwarder's first name],

[Opening: 2-3 sentences connecting a verified achievement to the company's specific challenge. Show understanding of what the company does.]

Quick background:
• [HIGH PRIORITY: Most relevant achievement with metric + context]
• [MEDIUM PRIORITY: Supporting achievement with metric + context]
• [Additional evidence: Leadership, scale, or relevant experience]

[Interest statement: Connect strongest achievement to company's specific needs]

Would you be open to connecting me with someone on the [Team] team? I'm interested in this role: [job URL]

Best,
[Job Seeker's name]
```

## Analysis Approach

1. **Extract company context** from job description: stage, core problems, team structure, success metrics
2. **Map candidate experience** to job requirements: find overlaps, note gaps, identify unique value
3. **Verify claims** against resume: only use achievements that have evidence
4. **Select style**:
   - DATA-DRIVEN: When context emphasizes metrics, growth, analytics ("achieved 250% growth...")
   - ACTION-DRIVEN: When context emphasizes building, launching, leadership ("after launching from 0-to-1...")

See [reference.md](reference.md) for detailed analysis framework.

## Quality Standards

- Every metric must be verified against resume
- Use company/product names, never "you" or "your" (bad: "your platform" → good: "the Amplitude platform")
- Bullets use "• " format, max 12 words each
- Total body under 150 words
- Include job URL in closing (or job title if URL unavailable)
- Tone: professional but conversational, not templated

## Example

**Request:** "I want to ask my friend Mike to intro me to someone at AnalyticsCo for their PM role."

**Generated Email:**
```
Subject: Quick intro request - AnalyticsCo Product Manager

Hey Mike,

Having achieved 250% WAU growth in our analytics product, I appreciate the scale of AnalyticsCo's platform challenges. My experience optimizing retention metrics showed me how critical accurate data is to product decisions, and I see direct parallels with how AnalyticsCo helps companies turn data into insights.

Quick background:
• Led analytics product growth from 10K to 35K WAU over 18 months
• Redesigned retention tracking, improving engagement 40% YoY
• Managed 4-person team through Series B product overhaul

Based on my experience scaling analytics products, I see valuable opportunities to help AnalyticsCo expand their enterprise offering.

Would you be open to connecting me with someone on the Product team? I'm interested in this role: https://analyticsCo.com/jobs/pm

Best,
Alex
```

Mike can now forward this directly to his contact at AnalyticsCo.

For more examples, see [examples.md](examples.md).

## Related Skills

**network-jobs** - Search job openings at companies where you have connections through your network. Use this skill first to find opportunities, then use intro-email-generator to craft the outreach email.

## Integration with network-jobs

If the user found a job using the network-jobs skill:
- Prefer explicitly reviewed roles: `network-jobs intros --title "..."` /
  `--url ...` / `--company ...` (joins `search/ranked.json` + `connections/connections.json`).
  Bare `network-jobs intros` takes the tail of the last search — fine as a
  default, but ask **one question** first when the user has reviewed specific
  roles (use the reviewed role, recommended, or the last-search tail).
- Forwarder order measures **title overlap only, never relationship
  strength** (every connection is 1st degree; closeness isn't in the data).
  Ask who the user actually knows well at that company — **one question per
  company**, recommending the strongest title overlap but saying plainly that
  the data cannot rank closeness — and pin their answer with
  `intros --prefer "Name"` (repeatable). Never present the top scorer as
  their closest tie.
- Confirm 1–2 roles and 1–2 forwarders with the user, following
  **Asking the user** above: **one question per call** — role first (group by
  family → specific role, or company → role), wait for the answer, then ask
  which forwarder. Offer multi-select only where several answers are genuinely
  valid. Lead with your recommended role/forwarder and a one-line reason, put
  the deciding data in the option labels (fit score, company, title overlap,
  connection count) — never in surrounding prose the user must correlate — and
  always keep a free-text path for anything not listed. Never bundle the role
  and forwarder questions into one prompt or one tool call; prose satisfies the
  letter of "confirm" but costs the user a summarization task.
- Fetch JD **only** for `fetchJdUrls` (or skip and use `department`)
- Job seeker name/email may be in ~/.network-jobs/profile.json
- Generate the intro email using the job details + resume
- After the exact text is approved, finish in-mail when possible: if the
  host exposes a mail/compose capability (Gmail MCP, Outlook/Graph, or a
  provider-agnostic compose tool), create a **draft** — `To` = forwarder's
  address on file, subject + approved body, JD as a link. If the forwarder
  has no email on file, do not guess one: report that no draft could be
  addressed and fall back to text. **Never send** — draft only, always, and
  only after the user approves the exact text. With no mail capability
  available, emit the text plus a one-line note that it pastes into any
  mail client.

## Helpers

- `network-jobs intros` (wraps [`helpers/rank-intros.py`](helpers/rank-intros.py)) — 1–2 roles + 1–2 forwarders → `$DATA/search/intros.json`
