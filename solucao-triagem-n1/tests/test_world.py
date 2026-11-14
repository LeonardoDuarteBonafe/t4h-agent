import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from triagem_n1.world import World, WorldError


def sdk_result(payload, error=False):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(payload))], is_error=error)


class WorldTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.world = World()

    def test_decode_sdk_text_and_preserve_clock(self):
        payload = {"status": "ok", "agora": "2026-12-07 08:00", "evento": {"id": "MAN-001"}}
        self.assertEqual(World.decode_result(sdk_result(payload)), payload)

    def test_decode_dictionary_alias_and_application_error(self):
        result = {"content": [{"type": "text", "text": json.dumps({"status": "erro", "codigo": 422, "mensagem": "Faltou campo"})}], "isError": True}
        with self.assertRaises(WorldError) as failure:
            World.decode_result(result, "registrar")
        self.assertEqual(failure.exception.code, 422)
        self.assertFalse(failure.exception.ambiguous)

    def test_malformed_write_result_is_ambiguous(self):
        with self.assertRaises(WorldError) as failure:
            World.decode_result(SimpleNamespace(content=[SimpleNamespace(type="text", text="não é JSON")]), "registrar")
        self.assertEqual(failure.exception.code, 502)
        self.assertTrue(failure.exception.ambiguous)

    async def test_503_retries_up_to_three_total_attempts(self):
        self.world._invoke = AsyncMock(side_effect=[WorldError(503, "offline"), WorldError(503, "offline"), sdk_result({"status": "ok"})])
        with patch("triagem_n1.world.asyncio.sleep", new=AsyncMock()):
            self.assertEqual(await self.world.call("registrar", base="chamados", dados={}), {"status": "ok"})
        self.assertEqual(self.world._invoke.await_count, 3)

    async def test_504_write_does_not_retry_and_is_ambiguous(self):
        self.world._invoke = AsyncMock(return_value=sdk_result({"status": "erro", "codigo": 504, "mensagem": "timeout"}, True))
        with self.assertRaises(WorldError) as failure:
            await self.world.call("enviar_mensagem", para="produto@t4h.exemplo", mensagem="teste")
        self.assertEqual(failure.exception.code, 504)
        self.assertTrue(failure.exception.ambiguous)
        self.assertEqual(self.world._invoke.await_count, 1)

    async def test_504_read_can_retry(self):
        self.world._invoke = AsyncMock(side_effect=[sdk_result({"status": "erro", "codigo": 504}, True), sdk_result({"status": "ok", "registros": []})])
        with patch("triagem_n1.world.asyncio.sleep", new=AsyncMock()):
            await self.world.call("consultar", base="faq")
        self.assertEqual(self.world._invoke.await_count, 2)

    async def test_next_event_is_not_blindly_retried(self):
        self.world._invoke = AsyncMock(return_value=sdk_result({"status": "erro", "codigo": 504}, True))
        with self.assertRaises(WorldError) as failure:
            await self.world.call("proximo_evento")
        self.assertTrue(failure.exception.ambiguous)
        self.assertEqual(self.world._invoke.await_count, 1)

    async def test_invalid_application_error_never_retries(self):
        self.world._invoke = AsyncMock(return_value=sdk_result({"status": "erro", "codigo": 422}, True))
        with self.assertRaises(WorldError):
            await self.world.call("registrar", base="chamados", dados={})
        self.assertEqual(self.world._invoke.await_count, 1)

    async def test_agent_identity_is_supplied_by_boundary(self):
        self.world._invoke = AsyncMock(return_value=sdk_result({"status": "ok"}))
        await self.world.call("consultar", "especialista_tecnico", motivo="Conferir FAQ", base="faq", agente="forjado")
        args = self.world._invoke.await_args.args[1]
        self.assertEqual(args["agente"], "especialista_tecnico")
        self.assertEqual(args["motivo"], "Conferir FAQ")

    async def test_query_paginates_with_actual_page_length(self):
        self.world.call = AsyncMock(side_effect=[{"status": "ok", "registros": [{"id": "A"}], "tem_mais": True}, {"status": "ok", "registros": [{"id": "B"}], "tem_mais": False}])
        records = await self.world.query("faq", agent="especialista_administrativo")
        self.assertEqual([record["id"] for record in records], ["A", "B"])
        self.assertEqual(self.world.call.await_args_list[1].kwargs["offset"], 1)
        self.assertEqual(self.world.call.await_args_list[0].kwargs["limite"], 100)

    async def test_query_refuses_non_advancing_pagination(self):
        self.world.call = AsyncMock(return_value={"status": "ok", "registros": [], "tem_mais": True})
        with self.assertRaises(WorldError) as failure:
            await self.world.query("chamados")
        self.assertEqual(failure.exception.code, 502)

    async def test_read_event_unwraps_payload(self):
        self.world.call = AsyncMock(return_value={"status": "ok", "evento": {"id": "MAN-001"}})
        self.assertEqual(await self.world.read_event("MAN-001"), {"id": "MAN-001"})

    async def test_post_event_requires_confirmed_id(self):
        self.world._http = AsyncMock(return_value={"status": "ok"})
        with self.assertRaises(WorldError) as failure:
            await self.world.post_event({"assunto": "teste", "corpo": "mensagem"})
        self.assertTrue(failure.exception.ambiguous)


if __name__ == "__main__":
    unittest.main()
