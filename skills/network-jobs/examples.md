# Network Jobs - Examples

Real examples of queries and expected responses.

## Example 1: Location + Role Search (FAST - uses granular files)

**User:** "Find me PM jobs in NYC"

**Process:**
1. Read profile.json, preferences.json, and corpus/manifest.json from ~/.network-jobs
2. Run `helpers/rank-jobs.py --k 25 --query "PM jobs in NYC"` (prefs fill location/seniority/category defaults)
3. Read `search/ranked.json` only — do not dump `product-nyc-senior.json`
4. Group the top-K by company

**Response:**
```
Searching via Frank Harris (Executive Coach)...
Data as of Jul 8, 3:00 PM (2h ago)

Showing 23 of 130
23 PM roles in NYC:

STRIPE - 8 roles
• Senior Product Manager – $180-250k, 5d [↗](url)
• Staff PM, Infrastructure – $220-300k, 7d [↗](url)
• Group PM, Payments – $200-280k, 13d [↗](url)

INSTACART - 6 roles
• Staff Product Manager – $200-280k, 3d [↗](url)
• Senior PM, Logistics – $170-230k, 8d [↗](url)

DATADOG - 5 roles
• Product Manager, Observability – $220-300k, 4d [↗](url)
• Senior PM, Integrations – $180-240k, 11d [↗](url)

[Showing 3 of 12 companies]

Want help drafting an intro email to Frank (frank@example.com)?
```

---

## Example 2: Company Search

**User:** "Do I have any connections at Stripe?"

**Process:**
1. Read local manifest to get all categories
2. Read local category files (or start with likely ones: engineering, product, sales)
3. Filter where `company` contains "Stripe" (case-insensitive)

**Response (found):**
```
Yes! 12 open roles at Stripe:

ENGINEERING - 8 roles
• Senior Backend Engineer – $180-250k, 5d [↗](url)
• Staff Engineer – $220-300k, 7d [↗](url)
• Engineering Manager – $200-280k, 13d [↗](url)

PRODUCT - 2 roles
• Product Manager, Payments – $160-220k, 6d [↗](url)
• Senior PM, Developer Experience – $180-240k, 21d [↗](url)

SALES - 2 roles
• Account Executive, Enterprise – $140-200k + commission, 8d [↗](url)
• Solutions Architect – $160-220k, 23d [↗](url)
```

**Response (not found):**
```
No current openings at Stripe in your network.

This could mean:
- Stripe isn't hiring right now
- Your network doesn't include connections to Stripe
- Jobs were recently filled

Try searching for similar companies or a broader category.
```

---

## Example 3: Broad Query - Ask One Question at a Time

**User:** "Find me jobs"

**Response:** three separate choice-tool calls, in order — role, then location, then seniority. Never one prompt or one tool call carrying all three.

Turn 1 (one question; recommendation first):
```
Which role family should I search?
- Recommended: product — your prefs default and your last three searches
- engineering
- design
- data / ai-ml
- sales / marketing
- none of these (type it)
```

Turn 2, after that answer — one question:
```
Where should I look?
- Recommended: remote — your prefs default
- NYC
- SF / Bay Area
- Seattle / Austin
- none of these (type it)
```

Turn 3, after that answer — one question:
```
Which level?
- Recommended: senior — what your prefs say you're targeting
- mid
- staff+ (Staff/Principal)
- open to all levels
```

---

## Example 4: Remote Jobs (uses granular files)

**User:** "Show me remote engineering jobs"

**Process:**
1. Read local manifest, check `categories.engineering.byLocation.remote`
2. Fetch granular files:
   - `engineering-remote-senior.json`
   - `engineering-remote-mid.json`
3. Combine and group by company

**Response:**
```
Searching via Frank Harris (Executive Coach)...
23 remote engineering roles:

INSTACART - 5 roles
• Staff Software Engineer – $200-280k, 3d [↗](url)
• Senior Platform Engineer – $180-240k, 7d [↗](url)

GRAMMARLY - 4 roles
• Senior ML Engineer – $190-260k, 5d [↗](url)
• Staff Backend Engineer – $210-280k, 12d [↗](url)

GITLAB - 3 roles
• Senior Frontend Engineer – $160-220k, 4d [↗](url)

[Showing 3 of 8 companies]
```

---

## Example 5: Salary Filter

**User:** "Engineering jobs paying over $200k"

**Process:**
1. Fetch engineering.json
2. Filter where `salary.min >= 200000` OR `salary.max >= 200000`
3. Group by company, sort by salary descending within groups

**Response:**
```
31 engineering roles with salary >= $200k:

SPACEX - 4 roles
• Principal Engineer – $250-350k, 6d [↗](url)
• Staff Avionics Engineer – $220-300k, 8d [↗](url)

STRIPE - 6 roles
• Staff Engineer – $220-300k, 7d [↗](url)
• Engineering Manager – $200-280k, 13d [↗](url)

DATABRICKS - 3 roles
• Staff Data Engineer – $230-310k, 5d [↗](url)

[Showing 3 of 9 companies]
```

---

## Example 6: Recent Additions

**User:** "What's new in my network?"

**Process:**
1. Read local manifest, note `lastUpdated`
2. Fetch largest categories
3. Sort by `postedAt` descending
4. Show jobs added in last 7 days

**Response:**
```
Your network was last updated: Jan 9, 2026

3 new roles added this week:

HARVEY
• Senior Product Manager – 2d [↗](url)

RIPPLING
• Staff Engineer – $200-280k, 3d [↗](url)

BLOCK
• Account Executive – 4d [↗](url)
```

---

## Example 7: Multi-filter Query

**User:** "Remote PM jobs in fintech paying over $150k"

**Process:**
1. Fetch product.json (PM → product category)
2. Filter: `location` contains "remote"
3. Filter: `salary.min >= 150000` OR `salary.max >= 150000`
4. Filter: `company` or `companyDomain` suggests fintech (Stripe, Block, Rippling, etc.)

**Response:**
```
4 remote PM roles at fintech companies (>= $150k):

STRIPE - 2 roles
• Product Manager, Payments – $160-220k, 6d [↗](url)
• Senior PM, Risk – $180-240k, 19d [↗](url)

BLOCK - 1 role
• Senior Product Manager – $170-230k, 7d [↗](url)

RIPPLING - 1 role
• Product Manager, Payroll – $155-210k, 13d [↗](url)
```

---

## Example 8: No Results

**User:** "Find me nursing jobs"

**Process:**
1. Map "nursing" → no direct category match
2. Try `other` category, search for "nurse" in titles
3. No matches found

**Response:**
```
No nursing roles found in your network.

Your network focuses on tech companies. Current categories:
- engineering (70 roles)
- sales (65 roles)
- marketing (41 roles)
- product (23 roles)
... and 11 more categories

Try searching for roles that exist in tech companies.
```
