# Customer phone numbers stay one comma-joined column

`customers.phone` is a single free-text column that in practice holds a **list**: of the 2,307
customers with a phone, **1,422 (62%) hold two to five numbers** separated by commas, and 83 carry
an `F:` fax marker inline. We decided to **keep the column shape as-is and handle the multi-value
nature at the display layer** — every number rendered on its own line, each independently tappable,
fax labelled separately — rather than restructure the data. The readability complaint that started
this is entirely a rendering problem, and rendering is where it can be fixed without putting
2,665 rows of live sales contact data at risk.

## Considered options

- **A child table (`customer_phones`, one row per number).** Rejected — it is the textbook-correct
  model, but it touches every consumer (sales, AR, call card, customer map, the mobile sales-trip
  screens) and rewrites 2,307 rows of the numbers reps actually dial, to fix a symptom that never
  leaves the template layer. Revisit only if the trigger below fires.
- **Designate the first number as the "primary phone".** Rejected — **nothing in the data says which
  number is primary.** `templates/m/customer.html` already guesses this inline
  (`customer.phone.split(',')[0]`), and a wrong guess silently sends a rep to the wrong number.
  Showing all of them removes the guess entirely.
- **Normalize storage to one canonical separator and stop there.** Rejected as insufficient — it
  leaves the broken `tel:` link and the unreadable single-line blob exactly as they are.

## Consequences

- **`call/card.html` is currently broken and the display layer is where it gets fixed.** It passes
  the raw column into `href="tel:{{ m.phone }}"`, so the "กดเพื่อโทร" button hands the dialler a
  comma-joined string for 62% of customers.
- **`customer_contact_normalize.py` leaving the comma-joined list intact is deliberate, not an
  unfinished job.** That pipeline normalizes *noise* — โทร/Tel labels, embedded contact names, fax,
  address — and correctly leaves a genuine multi-number list alone. Do not "finish" it by splitting.
- **The normalizer's rollout is separately incomplete** (1,674 customers never touched, 62 rows
  pending human review). That is data coverage, a different problem from this one, and it does not
  argue for a schema change either way.
- **The trigger to revisit:** the moment a number needs its own attributes — who answers it,
  do-not-call, verified-on, per-number ordering — the column can no longer carry the model and the
  child table becomes correct. Until then it is one string with a separator.

_Counts measured on the local dev DB snapshot, 2026-09-08; re-derive against prod before acting._
