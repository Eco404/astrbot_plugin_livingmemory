---
layout: home
title: LivingMemory
titleTemplate: Source-grounded long-term memory
hero:
  name: LivingMemory
  text: Memory that can remember and reorganize
  tagline: Conversations become Timeline memories, then source-grounded Topics. Recall stays concise while provenance, maintenance, and recovery remain visible.
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
    details: Converts Timeline into formal fragments and merges related material across time without losing provenance.
  - title: Topic-first recall
    details: The current message qualifies candidates; optional reranking, facts, and fragments restore useful detail and tone.
  - title: Inspectable maintenance
    details: Full and incremental builds, rebuilds, reviews, session cleanup, and database checks expose preview and progress.
---

<section class="home-band">
  <span class="home-kicker">Memory architecture</span>
  <h2>Two memory layers, one traceable chain</h2>
  <p>Timeline records what happened. Topic reorganizes related events for retrieval. Formal fragments, facts, actors, and stable revisions keep both layers connected.</p>

![LivingMemory architecture](../assets/images/architecture-overview-en.svg){.diagram}

  <div class="home-memory-grid">
    <div><h3>Timeline is the source layer</h3><p>Round, idle, or manual summarization produces editable memories with source ranges and stable identity.</p></div>
    <div><h3>Topic is the derived layer</h3><p>Topics are read-only. Changes flow from Timeline through formal fragments into atomically published Topic snapshots.</p></div>
  </div>
</section>

<section class="home-band">
  <span class="home-kicker">Recall</span>
  <h2>Relevance first, context preserved</h2>
  <p>The current message controls eligibility. Recent context adds bounded support; formal fragments and facts restore concrete events and affect.</p>

![Topic-first recall](../assets/images/recall-flow-en.svg){.diagram}
</section>

<section class="home-band">
  <span class="home-kicker">Operations</span>
  <h2>Long-running memory needs maintenance</h2>
  <p>The maintenance center separates routine browsing from rebuilds, reviews, cleanup, database work, and diagnostics.</p>
  <div class="home-ops-grid">
    <div><h3>Build and repair</h3><p>Atomic full builds, bounded incremental updates, and in-place Timeline reconstruction.</p></div>
    <div><h3>Audit and diagnose</h3><p>Review ambiguity, inspect real recall traces, test models, and audit sessions.</p></div>
    <div><h3>Clean and archive</h3><p>Remove completed build artifacts, manage inactive memories, and compact storage on demand.</p></div>
    <div><h3>Migrate and recover</h3><p>Backed-up migrations follow v8 to v9 to v10, then the current v10.x schema.</p></div>
  </div>
</section>
