# FAQ index

Answers to the questions different teams ask when evaluating, adopting, or reviewing this
repository as a common base. Each file is written for a specific audience; skim the one
that matches your role.

| FAQ | For | Answers |
|---|---|---|
| [security-faq.md](security-faq.md) | AppSec / security review | authn/authz, tenancy, secrets, supply chain, the audit chain, what's in vs out of scope |
| [portability-faq.md](portability-faq.md) | Architecture / cloud / exit planning | portability boundaries, the five runtime profiles, on-prem/sovereign exit, data export |
| [features-faq.md](features-faq.md) | Product / compliance / delivery | what the agent does, what is deterministic vs LLM, and the boundary with sibling platform systems |
| [adoption-faq.md](adoption-faq.md) | Engineering leads forking the repo | rename, upstream fixes, extension points, versioning |
| [compliance-faq.md](compliance-faq.md) | Compliance / MLRO / model risk | regulatory posture, PII, maker-checker, residency, model-risk evidence |

These FAQs deliberately do **not** re-document capabilities owned by sibling systems in the
[catalog](https://github.com/portable-genai). Where a concern belongs to
another repo (the guardrail gateway, the governed knowledge base, the eval platform, ...),
the FAQ points at it and explains the boundary rather than duplicating it. See
[features-faq.md](features-faq.md) for the full "what this repo owns vs what it integrates"
map.
