# AI Gateway on Amazon ECS

November 11 infrastructure milestone for AWS Builder Center Speed Run 3. The gateway exposes an OpenAI-compatible API through LiteLLM Proxy and invokes Amazon Nova Micro in Amazon Bedrock. Aurora PostgreSQL Serverless v2 stores virtual keys, their budgets, and spend records.

```text
Operator and API client
  -> SSM tunnel on localhost:4000
  -> LiteLLM Proxy on ECS Fargate
       -> Bedrock Converse using the ECS task role
       -> Aurora PostgreSQL over TLS
```

The demo uses one AWS account in `us-east-1`. Fargate gets an outbound public IPv4 address to reach AWS APIs and ECR without a NAT gateway. Its security group has no inbound rules, and LiteLLM binds to loopback. Aurora runs in isolated subnets with encrypted storage and PostgreSQL access from the task security group. ECS injects database credentials and the gateway admin credential from Secrets Manager. The task role can invoke only the configured Bedrock foundation model. CloudWatch keeps container logs for seven days.

This demo uses one task and one database writer. A task update briefly interrupts service so only one LiteLLM instance runs schema initialization. The runtime uses the Aurora cluster user for schema initialization and proxy operations. Use short-lived virtual keys for API calls. Treat permission to open SSM sessions on the task as privileged access to the container and its network. Restrict it to trusted operators and the session document they need. [AWS Session Manager documentation](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-sessions-start.html)

## Check locally

Install Node.js 24, pnpm 12.18.0, Python 3 and Make.

```sh
make install
make check
make synth
```

The tests assert the synthesized network, IAM, ECS and RDS settings and check connection URL escaping and TLS configuration. Synthesis requires no AWS login or Docker daemon. To build the image locally, run `docker build --platform linux/amd64 -t speed-run-gateway ./gateway`.

## Deploy and verify

Use AWS CLI v2 with credentials for the intended demo account and permission to deploy CDK stacks. A running Docker daemon is required to publish the image to the CDK bootstrap ECR repository. Install the AWS Session Manager plugin for port forwarding. Confirm that Nova Micro on-demand inference and Aurora PostgreSQL 16.8 Serverless v2 are available to your account in `us-east-1`. [Nova Micro availability](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-amazon-nova-micro.html) · [Aurora PostgreSQL versions](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraPostgreSQLReleaseNotes/aurorapostgresql-release-calendar.html)

```sh
export AWS_PROFILE=your-demo-profile
make bootstrap                 # Once per account and region
make diff
make deploy
make tunnel                    # Leave running in this terminal
```

In another terminal with the same AWS profile, run `make smoke`. It checks readiness and unauthenticated rejection, creates a temporary model-scoped virtual key with a USD 0.01 budget, reads the stored policy back, calls Nova Micro, and deletes the key. The operator needs CloudFormation output reads, ECS task discovery, and SSM session permissions. The smoke operator also needs `secretsmanager:GetSecretValue` for the stack's master-key secret. The test keeps both keys in memory and prints only its result.

The LiteLLM Admin UI is at `http://127.0.0.1:4000/ui` while the tunnel is open. The master key is `sk-` followed by the `key` field of the secret identified by `MasterKeySecretArn`. To check persistence across task replacement, create a disposable virtual key with a small budget in the UI, force a new ECS deployment, reopen the tunnel to the replacement task, and confirm the same key can still invoke the model with its original budget. Delete the key afterwards.

## Cost and teardown

At 730 hours per month in Northern Virginia, one Linux/x86 task (0.5 vCPU, 2 GiB) costs about **$21**, its public IPv4 address **$3.65**, and Aurora at its 0.5 ACU floor **$43.80**. The minimum compute and IPv4 subtotal is about **$69/month** before database storage and I/O, CloudWatch, Secrets Manager, ECR, and Bedrock token charges. At 2 ACUs throughout the month, Aurora compute alone would cost about **$175/month**. LiteLLM key budgets govern model usage through the gateway. AWS bills infrastructure independently. [Fargate](https://aws.amazon.com/fargate/pricing/) · [VPC](https://aws.amazon.com/vpc/pricing/) · [Aurora](https://aws.amazon.com/rds/aurora/pricing/) · [Bedrock](https://aws.amazon.com/bedrock/pricing/)

Close the tunnel and run `make destroy` in the intended profile. Destruction deletes the demo database and its keys, budgets and spend records without a final snapshot. Export any data you need first. Verify `SpeedRunAiGateway` reached `DELETE_COMPLETE`. The shared `CDKToolkit` stack and its ECR assets may remain. Remove unused assets according to the account's cleanup procedure, keeping assets needed by other stacks.

## Milestone verification

The original requirements and quality-explainer attachments were unavailable in this workspace. The architecture follows the November 11 handoff. See the pull request for the checks run and the status of live deployment.
