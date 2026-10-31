# Write path: batching commits

A stub of the plan for when one commit per save stops being enough. Nothing here is built yet. How writes reach
the lake today is in `PLAN.md` ("How writes reach the lake").

## The problem

Every saved shot, bag or equipment change is its own Hugging Face commit (`Store._commit` → `Lake.publish`), made
before the save is acknowledged. Hugging Face advises that a repo slows down after a few thousand commits: pushes
and history reads get slower, and saves with them. Volume is low today, so the fix is deferred.

## When to act

Any of:

- the "registry.coffee: write volume" alert fires: more than `saves_per_day_alert` (default 2,000) saves in 24 h,
  or the p95 of `publish_ms` over 5 s (`src/terraform/alerts.tf`);
- the dataset's total commit count passes about 1,500;
- saves regularly take more than a few seconds.

## The plan

- **Durable queue.** A save writes the record to a durable queue and is acknowledged once it's there. Entries are
  immutable and keyed on `(table, id, rev)`, the `rev` in `PLAN.md`, so a resend is a no-op, not a second entry.
- **Batch writer.** One writer drains the queue and makes one commit per batch: one add-only Parquet file holding
  the batch's records, plus one manifest row naming that file and the keys in it. Nothing in the dataset is
  replaced. A batch that lands twice is recognised by its manifest row and skipped.
- **Read your own writes.** Until a record's batch lands, the app serves it from the queue on top of the lake, so
  the person who saved it sees it at once, and a restart doesn't hide it.
- **History squash, occasionally.** Batching slows commit growth but doesn't stop it. Now and then, when the count
  is high, squash the dataset's history with `HfApi.super_squash_history`. It rewrites the history, so it's a
  manual step, run by the owner outside the weekly run.

## Proof of concept, in order

1. A local asyncio prototype: an in-process queue backed by a file on disk, a batch writer task, and tests for
   dedupe, crash between ack and commit, and read-your-own-writes.
2. Then a real durable queue: Pub/Sub, or objects in a GCS bucket that the writer lists and deletes after commit.
   Pick on cost at one instance and on how simple replay is.

## The risk to avoid

Acknowledging a save before it's durable. If the app says "saved" while the record is only in memory, a restart
or scale-to-zero loses it, and the phone's outbox has already let it go. Ack only after the queue write succeeds;
everything after that (batching, commit, squash) may be slow or retried, but not lossy.
