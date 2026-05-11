# TheHerta4

TheHerta4 is a Blender addon for SSMT4 workflows. It imports extracted game model data into Blender, prepares objects for export through a blueprint-driven pipeline, and generates game-specific mod output.

## Language

**Blueprint**:
A node-graph configuration that describes how selected Blender objects are preprocessed and exported.
_Avoid_: graph, tree, pipeline config

**DrawIB**:
A draw-call oriented data grouping parsed from extracted buffers and reused as an export unit.
_Avoid_: mesh group, generic part

**Component**:
A finer-grained subdivision inside a **DrawIB**, typically keyed by index ranges when a game workflow needs per-part processing.
_Avoid_: object, collection

**GameType**:
The game-specific buffer layout and export rules that control how imported data is interpreted and how output files are written.
_Avoid_: preset, template

**Preprocess**:
The export preparation phase that clones, merges, normalizes, and modifier-applies Blender objects before buffer calculation.
_Avoid_: warmup, cleanup

**Non-mirror workflow**:
An import/export workflow that uses object-level X-scale flipping instead of mutating lower-level mesh data structures.
_Avoid_: mirror hack

## Relationships

- A **Blueprint** drives one **Preprocess** flow and one export flow
- A **GameType** determines how a **DrawIB** and its **Components** are interpreted during import and export
- A **DrawIB** may contain multiple **Components**
- The **Non-mirror workflow** can wrap both import and export behavior

## Example dialogue

> **Dev:** "Should this logic live on the **Blueprint**, or is it really part of **Preprocess**?"
> **Domain expert:** "If it changes object state before buffer generation, treat it as **Preprocess**. The **Blueprint** only declares which flow should happen."

## Flagged ambiguities

- "export" can mean buffer-only export or full mod package output. Prefer "buffer export" or "mod export" when the distinction matters.
- "**Component**" and Blender object are not interchangeable terms. A single object may contribute data to one or more **Components** depending on the game workflow.
