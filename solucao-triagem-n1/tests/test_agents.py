"""Contract repair and independent review without API calls."""
import json
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock
import unittest
from triagem_n1.agents import Agents, Classification, PlanAudit


class AgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.agents = Agents(SimpleNamespace(api_key='test-placeholder', base_url='https://unused.invalid/v1'))

    async def test_invalid_schema_is_repaired_with_precise_feedback(self):
        sent = []
        outputs = [{'sector': 'Infraestrutura'},
                   {'sector': 'Infraestrutura', 'category': 'FAQ', 'destination': 'especialista_tecnico', 'reason': 'VPN'}]
        class Client:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
            async def post(self, url, **kwargs):
                sent.append(json.loads(json.dumps(kwargs['json'])))
                value = outputs.pop(0)
                return SimpleNamespace(status_code=200, json=lambda: {'choices': [{'message': {'content': json.dumps(value)}}]})
        with patch('triagem_n1.agents.httpx.AsyncClient', return_value=Client()):
            result = await self.agents.classify({'events': []})
        self.assertIsInstance(result, Classification)
        self.assertEqual(len(sent), 2)
        self.assertEqual({r['model'] for r in sent}, {'gpt-4o-mini'})
        self.assertIn('destination', sent[1]['messages'][-1]['content'])
        self.assertEqual(sent[1]['messages'][-2]['role'], 'assistant')

    async def test_review_does_not_inherit_incorrect_initial_missing_fields(self):
        invoke = AsyncMock(return_value=PlanAudit(decision='valid', reason='Plano com aprovação pendente válido.'))
        self.agents._invoke = invoke
        result = await self.agents.review({
            'events': [{'corpo': 'Acesso leitura CRM para vendas'}],
            'classification': {'missing': ['sistema']},
            'proposal': {'catalog_type': 'Acesso a sistema', 'fields': {'sistema': 'CRM'}},
            'sources': {'catalogo': [{'tipo': 'Acesso a sistema', 'campos_obrigatorios': ['sistema']}], 'times': []}})
        context = invoke.call_args.args[1]
        self.assertNotIn('classification', context)
        self.assertEqual(context['field_check']['missing_required'], [])
        self.assertEqual(result.verdict, 'approved')

    async def test_human_only_review_cannot_become_a_valid_plan(self):
        self.agents._invoke = AsyncMock(return_value=PlanAudit(decision='human_only', reason='Golpe suspeito.'))
        result = await self.agents.review({'proposal': {}, 'sources': {}})
        self.assertEqual(result.verdict, 'human')


if __name__ == '__main__':
    unittest.main()
