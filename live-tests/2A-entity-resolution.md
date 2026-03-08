# Live Test: SPEC-2A — Entity Resolution + Fact Deduplication

**Tenant ID (copy-paste):** `discord_364303223047323649`

**Prerequisites:**
- KERNOS running on Discord
- `VOYAGE_API_KEY` set in `.env` (enhanced path active)
- Bot is live (start with `./start.sh` or equivalent)

**Check enhanced path is active:**
```
source .venv/bin/activate
python -c "import os; from dotenv import load_dotenv; load_dotenv(); key = os.getenv('VOYAGE_API_KEY',''); print('Enhanced path: ACTIVE' if key else 'Enhanced path: INACTIVE (legacy hash-only)')"
```
Result: Enhanced Path active
---

## Step-by-Step Test Table

### Phase 1: Entity creation and alias resolution

**Step 1:** Send in Discord:
```
I need to call back two clients later at around 5:00 David Finchman 782-39-2328 and Tina Muchacha 328-583-5324
```
Expected: Agent responds naturally

**Step 2:** Check entities:
```
./kernos-cli entities discord_364303223047323649
```
OUTPUT:
./kernos-cli entities discord_364303223047323649
────────────────────────────────────────────────────────────
  Entities: discord_364303223047323649  (4 entities)
────────────────────────────────────────────────────────────

  Sarah Henderson (person) — colleague/legal collaborator
    id: ent_36038b5f
    phone: 142-555-9234
    first seen: 2026-03-07  last seen: 2026-03-07

  Greg (person) — friend
    id: ent_811b3b13
    first seen: 2026-03-07  last seen: 2026-03-07

  David Finchman (person) — client
    id: ent_cf14d913
    phone: 782-39-2328
    first seen: 2026-03-07  last seen: 2026-03-07

  Tina Muchacha (person) — client
    id: ent_1f0482f5
    phone: 328-583-5324
    first seen: 2026-03-07  last seen: 2026-03-07

Note: Extra entries are correct, from earlier in the conversation.
NOTE 2:  It seems the entries were not added immediately, to my interpretation they were added upon the NEXT communication from me.  
Example:
bananapancake — 2:35 PM
I need to call back two clients later at around 5:00 David Finchman 782-39-2328 and Tina Muchacha 328-583-5324
Kernos
APP
 — 2:35 PM
Got it — two callbacks around 5:00:

David Finchman — 782-39-2328
Tina Muchacha — 328-583-5324

bananapancake — 2:35 PM
What time is it now?
Kernos
APP
 — 2:36 PM
2:36 PM Pacific. You've got a little over two hours before those 5:00 callbacks.

In this case the new entities shown from ./kernos-cli entities discord_364303223047323649 showed up AFTER asking for the time.

---

### Phase 2: Contact info as deterministic resolution signal
Addressed enough above I believe

---

### Phase 3: Vague references do NOT create entities

**Step 7:** Send in Discord:
```
My friend told me about a great Italian restaurant near downtown
```
Expected: Agent responds normally.

**Step 8:** Check entities:
```
./kernos-cli entities discord_364303223047323649
```
Expected: Entity count UNCHANGED. No EntityNode for "friend." The restaurant fact may appear as a KnowledgeEntry.

Reuslt:  Nothing about the resturaunt

---

### Phase 4: Unique relationship roles DO create entities

**Step 9:** Send in Discord:
```
My wife loves Italian food, she would enjoy that place
```
Expected: Agent responds naturally.

**Step 10:** Check entities:
```
./kernos-cli entities discord_364303223047323649
```
Expected: EntityNode created for user's wife (unique relationship role) even without a name yet. `relationship_type: wife`.

None shown with following communication:
bananapancake — 4:23 PM
My wife loves Italian food, she would enjoy potatoland
Kernos
APP
 — 4:23 PM
Good to know — Potatoland could be a date night spot. What's her name?
bananapancake — 4:23 PM
Now what time is it?
Kernos
APP
 — 4:23 PM
4:23 PM Pacific. About 37 minutes until those 5:00 callbacks.

THEN with specifics:

bananapancake — 4:26 PM
My wife's name is Liana
Kernos
APP
 — 4:26 PM
Liana — got it. She loves Italian, Potatoland's on the list for a night out.

Now shows up:

  user's wife (person) — spouse
    id: ent_cb0bed64
    first seen: 2026-03-08  last seen: 2026-03-08


---

### Phase 5: Fact deduplication — NOOP reinforcement

**Step 11:** Send in Discord (repeat of Step 1's information):
```
I'm working with Sarah Henderson on a contract, she's a client
```
Expected: Agent responds naturally.

**Step 12:** Check knowledge:
```
./kernos-cli knowledge discord_364303223047323649 --subject Henderson
```
Expected: No new duplicate entry. Existing entry's reinforcement_count may be higher. Knowledge store does NOT grow from semantic duplicates.

---

### Phase 6: Fact UPDATE — supersedes chain

**Step 13:** Send in Discord:
```
Actually she moved her practice to Seattle last month
```
Expected: Agent acknowledges the update.
Yes

**Step 14:** Check knowledge with archived entries:
```
./kernos-cli knowledge discord_364303223047323649 --include-archived
```
Expected: If a previous "Portland" or "practice location" entry existed, it shows as `[archived]`. New Seattle entry shows `supersedes: know_xxx`.

Strangely I'm only seeing updates prior to this last update.  Nothing about the new names.  Claude code fixed this, new entries wern't showing up.  New output (topmost entries):

 [high] fact: "Moved her practice to Seattle last month" (2026-03-08)
    subject: Sarah Henderson | archetype: structural | R: 1.00 | salience: 0.50

  [high] fact: "Lives in Portland" (2026-03-08)
    subject: user | archetype: structural | R: 1.00 | salience: 0.70

  [high] fact: "Only wears purple — has a one-color wardrobe policy" (2026-03-08)
    subject: Sarah Henderson | archetype: identity | R: 1.00 | salience: 0.20

  [stated] fact: "client" (2026-03-08)
    subject: Sarah Henderson.relationship_type | archetype: structural | R: 1.00 | salience: 0.50

  [stated] fact: "client" (2026-03-08)
    subject: Sarah Henderson.relationship_type | archetype: structural | R: 1.00 | salience: 0.50

  [stated] fact: "Liana" (2026-03-08)
    subject: user's wife.name | archetype: structural | R: 1.00 | salience: 0.50

  [stated] fact: "Liana" (2026-03-08)
    subject: entity.name | archetype: structural | R: 1.00 | salience: 0.50

  [high] fact: "Loves Italian food; would enjoy Potatoland restaurant" (2026-03-08)
    subject: user's wife | archetype: habitual | R: 1.00 | salience: 0.30

  [high] preference: "Enjoys Italian food" (2026-03-08)
    subject: user's wife | archetype: habitual | R: 1.00 | salience: 0.50

  [high] fact: "Friend recommended an Italian restaurant called Potatoland near downtown" (2026-03-08)
    subject: user | archetype: contextual | R: 1.00 | salience: 0.20

  [high] fact: "Has plans to play cards with Greg tonight" (2026-03-07)
    subject: user | archetype: ephemeral | R: 1.00 | salience: 0.20

  [high] fact: "Needs a callback to discuss contract details and SR22 updates" (2026-03-07)
    subject: Sarah Henderson | archetype: contextual | R: 1.00 | salience: 0.50

  [high] fact: "Working with Sarah Henderson on a legal case" (2026-03-07)
    subject: user | archetype: contextual | R: 1.00 | salience: 0.50



---

### Phase 7: Present, don't presume (name collision)

**Step 15:** Send in Discord:
```
I met this girl named Sarah today, she seems really cool — not Henderson, different person
```
Expected: Agent does NOT auto-merge with Sarah Henderson. May ask naturally whether this is the same Sarah or someone new.

Chat:

bananapancake — 4:53 PM
I met this girl named Sarah today, she seems really cool — not Henderson, different person
Kernos
APP
 — 4:53 PM
Nice — two Sarahs in the mix now. What's this one's last name, or is she just "cool Sarah" for now?
bananapancake — 4:54 PM
I don't know
Kernos
APP
 — 4:54 PM
Cool Sarah it is, until further notice. 😄
bananapancake — 4:54 PM
WHat time is it?
Kernos
APP
 — 4:55 PM
4:54 PM.


**Step 16:** Check entities:
```
./kernos-cli entities discord_364303223047323649 

────────────────────────────────────────────────────────────
  Entities: discord_364303223047323649  (7 entities)
────────────────────────────────────────────────────────────

  Sarah Henderson (person) — colleague/legal collaborator
    id: ent_36038b5f
    phone: 142-555-9234
    first seen: 2026-03-07  last seen: 2026-03-08

  Greg (person) — friend
    id: ent_811b3b13
    first seen: 2026-03-07  last seen: 2026-03-07

  David Finchman (person) — client
    id: ent_cf14d913
    phone: 782-39-2328
    first seen: 2026-03-07  last seen: 2026-03-07

  Tina Muchacha (person) — client
    id: ent_1f0482f5
    phone: 328-583-5324
    first seen: 2026-03-07  last seen: 2026-03-07

  user's wife (person) — spouse
    id: ent_cb0bed64
    first seen: 2026-03-08  last seen: 2026-03-08

  Liana (person) — spouse
    id: ent_decd315d
    first seen: 2026-03-08  last seen: 2026-03-08

  Sarah Henderson (person) — colleague
    id: ent_306e5675
    first seen: 2026-03-08  last seen: 2026-03-08


```
Expected: A second Sarah entity exists OR a MAYBE_SAME_AS edge links them. The original Sarah Henderson is NOT merged with this new Sarah.

No new Sarah.

---

### Phase 8: Classification logging verification

**Step 17:** Check logs for classification decisions:
```
source .venv/bin/activate && python -m kernos.cli events discord_364303223047323649 --type entity.created --limit 10
────────────────────────────────────────────────────────────
  Events for discord_364303223047323649  (6 shown)
────────────────────────────────────────────────────────────

[2026-03-07T22:05:56.667294+00:00] entity.created  (evt_1772921156667261_d877)
  source: entity_resolver
  name: Sarah Henderson
  resolution_type: new_entity
  entity_id: ent_36038b5f

[2026-03-07T22:33:17.989877+00:00] entity.created  (evt_1772922797989847_95a2)
  source: entity_resolver
  name: Greg
  resolution_type: new_entity
  entity_id: ent_811b3b13

[2026-03-07T22:36:08.740106+00:00] entity.created  (evt_1772922968740057_fe1a)
  source: entity_resolver
  name: David Finchman
  resolution_type: new_entity
  entity_id: ent_cf14d913

[2026-03-07T22:36:08.858934+00:00] entity.created  (evt_1772922968858908_ba9c)
  source: entity_resolver
  name: Tina Muchacha
  resolution_type: new_entity
  entity_id: ent_1f0482f5

[2026-03-08T00:26:12.597435+00:00] entity.created  (evt_1772929572597388_bcf2)
  source: entity_resolver
  name: user's wife
  resolution_type: new_entity
  entity_id: ent_cb0bed64

[2026-03-08T00:27:24.032456+00:00] entity.created  (evt_1772929644032419_1881)
  source: entity_resolver
  name: Liana
  resolution_type: new_entity
  entity_id: ent_decd315d


```
~/Kernos$ source .venv/bin/activate && python -m kernos.cli events discord_364303223047323649 --type knowledge.reinforced --limit 10
────────────────────────────────────────────────────────────
  Events for discord_364303223047323649  (7 shown)
────────────────────────────────────────────────────────────

[2026-03-07T22:25:53.002690+00:00] knowledge.reinforced  (evt_1772922353002656_f9eb)
  source: fact_dedup
  entry_id: know_1772921156762040_cb1a
  reinforcement_count: 2

[2026-03-07T22:33:22.727521+00:00] knowledge.reinforced  (evt_1772922802727479_f661)
  source: fact_dedup
  entry_id: know_1772922784088832_ee55
  reinforcement_count: 2

[2026-03-08T00:23:31.216265+00:00] knowledge.reinforced  (evt_1772929411216226_4155)
  source: fact_dedup
  entry_id: know_1772929247278842_4b61
  reinforcement_count: 2

[2026-03-08T00:26:15.575989+00:00] knowledge.reinforced  (evt_1772929575575952_0938)
  source: fact_dedup
  entry_id: know_1772929435329858_d44b
  reinforcement_count: 2

[2026-03-08T00:26:18.940373+00:00] knowledge.reinforced  (evt_1772929578940336_fde3)
  source: fact_dedup
  entry_id: know_1772929435329858_d44b
  reinforcement_count: 3

[2026-03-08T00:34:18.534255+00:00] knowledge.reinforced  (evt_1772930058534204_a87b)
  source: fact_dedup
  entry_id: know_1772929787572569_e08d
  reinforcement_count: 2

[2026-03-08T00:34:18.642993+00:00] knowledge.reinforced  (evt_1772930058642978_1ca0)
  source: fact_dedup
  entry_id: know_1772930058543115_9323
  reinforcement_count: 2


---

## Quick Reference Commands

```bash
# View all entities
./kernos-cli entities discord_364303223047323649

# View entities including inactive
./kernos-cli entities discord_364303223047323649 --include-inactive

# View knowledge about Henderson
./kernos-cli knowledge discord_364303223047323649 --subject Henderson

# View all knowledge including archived
./kernos-cli knowledge discord_364303223047323649 --include-archived

# View entity events
./kernos-cli events discord_364303223047323649 --type entity.created
./kernos-cli events discord_364303223047323649 --type entity.merged
./kernos-cli events discord_364303223047323649 --type entity.linked

# View reinforcement events
./kernos-cli events discord_364303223047323649 --type knowledge.reinforced
```

---

## Troubleshooting

**No entities appear after sending messages:**
- Check VOYAGE_API_KEY is set: `grep VOYAGE_API_KEY .env`
- Check bot logs for "entity resolution services" initialization message
- If VOYAGE_API_KEY is missing, enhanced path is inactive — entity resolution won't run

**Entity count keeps growing (no deduplication):**
- Embeddings may be failing; check logs for "embedding failed" warnings
- NOOP zone requires cosine similarity > 0.92 — very different phrasings may legitimately ADD

**All facts being skipped (zero knowledge entries):**
- Check EXTRACTION_SCHEMA accepts the new `relationship_type`, `phone`, `email` fields in entities
- Run: `source .venv/bin/activate && python -c "from kernos.kernel.projectors.llm_extractor import EXTRACTION_SCHEMA; print('Schema OK')" `

**"present_not_presume" not firing:**
- Only triggers when strong new-person signals ("met today", "just met", "seems nice") are in context AND an entity with that exact name already exists
- Test requires an existing entity first — complete Steps 1-4 before testing Step 15
