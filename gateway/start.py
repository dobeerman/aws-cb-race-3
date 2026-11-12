import os
from urllib.parse import quote, urlencode


def database_url(user: str, password: str, host: str) -> str:
    credentials = f"{quote(user, safe='')}:{quote(password, safe='')}"
    options = urlencode({
        "sslmode": "require",
        "sslaccept": "strict",
        "sslcert": "/app/rds-ca.pem",
    })
    return f"postgresql://{credentials}@{host}:5432/litellm?{options}"


def main() -> None:
    os.environ["DATABASE_URL"] = database_url(
        os.environ.pop("DB_USER"), os.environ.pop("DB_PASSWORD"), os.environ["DB_HOST"]
    )
    os.environ["LITELLM_MASTER_KEY"] = "sk-" + os.environ.pop("LITELLM_MASTER_KEY_SUFFIX")
    os.execv("/app/docker/prod_entrypoint.sh", [
        "/app/docker/prod_entrypoint.sh", "--config", "/app/gateway/config.yaml",
        "--host", "127.0.0.1", "--port", "4000",
    ])


if __name__ == "__main__":
    main()
