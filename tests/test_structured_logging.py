"""Regression coverage for machine-readable, secret-safe bridge event logs."""

import ast
import io
import json
import logging
import os
from pathlib import Path
import unittest
from unittest.mock import patch


class StructuredLoggingTests(unittest.TestCase):
    def test_event_is_json_with_level_event_context_and_redacted_secrets(self):
        """Nexus/Solana incident tooling needs stable context without custody credentials."""
        from src import structured_logging

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(structured_logging.JsonFormatter())
        logger = logging.getLogger("swapService.tests.structured")
        logger.handlers[:] = [handler]
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        try:
            with patch.dict(os.environ, {"NEXUS_PIN": "1234", "HELIUS_API_KEY": "helius-secret"}):
                structured_logging.emit(
                    logger,
                    logging.WARNING,
                    "nexus_debit_held",
                    "Nexus debit needs resolution; pin=1234 helius=helius-secret",
                    intent_id="intent-1",
                    nexus_pin="1234",
                    session="session-secret",
                    chains={"source": "Nexus", "destination": "Solana"},
                )
        finally:
            logger.handlers.clear()

        payload = json.loads(stream.getvalue())
        self.assertEqual(payload["level"], "warning")
        self.assertEqual(payload["event"], "nexus_debit_held")
        self.assertEqual(payload["fields"]["intent_id"], "intent-1")
        self.assertEqual(payload["fields"]["chains"]["source"], "Nexus")
        self.assertEqual(payload["fields"]["nexus_pin"], "***")
        self.assertEqual(payload["fields"]["session"], "***")
        self.assertNotIn("1234", payload["message"])
        self.assertNotIn("helius-secret", payload["message"])
        self.assertIn("timestamp", payload)

    def test_chain_clients_emit_structured_events_instead_of_console_prose(self):
        """Money-path diagnostics must remain machine-readable for incident correlation."""
        source_root = Path(__file__).resolve().parents[1] / "src"
        for name in ("nexus_client.py", "solana_client.py"):
            tree = ast.parse((source_root / name).read_text(encoding="utf-8"))
            console_prints = [
                node for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "print"
            ]
            self.assertEqual(console_prints, [], f"{name} still writes console prose")


if __name__ == "__main__":
    unittest.main()
