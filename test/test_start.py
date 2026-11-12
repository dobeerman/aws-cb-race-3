import unittest
from urllib.parse import parse_qs, unquote, urlsplit

from gateway.start import database_url


class DatabaseUrlTest(unittest.TestCase):
    def test_reserved_characters_and_tls(self):
        password = "p@ss:/?#%+ word"
        parsed = urlsplit(database_url("gateway_admin", password, "cluster.example.com"))

        self.assertEqual(parsed.scheme, "postgresql")
        self.assertEqual(parsed.hostname, "cluster.example.com")
        self.assertEqual(parsed.port, 5432)
        self.assertEqual(parsed.path, "/litellm")
        self.assertEqual(parsed.username, "gateway_admin")
        self.assertEqual(unquote(parsed.password), password)
        self.assertEqual(parse_qs(parsed.query), {
            "sslmode": ["require"],
            "sslaccept": ["strict"],
            "sslcert": ["/app/rds-ca.pem"],
        })
