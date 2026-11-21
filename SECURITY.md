# Security

`lmlog` is a local log-analysis tool. It can process sensitive application logs,
so treat uploaded files and the SQLite state as sensitive data.

## Reporting

Report suspected vulnerabilities privately via a GitHub security advisory in
this repository instead of a public issue.

## Hardening

- `lmlog serve` refuses to start without `LMLOG_API_TOKEN`.
- API requests use constant-time bearer-token comparison.
- Uploaded files are stored under generated UUID directories; the display name
  is sanitized and never used as a path.
- Upload extensions are allowlisted and size checks happen while streaming.
- LLM prompts treat log content as untrusted data.
- The service can run fully air-gapped with heuristic explanations.

## Demo data

Only synthetic logs and sanitized screenshots belong in this repository. Do not
commit customer log samples, internal hostnames or private namespace names.

## Supported versions

Only the latest `0.1.x` line is supported for security fixes.
