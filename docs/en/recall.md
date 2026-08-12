# Recall and injection

Long-term recall uses Topic as the main memory, adds non-duplicative facts and formal fragments, and falls back to Timeline when Topic is unavailable. The current private user's profile follows an independent loading route and never enters Topic candidate retrieval.

![Topic-first recall](../assets/images/recall-flow-en.svg){.diagram}

## Query branches

The current user message has weight `1.0` and must qualify a candidate independently. Recent user and optional assistant messages provide bounded context support but cannot promote an unrelated Topic by themselves.

## Candidate qualification and reranking

Embeddings and keywords establish eligibility and base relevance. Optional Rerank reorders qualified candidates; its rank bonus is reduced when the model shows little score separation. Relative floors, actor bonuses, context-overlap suppression, MMR, and dynamic stopping can return fewer than `k` results.

## Injection assembly

The system injects the Topic body first, then facts and fragments that add information. If a fragment body duplicates its parent Topic, the body is omitted while unique facts remain eligible. Repeated paragraphs and facts are merged before final injection.

An actual Topic injection forwards bounded access contribution to source Timeline memories, keeping the original lifecycle mechanism informed.

## Current-user profile injection

Profile injection requires a stable private actor from the current request and resolves an exact Bot-account, current-persona, and logical-user scope. Group chats, unstable identities, and arbitrary-user lookups cannot return a profile. An explicitly configured objective-profile share group may reuse a fact namespace, but persona relationships always remain in the current persona scope.

This route is independent of Topic/Timeline recall. A profile may still inject when ordinary long-term recall is disabled, fails, or returns no result. The objective section selects only current `active` facts; `pending`, `conflict`, `stale`, archived, and excluded facts never inject. The relationship receives its own priority budget, and unused fact or relationship capacity flows to the other side.

Defaults cap the whole profile at 800 characters and give the relationship a 350-character priority budget. The final block is marked data-only and states that the current message and conversation take precedence. Injection mode, total budget, relationship budget, and per-fact limits remain configurable.

## Agent tools and time

`recall_long_term_memory` uses the same retrieval pipeline and can accept optional time constraints. It returns no profile by default; the tool call must explicitly set `include_user_profile=true`, which can only expose the current stable private user and cannot target an arbitrary user. Passive recall does not automatically enable time filtering and never depends on `conversations.db` as its only time anchor.
