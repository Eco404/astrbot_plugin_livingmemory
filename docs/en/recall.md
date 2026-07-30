# Recall and injection

Production recall uses Topic as the main memory, adds non-duplicative facts and formal fragments, and falls back to Timeline when Topic is unavailable.

![Topic-first recall](../assets/images/recall-flow-en.svg){.diagram}

## Query branches

The current user message has weight `1.0` and must qualify a candidate independently. Recent user and optional assistant messages provide bounded context support but cannot promote an unrelated Topic by themselves.

## Candidate qualification and reranking

Embeddings and keywords establish eligibility and base relevance. Optional Rerank reorders qualified candidates; its rank bonus is reduced when the model shows little score separation. Relative floors, actor bonuses, context-overlap suppression, MMR, and dynamic stopping can return fewer than `k` results.

## Injection assembly

The system injects the Topic body first, then facts and fragments that add information. If a fragment body duplicates its parent Topic, the body is omitted while unique facts remain eligible. Repeated paragraphs and facts are merged before final injection.

An actual Topic injection forwards bounded access contribution to source Timeline memories, keeping the original lifecycle mechanism informed.

## Agent tools and time

`recall_long_term_memory` uses the same retrieval pipeline and can accept optional time constraints. Passive recall does not automatically enable time filtering and never depends on `conversations.db` as its only time anchor.
