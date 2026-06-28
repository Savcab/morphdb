# DynamoDB SDK Runtime Choice

Date: 2026-06-27

Status: Accepted

## Decision

Use synchronous `boto3` for the initial DynamoDB backend.

## Rationale

MorphDB's current runtime is synchronous and thread-based. The HTTP server uses
`ThreadingHTTPServer`, route dispatch is synchronous, and storage calls flow
through synchronous transaction and connection helpers.

A synchronous DynamoDB client fits the current architecture directly.

An async DynamoDB client, such as `aioboto3`, would only pay off cleanly if the
surrounding MorphDB runtime also moved to an async stack. Otherwise the backend
would need sync/async bridging inside the existing request path, increasing the
scope and risk of the DynamoDB implementation.

## Future Option

If MorphDB adopts an ASGI/async server in the future, revisit an async DynamoDB
implementation. Until then, tune boto3/botocore connection settings if
concurrency becomes a bottleneck.
