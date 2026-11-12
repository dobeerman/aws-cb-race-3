#!/usr/bin/env bash
set -euo pipefail

command -v session-manager-plugin >/dev/null || {
  echo "Install the AWS Session Manager plugin before opening a tunnel." >&2
  exit 1
}
export AWS_PAGER=
stack=SpeedRunAiGateway

output() {
  aws cloudformation describe-stacks --stack-name "$stack" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue | [0]" --output text
}

cluster=$(output ClusterName)
service=$(output ServiceName)
task=$(aws ecs list-tasks --cluster "$cluster" --service-name "$service" \
  --desired-status RUNNING --query 'taskArns[0]' --output text)
if [[ "$task" == None ]]; then
  echo "The gateway has no running task." >&2
  exit 1
fi

runtime_id=$(aws ecs describe-tasks --cluster "$cluster" --tasks "$task" \
  --query "tasks[0].containers[?name=='gateway'].runtimeId | [0]" --output text)
if [[ -z "$runtime_id" || "$runtime_id" == None ]]; then
  echo "The gateway container has no runtime ID yet." >&2
  exit 1
fi

exec aws ssm start-session --target "ecs:${cluster}_${task##*/}_${runtime_id}" \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["4000"],"localPortNumber":["4000"]}'
