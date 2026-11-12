# syntax=docker/dockerfile:1
FROM ghcr.io/berriai/litellm-non_root:v1.108.4@sha256:37d91f124eeb23b80a19634a5951091639df236b31cfd6ab238f9f9012097d94
ADD --checksum=sha256:b1711d12bae51838581281e23b6cb97b1074016873b4dafc80ed14002462dd77 --chmod=0444 https://truststore.pki.rds.amazonaws.com/us-east-1/us-east-1-bundle.pem /app/rds-ca.pem
COPY --chmod=0444 config.yaml /app/gateway-config.yaml
COPY --chmod=0444 start.py /app/gateway-start.py
RUN test -r /app/gateway-start.py && test -r /app/gateway-config.yaml && test -r /app/rds-ca.pem
ENTRYPOINT ["python", "/app/gateway-start.py"]
