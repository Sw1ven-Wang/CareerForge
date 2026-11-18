# Atomic continuation for new evidence-only answers

New no-funding turns receive the server-owned
`evidence_continuity_contract: evidence-answer-continuity-1.6.6` marker. Public
TurnInput/HTTP/CLI cannot choose this value. Admission discards inherited values;
removing or changing it after a plan is frozen fails the plan binding check.
Already-admitted plans without this marker retain their historical execution
and hashes. They are not silently rewritten or given a reconstructed intent.
Funded turns and navigation/action paths retain their existing contracts.

For the new marked factual path, the checked evidence-only plan also freezes
the original continuation projection: topic keys, retrieval keys/hints/scope,
source IDs, task type, original focus revision and last-turn ID. These fields
are the same ones ordinary completion uses. They guide a later question such as
“continue”; they do not add fact authority, source scope, or learner mastery.

The terminal answer transaction now commits all required no-run tail effects:

1. The final bound question outcome and its evidence KnowledgeGap, if any.
2. The existing rollout operational observation, including its existing canary
   regression/stop decisions. The observation remains unique per rollout/turn.
3. The continuation focus projection, only when the conversation revision still
   matches the completed turn's expected successor revision.
4. Turn completion and the answer.completed event.

There is no AgentRun result stage on this path because no AgentRun exists. No
fake run, receipt, call, learning event or mastery observation is introduced.

The operational latency value is measured by the server from the current
processing attempt through the final assessment and saved with the explicit
`processing_attempt_through_assessment` scope. A retry reuses that saved value;
it does not invent latency, add crash downtime, or duplicate the observation.
This is an operational monitor, not an evaluation of learning effectiveness.

Both normal delivery and interrupted recovery use the same transaction.
Completed replay is a no-op because its continuation effects are already durable.
A later real turn or newer focus revision is never overwritten. Original-question,
selection, publication, policy and privacy-generation checks remain in force.
Operational unverified answers still report task_satisfied=null and never create
an evidence-build gap just because completeness could not be checked.

This bounded change does not generalize atomicity to funded, navigation, coding,
planning or other action branches. Their old contracts are unchanged. It does
not migrate already frozen RC8 records. No database migration or public outcome
schema change is needed. Replay engine identity now includes the continuity
helper, conversation implementation and rollout monitor bytes.
