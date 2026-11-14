"""Persistence behavior, independently testable without a model or the world."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from triagem_n1.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.store = Store(self.path, "round-a")

    def tearDown(self):
        self.temp.cleanup()

    def test_case_survives_restart_and_partial_updates_preserve_history(self):
        first = self.store.put_case({"id": "CAS-1", "events": ["MAN-01"], "fields": {"item": "notebook"}})
        updated = self.store.put_case({"id": "CAS-1", "status": "waiting"})
        reopened = Store(self.path, "round-a")
        actual = reopened.get_case("CAS-1")
        self.assertEqual(actual["events"], ["MAN-01"])
        self.assertEqual(actual["fields"], {"item": "notebook"})
        self.assertEqual(actual["status"], "waiting")
        self.assertEqual(first["created_at"], updated["created_at"])
        self.assertNotEqual(first["updated_at"], updated["updated_at"])
        actual["events"].append("not-persisted")
        self.assertEqual(reopened.get_case("CAS-1")["events"], ["MAN-01"])

    def test_repeated_ids_and_audits_are_isolated_by_round(self):
        self.store.put_case({"id": "CAS-1", "status": "approved"})
        self.store.trace("CAS-1", "triagista", "analysis", {"ok": True})
        self.store.decision("CAS-1", "v1", "Operator", "approve")
        self.store.operation("MAN-01:reply", "CAS-1", "reply", {"event": "MAN-01"})
        other = Store(self.path, "round-b")
        self.assertIsNone(other.get_case("CAS-1"))
        self.assertEqual(other.traces("CAS-1"), [])
        self.assertEqual(other.decisions("CAS-1"), [])
        self.assertIsNone(other.get_operation("MAN-01:reply"))
        other.put_case({"id": "CAS-1", "status": "new"})
        self.assertEqual(self.store.get_case("CAS-1")["status"], "approved")
        self.assertEqual(other.get_case("CAS-1")["status"], "new")

    def test_operation_is_one_local_record_and_keeps_outcome(self):
        args = {"base": "chamados", "dados": {"tipo": "Outros pedidos"}}
        intent = self.store.operation("create-1", "CAS-1", "registrar", args)
        done = self.store.operation("create-1", "CAS-1", "registrar", args, "confirmed", {"id": "CH-1001"})
        repeated = self.store.operation("create-1", "CAS-1", "registrar", args)
        self.assertEqual(intent["id"], done["id"])
        self.assertEqual(done["id"], repeated["id"])
        self.assertEqual(repeated["status"], "confirmed")
        self.assertEqual(repeated["result"], {"id": "CH-1001"})
        self.assertEqual(len(self.store.list_operations("CAS-1")), 1)
        with self.assertRaises(ValueError):
            self.store.operation("create-1", "CAS-2", "registrar", args)
        self.assertEqual(self.store.get_operation("create-1")["status"], "confirmed")

    def test_audits_keep_structured_data_and_decision_versions(self):
        self.store.trace("CAS-1", "especialista_tecnico", "proposal", {"source": "FAQ-01"})
        self.store.trace("CAS-1", "triagista", "review", {"approved": False})
        self.store.decision("CAS-1", 2, "Person", "reject", "Wrong scope")
        trace = self.store.traces("CAS-1")
        self.assertEqual([t["stage"] for t in trace], ["proposal", "review"])
        self.assertEqual(trace[0]["data"], {"source": "FAQ-01"})
        self.assertEqual(self.store.decisions("CAS-1")[0]["version"], 2)
        self.assertEqual(self.store.decisions("CAS-1")[0]["decision"], "reject")

    def test_sourced_lessons_survive_new_round_and_are_deduplicated(self):
        self.store.add_lesson("VPN has a published procedure", {"base": "faq", "id": "FAQ-01"})
        second = Store(self.path, "round-b")
        self.assertEqual(second.lessons()[0]["source"]["id"], "FAQ-01")
        second.add_lesson("VPN has a published procedure", {"id": "FAQ-01", "base": "faq"})
        self.assertEqual(len(second.lessons()), 1)
        self.assertEqual(second.lessons()[0]["seen_count"], 2)
        with self.assertRaises(ValueError):
            second.add_lesson("An unsourced claim", "")

    def test_connections_allow_concurrent_distinct_writes(self):
        def create(index):
            return self.store.put_case({"id": f"CAS-{index}", "at": datetime.now(timezone.utc)})
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(create, range(18)))
        self.assertEqual(len(self.store.list_cases()), 18)
        self.assertIsInstance(self.store.get_case("CAS-0")["at"], str)

    def test_invalid_case_is_rejected_without_changing_existing_state(self):
        self.store.put_case({"id": "CAS-1", "status": "new"})
        with self.assertRaises(ValueError):
            self.store.put_case({"status": "bad"})
        self.assertEqual(len(self.store.list_cases()), 1)


if __name__ == "__main__":
    unittest.main()
