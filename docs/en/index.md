---
layout: home
title: LivingMemory
titleTemplate: Source-grounded long-term memory
hero:
  name: LivingMemory
  text: Memory that can remember and reorganize
  tagline: Conversations become Timeline, then branch into source-grounded Topics and current-user profiles. Recall stays concise while relationship continuity and provenance remain visible.
  image:
    src: /logo.png
    alt: LivingMemory
  actions:
    - theme: brand
      text: Get started
      link: /en/guide/getting-started
    - theme: alt
      text: Understand the architecture
      link: /en/architecture
features:
  - title: Timeline preserves experience
    details: Summarizes continuous conversations with facts, affect, time ranges, actor bindings, and source snapshots.
  - title: Topic reorganizes themes
    details: Converts Timeline into formal fragments, merges related material across time, and supports keyword or Embedding similarity search.
  - title: Topic-first recall
    details: The current message qualifies candidates; optional reranking, facts, and fragments restore useful detail and tone.
  - title: Current-user profiles
    details: Exact private scopes inject only current objective facts and the current persona's relationship state.
---

<section class="home-band">
  <span class="home-kicker">Memory architecture</span>
  <h2>One source, two traceable derived routes</h2>
  <p>Timeline records what happened. Topic reorganizes related events for retrieval, while user profiles understand the current private user. Both routes retain provenance and never write derived content back into Timeline.</p>

![LivingMemory architecture](../assets/images/architecture-overview-en.svg){.diagram}

  <div class="home-memory-grid">
    <div><h3>Timeline is the source layer</h3><p>Round, idle, or manual summarization produces editable memories with source ranges and stable identity.</p></div>
    <div><h3>Topic is the derived layer</h3><p>Topics are read-only. Changes flow from Timeline through formal fragments into atomically published snapshots, while browsing supports keyword and semantic search.</p></div>
    <div><h3>User profile is a parallel derived layer</h3><p>Deterministic facts and persona relationships are maintained for a stable private actor. Only active facts inject; relationship work resolves the current persona and stores only its digest.</p></div>
    <div><h3>Routes meet at request time</h3><p>Topic/Timeline supplies relevant long-term memory. The profile loads by exact Bot, persona, and logical user, even when ordinary recall returns nothing.</p></div>
  </div>
</section>

<section class="home-band">
  <span class="home-kicker">Recall</span>
  <h2>Relevance first, context preserved</h2>
  <p>The current message controls Topic eligibility. Recent context adds bounded support, while the current-user profile follows an independent exact-scope route and always yields to the current conversation.</p>

![Topic-first recall](../assets/images/recall-flow-en.svg){.diagram}
</section>

<section class="home-band">
  <span class="home-kicker">Operations</span>
  <h2>Long-running memory needs maintenance</h2>
  <p>The maintenance center separates routine browsing from rebuilds, reviews, cleanup, database work, and diagnostics.</p>
  <div class="home-ops-grid">
    <div><h3>Build and repair</h3><p>Atomic full builds, bounded incremental updates, and in-place Timeline reconstruction.</p></div>
    <div><h3>Audit and diagnose</h3><p>Review ambiguity, inspect real recall traces, test models, and audit sessions.</p></div>
    <div><h3>Clean and archive</h3><p>Remove completed artifacts, manage inactive Timeline, advance profile fact lifecycles, and compact rebuildable projections.</p></div>
    <div><h3>Migrate and recover</h3><p>Backed-up migrations follow v8 to v9 to v10 and release v10.4; profiles enter through one v10.3 to v10.4 upgrade.</p></div>
  </div>
</section>
