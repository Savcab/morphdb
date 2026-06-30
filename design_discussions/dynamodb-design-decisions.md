# DynamoDB Backend Design Decisions

Date: 2026-06-28

Status: Accepted design discussion summary

This file consolidates the DynamoDB design discussions into one grouped note.
The recurring theme is:

```text
Keep one stable MorphDB API externally.
Use DynamoDB-aware storage and access patterns internally.
Document less efficient DynamoDB patterns clearly for humans and agents.
```

The initial DynamoDB backend is not intended to expose a separate, narrower
DynamoDB-specific public API. It should preserve MorphDB behavior across SQLite,
Postgres, and DynamoDB, while making backend-specific cost visible.

## Theme 1: Product And API Direction

### Discussion: API Parity

Decision: DynamoDB should target the same public MorphDB API behavior as the
SQLite and Postgres backends.

This includes schemas, object CRUD, validation, defaults, relations, includes,
filters, sorting, exact `total`, `limit`, and `offset`.

Some requests are less natural on DynamoDB, but they should still return correct
MorphDB results. Documentation should call out the expensive cases so app
builders and AI agents can choose better UX or data access patterns.

Potentially expensive DynamoDB patterns include:

- large `offset` values
- exact totals on broad or filtered lists
- multiple filters that cannot share one DynamoDB access path
- `contains`
- negative filters such as `ne` and `exists=false`
- relation filters that require set subtraction
- arbitrary sort orders that do not match a key/index shape

### Discussion: Storage Approach

Decision: Implement DynamoDB as a real MorphDB storage backend, not as raw
DynamoDB exposure.

DynamoDB is schemaless, but MorphDB still provides the application-facing layer:

- stable generic HTTP endpoints
- schema registry
- validation and defaults
- relation and inverse-relation modeling
- app-key tenant isolation
- filter and sort conventions
- CLI and dashboard workflows
- no browser-side AWS credentials

Raw DynamoDB would force every vibe-coded app to invent its own API, validation
rules, relation model, access patterns, and security boundary.

### Discussion: Implementation Strategy

Decision: Introduce the logical MorphDB storage interface in one coordinated
refactor, then add DynamoDB behind that interface.

The old backend boundary was SQL-shaped: MorphDB emitted SQLite-flavored SQL,
and Postgres translated it. DynamoDB does not fit that model cleanly. The desired
boundary is:

```text
MorphDB API / domain behavior
  -> logical storage interface
    -> SQLite/Postgres SQL implementation
    -> DynamoDB-native implementation
```

Guardrails:

- preserve SQLite behavior
- preserve Postgres behavior
- keep the public MorphDB API stable
- add shared contract tests for storage behavior
- avoid DynamoDB-specific assumptions leaking into domain/API code

Rejected approach: scattered `if backend == "dynamodb"` branches throughout
domain modules.

### Discussion: Logical Storage Interface

Decision: Do not pretend DynamoDB is a SQL/DB-API backend.

The storage interface should expose MorphDB operations rather than SQL
statements:

- create/delete app
- read/write object schema
- read/write association schema
- create/update/delete object
- list objects
- maintain field-index records
- maintain relation edge records
- project relations
- reindex

SQLite and Postgres may continue using SQL internally. DynamoDB should implement
the same logical behavior with DynamoDB keys, items, indexes, transactions, and
batch operations.

### Discussion: Correctness Versus Efficiency

Decision: Correctness comes first for the initial DynamoDB backend.

Use efficient DynamoDB access paths where available, especially:

- get object by guid
- list objects by app/type
- positive relation lookups
- simple indexed field equality/range lookups
- batch fetches after index lookup

When a request does not map cleanly to DynamoDB, the backend should still return
the correct MorphDB result and document the cost.

Future work may add an explicit performance-optimized mode, but it must be opt
in. DynamoDB should not silently behave differently from SQLite/Postgres.

## Theme 2: Tenancy, Scale, And Table Layout

### Discussion: Tenancy And Table Boundary

Decision: Use one DynamoDB table per MorphDB deployment/environment by default,
with many isolated MorphDB apps inside that table.

In MorphDB, an `app` is the tenant boundary. Each app has its own schemas,
objects, relations, and derived indexes. Requests select the app with
`X-App-Key`.

Example:

```text
Company MorphDB deployment
  DynamoDB table: morphdb-prod
    MorphDB app: alice-crm
    MorphDB app: design-review-tracker
    MorphDB app: hackathon-inventory
    MorphDB app: support-dashboard
```

This matches the existing SQLite/Postgres model: one storage target backs a
deployment, and many apps live inside that storage target.

Future option: one DynamoDB table per app may be useful for stronger isolation,
per-app IAM, clearer billing, independent backup/restore, smaller blast radius,
or app-specific capacity tuning. It should be an advanced deployment mode, not
the default.

### Discussion: Scale Target

Decision: Optimize for many low-to-moderate-traffic MorphDB apps in one table.

The expected first use case is vibe-coding/prototyping and internal tools, not
high-traffic production SaaS workloads per app.

Design implications:

- expect many app keys in one table
- avoid key designs that force all deployment traffic into one partition
- avoid key designs that force all traffic for a busy app into one partition
- include app/type/object/index dimensions in access paths
- use on-demand billing for MorphDB-created tables
- optimize common prototype workloads first

### Discussion: Key Schema

Decision: Use one DynamoDB table with string primary keys:

```text
pk (S)
sk (S)
```

Use fixed GSIs for admin/common system paths:

```text
by_app
  gsi1pk (S)
  gsi1sk (S)
  projection: KEYS_ONLY

by_type_updated
  gsi2pk (S)
  gsi2sk (S)
  projection: ALL or selected object-ref metadata
```

`by_app` supports admin, cleanup, dashboard, and repair tooling.
`by_type_updated` supports `_updated_at` ordering for object refs.

Do not create one GSI per MorphDB field. Dynamic field and relation indexes are
represented as derived item collections.

Key encoding rules:

- escape or encode user-controlled app keys, type names, field names, relation
  names, and guids
- use explicit prefixes such as `APP#`, `TYPE#`, `GUID#`, `OBJ#`, `FIELD#`,
  `ASSOC#`, `EDGE#`, `VAL#`
- use sortable timestamp strings for `created_at` and `updated_at`
- define deterministic indexed-value encoding for string, datetime, boolean,
  and number values

Primary item families:

- app item: `pk=APP#{app}`, `sk=META`
- app registry ref: `pk=APPS`, `sk=APP#{app}`
- object schema: `pk=APP#{app}#SCHEMA`, `sk=TYPE#{type}`
- association schema: `pk=APP#{app}#ASSOC_SCHEMA`, `sk=ASSOC#{assoc}`
- object source item: `pk=APP#{app}#OBJ#{shard(guid)}`, `sk=GUID#{guid}`
- object list ref: `pk=APP#{app}#TYPE#{type}`, `sk=OBJ#C#{created_at}#G#{guid}`
- guid owner: `pk=GUID#{guid}`, `sk=OWNER`
- field index: `pk=FIDX#APP#{app}#TYPE#{type}#FIELD#{field}#VT#{value_type}`,
  `sk=VAL#{encoded_value}#G#{guid}`
- relation edge/index items for association edges and relation projections

The object source item is the source of truth. List refs, field indexes, and
relation indexes are derived records that can be rebuilt.

DynamoDB limits to respect:

- `Query` returns up to 1 MB before pagination
- `BatchGetItem` retrieves up to 100 items per call
- `BatchWriteItem` writes/deletes up to 25 items per call and requires retrying
  unprocessed items
- `TransactWriteItems` groups up to 100 actions and 4 MB per transaction
- individual items are limited to 400 KB
- GSI reads are eventually consistent, so correctness-sensitive paths should
  prefer base-table item collections where practical

## Theme 3: Indexing And Query Behavior

### Discussion: Indexing Strategy

Decision: Use a small fixed set of DynamoDB GSIs plus generic MorphDB index
items. Do not create one DynamoDB GSI per user-defined MorphDB field.

When a schema marks a field as indexed:

```json
{
  "status": { "type": "string", "index": true }
}
```

the backend maintains generic field-index records for that field and object
type. This keeps schema edits lightweight and avoids AWS infrastructure changes
per field.

### Discussion: Generic Field Index Items

Decision: Field-index items are derived records equivalent in purpose to the SQL
`field_index` table.

Example:

```text
FIELD app=demo type=task field=status   value=todo object=task_123
FIELD app=demo type=task field=priority value=3    object=task_123
```

A query such as `GET /objects/task?status=todo` can read matching field-index
items, collect object IDs, then fetch source object items.

Benefits:

- avoids scanning every object for common indexed filters
- avoids one GSI per MorphDB field
- keeps schema edits lightweight
- lets reindex repair derived items from source objects
- mirrors the SQLite/Postgres `field_index` design

### Discussion: Index Flag Semantics

Decision: `"index": true` has the same public meaning on DynamoDB as it does on
SQLite/Postgres.

An indexed scalar field is eligible for filtering and sorting. An unindexed field
continues to reject filter/sort requests.

Implementation detail: `"index": true` creates/maintains generic derived
field-index items, not a new DynamoDB GSI.

### Discussion: Default Values

Decision: Preserve MorphDB's lazy default-value semantics.

Objects store actual data. Reads project missing fields through the current
schema. Defaults should not be materialized into every object item.

For DynamoDB:

- object items store only actual field data
- schema projection applies defaults on read
- field-index records represent actual stored, type-valid values only
- missing or stale values are interpreted through current schema defaults during
  filtering and sorting
- filters/sorts that match defaults may require base-object checks for exact
  parity

This keeps schema edits lightweight and matches SQLite/Postgres behavior.

### Discussion: Multiple Filters

Decision: Preserve existing multiple-filter behavior.

DynamoDB cannot combine arbitrary independent indexes the way a relational query
planner can. The backend should pick a good base candidate path, then intersect
or post-filter as needed.

Planner preference:

1. positive relation equality/in filters through relation index data
2. indexed scalar equality/range filters through field-index data
3. app/type object listing through object refs

Remaining filters are applied by index-set intersection or application-side
filtering. Exact `total` is preserved.

### Discussion: Relation Filters

Decision: Preserve existing relation filter behavior:

```http
GET /objects/task?assignee=user_123
GET /objects/task?assignee__in=user_1,user_2
GET /objects/task?assignee__ne=user_123
GET /objects/task?assignee__exists=true
```

Efficient or naturally supported:

- relation equality
- relation `in`
- `exists=true`
- relation projection for a page of objects
- include hydration after relation lookup

Potentially expensive:

- `ne`
- `exists=false`
- broad symmetric relation lookups if the layout cannot serve both directions

Optimize positive relation filters first. Implement negative/existence-false
filters with base object reads plus set subtraction when needed.

### Discussion: Contains Filters

Decision: Preserve `contains` filter behavior.

DynamoDB `contains` is not a true substring index. It is a filter expression
applied after reading candidate items, so broad `contains` requests can be
scan-heavy.

Direction:

- keep case-insensitive substring semantics aligned with SQLite/Postgres
- use another efficient base path when available
- apply `contains` as post-filtering when needed
- expect app/type scanning if `contains` is the only filter

### Discussion: Sorting

Decision: Preserve existing sort behavior:

- `_created_at`
- `_updated_at`
- `_guid`
- declared scalar fields with `"index": true`

Continue rejecting unsupported sorts such as unindexed fields, `json` fields,
and relations.

DynamoDB only sorts efficiently when the requested order matches a table/index
sort key. Some MorphDB sort requests therefore require collecting candidate
items, sorting in application code, then applying `limit` and `offset`.

Optimize natural object listing, system timestamp paths, and indexed scalar
field sorts where the layout can support them directly.

### Discussion: Pagination

Decision: Support MorphDB's existing `limit`/`offset` pagination API first.

DynamoDB natively paginates with cursor-like `LastEvaluatedKey` values. To
emulate large offsets, the backend may need to walk pages internally until it
reaches the requested offset. This is correct but can be expensive.

Documentation should steer agents toward:

- small limits
- small offsets
- load-more/infinite-scroll UX
- avoiding page-number jumps on large DynamoDB-backed datasets

Future work should add cursor pagination across all backends, map DynamoDB
cursors to `LastEvaluatedKey`, and consider cursor caching for page-number
navigation.

### Discussion: Exact Totals And Future Performance Modes

Decision: Preserve exact `total` on list endpoints.

Exact totals can be expensive on DynamoDB for broad queries, filtered queries,
negative filters, `contains`, and multi-filter queries.

Future work may add an explicit performance mode, for example:

```text
performance_optimized=true
```

Such a mode could:

- omit exact totals
- prefer cursor pagination
- expose `has_more` / `next_cursor`
- warn on or reject scan-heavy filters and sorts

This should be explicit, not an accidental backend difference.

## Theme 4: Writes, Consistency, And Maintenance

### Discussion: Transactions And Consistency

Decision: Use DynamoDB transactions and conditional writes where practical to
preserve MorphDB write semantics.

SQLite/Postgres writes run in transactions. DynamoDB should preserve that for
normal-sized multi-item writes, including object blobs, field indexes, and
relations.

Direction:

- use `TransactWriteItems` for normal multi-item writes where practical
- use conditional writes for uniqueness and cardinality checks
- keep source objects and derived index/relation items consistent
- use strong reads where correctness matters
- fail clearly if an operation exceeds transaction limits and cannot be safely
  split

Large app/type deletes, large relation rewrites, and large backfills may exceed
single-transaction limits. Future work may add background jobs and repair/status
tracking for those cases.

### Discussion: Index Backfill

Decision: Indexed-field schema changes should synchronously backfill derived
field-index items for initial parity.

Expected behavior:

- adding `"index": true` backfills existing objects
- removing `"index": true` removes field-index items
- retyping an indexed field rebuilds index items according to the new field type
- object blobs remain the source of truth
- no DynamoDB GSI/table-level infrastructure changes happen for field changes

Large backfills may require multiple internal batches while the request remains
in progress.

### Discussion: Reindex

Decision: Support MorphDB reindex behavior on DynamoDB.

Reindex should:

- treat object items as source of truth
- scan relevant source object items
- recompute generic field-index items from object data and current schemas
- delete stale field-index items in the selected scope
- write rebuilt index items in DynamoDB batches
- remain synchronous in the initial implementation

Large reindex operations may be slower and more expensive on DynamoDB. Future
work may add async/background reindexing with explicit status.

### Discussion: Migration Scope

Decision: Do not include SQLite/Postgres-to-DynamoDB migration in the initial
scope.

The first DynamoDB backend targets fresh DynamoDB deployments and tables.
In-DynamoDB reindexing remains in scope because it rebuilds derived items from
DynamoDB source objects. Cross-backend migration can be considered separately.

## Theme 5: Configuration, Runtime, And AWS Operations

### Discussion: Target Syntax

Decision: Add a `dynamodb://` storage target URL.

Examples:

```text
dynamodb://morphdb-prod?region=us-west-2
dynamodb://morphdb-dev?region=us-west-2&endpoint_url=http://localhost:8000&create_table=true
dynamodb://morphdb-dev?region=us-west-2&profile=my-dev-profile
```

Supported query parameters:

- `region`
- `endpoint_url`
- `profile`
- `create_table`

Do not embed AWS access keys or secrets in the URL.

### Discussion: Environment Configuration

Decision: Use `MORPHDB_DATABASE_URL` for DynamoDB targets.

Do not add required DynamoDB-specific environment variables such as
`MORPHDB_DYNAMODB_TABLE` or `MORPHDB_DYNAMODB_REGION`.

This preserves MorphDB's existing backend selection model for SQLite paths,
Postgres URLs, and now DynamoDB URLs.

### Discussion: Credentials And IAM

Decision: Do not read a database password or database secret for DynamoDB. Use
AWS IAM and the boto3 credential chain.

Local development can use:

- `AWS_PROFILE`
- `~/.aws/credentials`
- AWS SSO
- environment credentials

AWS runtimes can use:

- Lambda execution roles
- ECS task roles
- EC2 instance roles
- CI/OIDC role assumption

For Lambda, the runtime role should receive least-privilege permissions for the
configured table and indexes. `CreateTable` should only be granted where table
creation is explicitly allowed.

### Discussion: Optional Dependency

Decision: DynamoDB support is an optional package extra:

```toml
[project.optional-dependencies]
dynamodb = ["boto3>=..."]
```

Install with:

```bash
pip install 'morphdb[dynamodb]'
```

This preserves the zero-dependency default SQLite install and mirrors
`morphdb[postgres]`.

If a `dynamodb://` target is configured without `boto3`, MorphDB should show a
clear install hint.

### Discussion: SDK Runtime Choice

Decision: Use synchronous `boto3` first.

MorphDB's current runtime is synchronous and thread-based. A synchronous
DynamoDB client fits the request path directly. Async clients such as
`aioboto3` should be revisited only if MorphDB moves to an async/ASGI runtime.

### Discussion: Billing Mode

Decision: When MorphDB creates a DynamoDB table, default to on-demand billing.
When a table already exists, leave its billing mode unchanged.

On-demand billing fits many small, spiky prototype/internal apps. Provisioned
capacity remains an operator-managed choice for predictable high-volume
workloads.

### Discussion: Table Creation Policy

Decision: Verify an existing table by default. Support explicit opt-in table
creation for local development or setup.

Direction:

- verify an existing table by default
- allow `create_table=true`
- do not implicitly create production tables
- never mutate billing mode, IAM, backups, or other production settings on an
  existing table
- when MorphDB creates a table, use the expected MorphDB key/index layout

Production deployments should usually manage tables through Terraform,
CloudFormation, CDK, Pulumi, the AWS console, or another infrastructure workflow.

### Discussion: Local And Production Behavior

Decision: Use the same boto3 backend code path for local development and
production. The only difference should be endpoint configuration.

Local example:

```bash
export MORPHDB_DATABASE_URL='dynamodb://morphdb-dev?region=us-west-2&endpoint_url=http://localhost:8000&create_table=true'
```

Production example:

```bash
export MORPHDB_DATABASE_URL='dynamodb://morphdb-prod?region=us-west-2'
```

Production omits `endpoint_url`, so boto3 talks to AWS DynamoDB.

### Discussion: IAM Policy Examples

Decision: Documentation should include IAM policy examples for runtime access
and separate dev/setup table-creation permissions.

Production runtime actions should include:

```text
dynamodb:DescribeTable
dynamodb:GetItem
dynamodb:PutItem
dynamodb:UpdateItem
dynamodb:DeleteItem
dynamodb:Query
dynamodb:Scan
dynamodb:BatchGetItem
dynamodb:BatchWriteItem
dynamodb:TransactWriteItems
```

Resources should be scoped to:

```text
arn:aws:dynamodb:<region>:<account>:table/<table>
arn:aws:dynamodb:<region>:<account>:table/<table>/index/*
```

Only explicit dev/setup flows should add permissions such as
`dynamodb:CreateTable`. `dynamodb:DeleteTable` should not be included by
default.

## Theme 6: Testing, Docs, Dashboard, And Rollout

### Discussion: Testing Strategy

Decision: DynamoDB testing should mirror the current Postgres testing approach.

The default test suite remains zero-service and SQLite-based. DynamoDB tests run
only when an explicit DynamoDB test target is provided.

Direction:

- keep default CI free of AWS and DynamoDB Local requirements
- support optional integration tests with `MORPHDB_TEST_DATABASE_URL`
- redirect `db.init_db(":memory:")` to a reset DynamoDB test table/prefix when a
  DynamoDB test target is configured
- run public behavior tests against DynamoDB
- skip SQL/SQLite-specific tests such as query-plan assertions
- add DynamoDB-specific unit tests for target parsing, key layout, dependency
  errors, and query planning

Example:

```bash
MORPHDB_TEST_DATABASE_URL='dynamodb://morphdb-test?region=us-west-2&endpoint_url=http://localhost:8000&create_table=true' \
  python -m pytest tests/
```

### Discussion: Test Tooling

Decision: Use DynamoDB Local or LocalStack as the primary optional integration
test target.

Test layers:

- default suite: SQLite only, no AWS or local DynamoDB dependency
- unit tests: target parsing, dependency errors, key encoding, query planning,
  layout helpers
- optional integration tests: DynamoDB Local or LocalStack through `endpoint_url`
- optional manual/release tests: real AWS DynamoDB table

Moto may be useful for narrow unit tests, but should not be the only validation
because mocks can differ from real DynamoDB behavior.

### Discussion: Storage Contract Tests

Decision: Add shared storage contract tests for the logical MorphDB storage
interface.

Every storage implementation should pass the same contract where behavior is
part of MorphDB public semantics:

- SQLite
- Postgres
- DynamoDB

Contract tests should cover apps, schemas, object CRUD, default projection,
indexed field maintenance, relation behavior, list/filter/sort behavior,
reindexing, and transaction/atomicity expectations where applicable.

### Discussion: Docs And Dashboard

Decision: Add DynamoDB docs and dashboard support.

AWS docs should cover:

- `MORPHDB_DATABASE_URL=dynamodb://...`
- Lambda execution-role IAM permissions
- explicit dev table creation with `create_table=true`
- production recommendation to pre-provision tables
- on-demand billing for MorphDB-created tables
- DynamoDB Local/LocalStack via `endpoint_url`
- performance caveats for exact totals, offset pagination, sorting, `contains`,
  negative filters, and broad scans
- the existing unauthenticated public Function URL warning

The dashboard should work with DynamoDB through logical MorphDB views rather
than SQL table introspection. Initial support can show backend/table target,
apps, object schemas, object counts where feasible, relation schemas, and
logical object browsing. SQL raw-table exploration remains SQL-specific until a
DynamoDB raw/logical item explorer is designed.

### Discussion: Stacked PR Plan

Decision: Implement DynamoDB support as a stack of focused PRs.

PR 1: logical storage interface.

- establish the new abstraction boundary
- preserve SQLite and Postgres behavior
- add shared storage contract tests
- keep public behavior tests passing

PR 2: DynamoDB backend.

- add `morphdb[dynamodb]`
- support `dynamodb://...` through `MORPHDB_DATABASE_URL`
- implement table verification/optional creation
- implement DynamoDB-native app/schema/object/index/relation storage
- add optional DynamoDB Local/LocalStack integration path

PR 3: docs and dashboard.

- add AWS Lambda + DynamoDB deployment docs
- add IAM examples
- document performance caveats for humans and agents
- support DynamoDB logical dashboard views

PR 4: AWS deploy support.

- teach the AWS deploy script to package the DynamoDB dependency when needed
- wire `MORPHDB_DATABASE_URL` into Lambda
- attach scoped DynamoDB runtime permissions
- keep local clients pointed at the deployed MorphDB host, not DynamoDB directly

This keeps the abstraction refactor, backend implementation, and operational
docs/dashboard/deploy changes reviewable.

## Future Options

These are intentionally out of the first implementation scope or explicit future
work:

- one table per app as an advanced isolation mode
- cursor pagination across all backends
- cursor caching for page-number navigation
- explicit DynamoDB performance mode
- async AWS SDK/runtime rewrite
- background reindex/backfill jobs
- SQLite/Postgres-to-DynamoDB migration tooling
- raw DynamoDB item explorer in the dashboard
