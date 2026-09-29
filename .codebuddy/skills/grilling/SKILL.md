---
name: grilling
description: Grill the user relentlessly about a plan, decision, or idea. Use when the user wants to stress-test their thinking, or uses any 'grill' trigger phrases.
---

Interview the user relentlessly until a shared understanding is reached. Map this as a **design tree**: every decision branches into the decisions that hang off it.

Work the tree in **rounds**. The **frontier** is every decision whose prerequisites are already settled: the decisions that can be resolved _now_ without guessing at answers not heard yet. Ask the whole frontier in one round: number each question and give a recommended answer. Then wait for the user's answers before the next round.

Apply a **confidence rule** to every decision on the frontier: if a recommended answer can be given with **≥90% confidence**, adopt it as settled directly and do not ask the user for it. List adopted assumptions in a single line before the round's questions so the user can deny any of them. Only decisions with confidence below 90% become questions for the user.

Format a round like so:

```
❓ **Q1** - **<question title>**: <question body, might be multiple paragraphs, including multiple choices>

➡️ <your recommended answer>

---

❓ **Q2** - **<question title>**: <question body, might be multiple paragraphs, including multiple choices>

➡️ <your recommended answer>
```

Each round the user answers reshapes the tree: settled decisions push the frontier outward and unblock questions that depended on them. Recompute the frontier and ask the next round. A question whose answer depends on another question still open in this round belongs to a _later_ round, not this one.

Finding _facts_ is the agent's job, never the user's. When a frontier question needs a fact from the environment (filesystem, tools, etc.), look it up with the available tools instead of asking the user for anything that can be looked up yourself. Don't block on it: a running exploration is an unsettled prerequisite, so only the questions downstream of it wait for the exploration to report; ask the rest of the frontier now. The _decisions_ below 90% confidence are the user's: put each to them and wait.

The session is done when the frontier is empty: every branch of the design tree visited, nothing left silently assumed. Do not act on it until the user confirms a shared understanding has been reached, including any directly adopted assumptions.