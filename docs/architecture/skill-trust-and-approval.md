# Skill trust and approval

Recall preserves two independent hierarchies. Decision authority ranks
user-approved decisions above project policy, approved plans, delegated
decisions, and unapproved recommendations. Factual evidence ranks current
repository/tests and successful tool output above versioned documentation,
user statements, trusted-agent conclusions, unverified-agent statements, and
external content.

Compilation does not collapse these labels into confidence. Facts retain a
verification label; proposed decisions remain proposed; contradictory sources
remain visible in `explain` and evidence expansion.

The Phase 4 workflow is evidence -> candidate -> pending approval -> exact-
version review. The scope user or an explicitly trusted reviewer may approve.
Approval, rejection, request-changes, deprecation, and archival decisions are
append-only audit rows with reviewer, reason, and timestamp. A final rejection
cannot be silently reversed on the same version; changed content requires a new
version. External or unverified claims are never auto-approved.
