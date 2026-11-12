STACK := SpeedRunAiGateway
export AWS_REGION := us-east-1
export AWS_DEFAULT_REGION := us-east-1

.PHONY: install check synth diff bootstrap deploy tunnel smoke test-budgets test-reliability destroy verify

install:
	pnpm install --frozen-lockfile

check:
	pnpm typecheck
	pnpm test
	PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s test -p 'test_*.py'

synth:
	pnpm cdk synth $(STACK) --strict

diff:
	pnpm cdk diff $(STACK) --no-change-set

bootstrap:
	@account=$$(aws sts get-caller-identity --query Account --output text) && \
		pnpm cdk bootstrap "aws://$$account/$(AWS_REGION)"

deploy:
	docker info >/dev/null
	aws sts get-caller-identity --query Account --output text
	pnpm cdk deploy $(STACK) --require-approval broadening

tunnel:
	bash scripts/tunnel.sh

smoke:
	python3 scripts/smoke.py

test-budgets:
	PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_budgets.py

test-reliability:
	PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_reliability.py

destroy:
	pnpm cdk destroy $(STACK)

verify: check smoke test-budgets test-reliability
	@echo "All verification steps passed."
