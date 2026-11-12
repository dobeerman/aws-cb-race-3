# AI Gateway on Amazon ECS

A reproducible AI gateway using **LiteLLM Proxy**, **Amazon ECS Fargate**, **Amazon Bedrock (Nova Micro)**, and **Aurora PostgreSQL Serverless v2**. The gateway exposes an OpenAI-compatible API and demonstrates model-scoped virtual keys, independent application budgets, spend accounting, and deterministic reliability tests.

Built for **AWS Builder Center Speed Run 3**. This is a single-account demonstration, **not a production-ready multi-tenant gateway**.

**Verified end-to-end:** a clean `make destroy → make deploy → make verify` cycle was completed successfully on November 12, 2026. The SSM tunnel must be running in a separate terminal during `make verify`.

## Architecture

```text
Operator / local API client
       |
       | AWS Systems Manager port forwarding
       v
localhost:4000  ->  ECS Fargate, 1 task, LiteLLM Proxy (127.0.0.1:4000)
                         |                       |
                         |                       +--> Amazon Bedrock Converse
                         |                            Nova Micro via task IAM role
                         |
                         +--> Aurora PostgreSQL Serverless v2
                              virtual keys, budgets, spend records
                              
Secrets Manager -> ECS task secrets
CloudWatch Logs <- ECS container logs
```

The stack runs in **us-east-1**. Fargate uses a public subnet and outbound public IPv4 address to reach AWS APIs without a NAT gateway. Its security group permits **no inbound traffic**, and LiteLLM listens on loopback. The database lives in isolated subnets, is not publicly accessible, uses encrypted storage, requires TLS, and permits PostgreSQL connections only from the ECS task security group.

An SSM tunnel, not an ALB or public HTTP endpoint, provides local access to both the OpenAI-compatible API and LiteLLM Admin UI. Permissions to start an SSM session are privileged and should be restricted to trusted operators. ECS injects database credentials and the LiteLLM master-key suffix from Secrets Manager. The task role grants Bedrock invocation only for the configured Nova Micro foundation model.

### Design decisions and trade-offs

| Decision | Why | Cost or limitation |
| --- | --- | --- |
| ECS Fargate | Runs the regular long-lived LiteLLM Proxy and Admin UI | Always-on compute; this single-task demo briefly interrupts service during updates |
| Aurora Serverless v2 | Keeps keys, budgets, and usage across task replacements | Nontrivial minimum cost; runtime currently uses the database's generated cluster user, including schema initialization |
| SSM port forwarding | Avoids publishing an admin API on the internet | For operator-led tests, not a production ingress pattern |
| Public task subnet, no NAT | Simple outbound AWS connectivity with lower infrastructure cost | Public IPv4 charge and outbound internet path |
| One task, one writer | Limits concurrent schema-initialization complexity | No application-level high availability |
| Short-lived virtual keys | Per-application model and spend controls | Not a replacement for AWS tenant isolation or an infrastructure spending cap |

Aurora PostgreSQL is pinned to version 16.8, with **0.5–2 ACUs**, encryption and a one-day backup retention. CloudWatch logs are retained for seven days. Database deletion protection is disabled and removal is destructive: use a disposable account or accept loss of demo data on teardown.

## Prerequisites

- Node.js **24+**, **pnpm 12.18.0**, Python 3, Make and Git.
- AWS CLI v2, credentials authorized to deploy CDK/ECS/RDS/IAM/Secrets Manager resources, and a selected AWS profile.
- Docker with a running daemon and `linux/amd64` support.
- [AWS Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html).
- Amazon Nova Micro inference availability in **us-east-1**, plus Aurora PostgreSQL 16.8 Serverless v2 availability.
- For live tests: CloudFormation output read, ECS task discovery, SSM session, and `secretsmanager:GetSecretValue` permissions for this stack's master-key secret.

Check your AWS identity before deployment or destruction:

```bash
export AWS_PROFILE=your-demo-profile
aws sts get-caller-identity
```

The Makefile sets `AWS_REGION` and `AWS_DEFAULT_REGION` to `us-east-1` and deploys the `SpeedRunAiGateway` stack.

## 1. Install and check locally

```bash
make install
make check
make synth
```

`make check` runs TypeScript type checking, TypeScript tests, and Python unit tests. `make synth` renders a strict CloudFormation template. Synthesis and tests don't require a deployed gateway. To build the container separately:

```bash
docker build --platform linux/amd64 -t speed-run-gateway ./gateway
```

The gateway image pins LiteLLM to `v1.108.4` with a SHA-256 digest and includes the Amazon RDS CA bundle. Its startup script builds a TLS-configured PostgreSQL URL and runs LiteLLM bound to loopback.

## 2. Deploy the stack

```bash
make bootstrap  # Once for the account and us-east-1, if not already bootstrapped
make diff
make deploy
```

`make deploy` checks Docker and prints the AWS account ID before CDK deployment. Review the changes and any broadening-permission approval prompt. Outputs include `ClusterName`, `ServiceName`, `MasterKeySecretArn`, and `LogGroupName`.

## 3. Start the SSM tunnel

In **terminal A**:

```bash
export AWS_PROFILE=your-demo-profile
make tunnel
```

Leave the command running. In a **second terminal**, verify reachability:

```bash
curl --noproxy '*' -i http://127.0.0.1:4000/health/readiness
```

A successful readiness check returns HTTP 200. The gateway API is at `http://127.0.0.1:4000/v1`; the Admin UI is at `http://127.0.0.1:4000/ui`.

**Common pitfall:** `make deploy` does **not** open the tunnel, and `make verify` does **not** start it automatically. Running `make smoke` without the tunnel produces `/health/readiness could not reach the gateway; check make tunnel`. If ECS replaces the task, reopen the tunnel to its new runtime ID.

The LiteLLM master key is `sk-` followed by the `key` field from the Secrets Manager secret referenced by `MasterKeySecretArn`. Keep the master key in operator-only use; applications should use virtual keys.

## 4. Smoke-test real Bedrock inference

In terminal B, with the tunnel still running:

```bash
export AWS_PROFILE=your-demo-profile
make smoke
```

This script:

1. Checks readiness and rejects an unauthenticated model-list request.
2. Creates a disposable virtual key limited to `nova-micro` with a **$0.01** budget.
3. Reads the stored key policy back.
4. Sends a real completion request to Nova Micro through Bedrock.
5. Deletes the temporary key.

On success it prints:

```text
Readiness, authentication, budgeted key, Bedrock completion and key deletion passed.
```

A successful readiness check alone does not prove inference works. The completion step does.

## 5. Prove that application budgets are independent

```bash
make test-budgets
```

The live test creates disposable keys for **App A ($0.01)** and **App B ($0.10)**, both limited to `nova-micro`. It uses synthetic requests to bring App A's estimated spend to the threshold, waits for LiteLLM's stored spend to catch up, and checks for a **budget-specific denial**. It then checks that App B had zero spend before use, makes a successful App B request, and confirms that its charge is recorded.

This matters because receiving a successful completion with a budgeted key is not evidence that budgets are enforced. The test checks both **denial after exhaustion** and **continued availability of a separately budgeted application**.

The script caps output at eight tokens per request; it makes at most 30 large App A requests (48,000 bytes of synthetic input each), six small denial probes and one App B request. The estimated worst-case client-side workload is roughly **$0.05** under the documented token-cost assumption, before gateway retries or price changes. This is **not** a hard billing cap. Keys expire after 30 minutes and the script attempts cleanup even on failure.

A manual UI walkthrough is optional: open `/ui`, create two disposable model-scoped keys with different budgets, observe their independent spend, and delete them. Never put the master key into client application code.

## 6. Exercise retries and fallback in isolation

```bash
make test-reliability
```

This test uses the pinned LiteLLM image, a local mock provider, and a Docker container with **external networking disabled**. No AWS credentials, SSM tunnel, database, or Bedrock calls are needed.

| Scenario | Primary response sequence | Backup response sequence | Expected outcome |
| --- | --- | --- | --- |
| Baseline | 200 | None | Primary completion |
| Retry | 429, 429, 200 | None | Primary completion after retries |
| Fallback | 503, 503, 503 | 200 | Backup completion |
| Exhaustion | 503, 503, 503 | 503, 503, 503 | Injected 503 error |

The isolated router configuration allows two retries per model group and one fallback hop. Assertions compare the exact upstream request sequence, client HTTP status, and response marker. Reports are written to `.local/reliability/<run-id>/` as `results.json`, `summary.md`, and `evidence.json`; the reports exclude secrets, request bodies, and raw proxy logs.

**Boundary:** production deployment config exposes only `nova-micro`. This mock suite tests how the pinned LiteLLM build handles retries and a configured backup route; it is **not proof of deployed cross-provider failover**.

## 7. Run complete verification

With the gateway deployed and `make tunnel` running in terminal A:

```bash
make verify
```

The Makefile runs these targets sequentially:

| Target | Checks | External dependency |
| --- | --- | --- |
| `make check` | Type checking and unit tests | Local tooling |
| `make smoke` | Authentication, model-scoped key, real completion | Deployed gateway, SSM tunnel, Bedrock |
| `make test-budgets` | Spend tracking, exhausted App A, independent App B | Deployed gateway, SSM tunnel, Bedrock |
| `make test-reliability` | Deterministic retries, fallback, exhaustion | Local Docker only |

Only after every target passes does Make print `All verification steps passed.`. On **November 12, 2026**, the full clean redeploy and verification cycle passed:

```text
make destroy   PASS
make deploy    PASS
make verify    PASS
```

This result confirms the tested path; it does not establish production readiness, high availability or exhaustive security isolation.

## Troubleshooting

- **`/health/readiness could not reach the gateway`**: run `make tunnel` in another terminal, verify port 4000 isn't occupied, and rerun the readiness `curl`.
- **SSM session cannot start**: check the Session Manager plugin, profile, session permissions, running ECS task, and ECS Exec configuration. A replaced task needs a new tunnel.
- **Task not healthy**: inspect ECS service events and the CloudWatch log group from the stack output. Investigate database connection, startup migrations, TLS, and injected secrets.
- **Bedrock returns an error**: check regional model availability, applicable model-access requirements, quotas, and the task role's explicit Nova Micro permissions.
- **Spend stays zero / budget test fails**: inspect model pricing and persisted spend records; accounting can lag behind requests. A gateway budget isn't an AWS Billing limit.
- **Reliability test fails**: verify Docker is running, `linux/amd64` can execute, and the pinned container image can be pulled. Inspect the generated `.local/reliability/` report.

## Estimated cost

At the configured minimum capacity for 730 hours in `us-east-1`, approximate monthly baseline charges are:

| Resource | Approximate monthly cost |
| --- | ---: |
| Fargate, 0.5 vCPU / 2 GiB, x86 | $21 |
| One public IPv4 address | $3.65 |
| Aurora Serverless v2 at 0.5 ACU | $43.80 |
| **Minimum compute + IPv4 subtotal** | **~$69** |

This excludes Aurora storage and I/O, CloudWatch, Secrets Manager, ECR, and Bedrock inference. At 2 ACUs continuously, Aurora compute alone would be about $175/month. Costs vary by actual usage and current rates. See [Fargate pricing](https://aws.amazon.com/fargate/pricing/), [VPC pricing](https://aws.amazon.com/vpc/pricing/), [Aurora pricing](https://aws.amazon.com/rds/aurora/pricing/), and [Bedrock pricing](https://aws.amazon.com/bedrock/pricing/).

Virtual-key budgets limit usage at the LiteLLM layer; they **do not limit Fargate, Aurora, networking or other AWS charges**.

## Cleanup

Stop the SSM tunnel, check the profile, and delete the demo stack:

```bash
aws sts get-caller-identity
make destroy
```

Destruction deletes the Aurora database, virtual keys, budgets, and spend history **without a final snapshot**. Confirm that the `SpeedRunAiGateway` stack reaches `DELETE_COMPLETE` in CloudFormation. The shared `CDKToolkit` bootstrap stack and its ECR assets may remain; only remove assets after checking that other deployed stacks don't need them.

## Security and production gaps

This implementation intentionally does **not** provide public application ingress, customer identity or authorization, AWS tenant isolation, multi-task HA, autoscaling, database credential separation for migrations, a disaster recovery plan, or complete monitoring and alerting. Virtual keys are useful application controls, not a substitute for AWS account isolation. Treat the SSM access path and the master secret as privileged administrator capabilities.

## Project map

| Path | Purpose |
| --- | --- |
| `lib/gateway-stack.ts` | CDK VPC, ECS, Aurora, secrets, IAM, logging |
| `gateway/` | LiteLLM image, model configuration and startup |
| `scripts/tunnel.sh` | SSM port forwarding |
| `scripts/smoke.py` | Live deployed-gateway smoke test |
| `scripts/test_budgets.py` | Live independent-budget enforcement test |
| `scripts/test_reliability.py` | Offline mock-based reliability test |
| `test/` | TypeScript and Python unit tests |
| `Makefile` | Repeatable install, deploy, verify and teardown commands |
