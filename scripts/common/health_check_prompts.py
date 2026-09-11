"""Shared single-PR health check dimension descriptions.

Used by learn_prompts.py (Phase 0) and referenced in run_pr_audit.py instructions.
"""

HEALTH_CHECK_DIMS = """\
1. **Spec Coverage**: Check `has_acceptance_criteria` in the PR data. If false, read the issue \
excerpt — is there testable behavior described? Flag "AC missing" only when the description has \
NO testable outcome at all.
2. **Design Decisions**: Were architectural trade-offs made? Check: `ls docs/adr/ 2>/dev/null | head -5`. \
If a significant decision was made and no ADR exists, recommend a 2-line ADR entry.
3. **Security & SRE**: Did this PR touch auth/rbac/iam/credential/secret/permission/token/oauth files? \
Was a rollback plan or feature flag mentioned? Were observability additions made for new code paths?
4. **Review Quality**: Check `rubber_stamp_approvers` and `substantive_human_comment_count`. \
Only flag rubber-stamp for HIGH blast-radius changes (auth/billing/config/infra).
5. **CI Health**: Count "Build #" in the CI bot comments. Flag if ≥5 iterations.
"""
