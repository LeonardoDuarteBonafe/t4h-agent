"""Runtime guarantees with local stores and fakes, never an API or credential."""

from __future__ import annotations

from copy import deepcopy
import asyncio
from pathlib import Path
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from triagem_n1.agents import Agents, Classification, ModelError, Proposal, Review
from triagem_n1.config import Settings
from triagem_n1.policy import fingerprint
from triagem_n1.runtime import AmbiguousOperation, Runtime, StaleCase
from triagem_n1.world import WorldError


DATA = Path(__file__).resolve().parents[2] / 'Servidor MCP (Compartilhado)' / 'dados' / 'triagem'


def knowledge_fixture():
    return {
        'faq': json.loads((DATA / 'faq.json').read_text(encoding='utf-8')),
        'catalogo': json.loads((DATA / 'catalogo.json').read_text(encoding='utf-8')),
        'times': json.loads((DATA / 'times.json').read_text(encoding='utf-8')),
        'chamados': json.loads((DATA / 'chamados_abertos.json').read_text(encoding='utf-8')),
        'incidentes': [],
        'mensagens': [],
    }


def event(event_id='MAN-TEST-1', **changes):
    value = dict(id=event_id, de='pessoa.teste@t4h.exemplo', nome='Pessoa Teste',
                 assunto='Como usar VPN', corpo='Como configuro minha VPN?',
                 status='lido', acoes=[])
    value.update(changes)
    return value


def proposal(**changes):
    value = dict(kind='faq', sector='Infraestrutura', summary='Orientação de VPN',
                 faq_id='FAQ-01', response='Este rascunho não pode substituir o texto da FAQ.',
                 reason='A dúvida corresponde à FAQ-01.', sources=['FAQ-01'])
    value.update(changes)
    return Proposal(**value).model_dump()


class FakeWorld:
    """Only the contracts exercised by these tests; unknown calls fail closed."""

    base_url = 'http://127.0.0.1:1'

    def __init__(self):
        self.sources = knowledge_fixture()
        self.events = {'MAN-TEST-1': event()}
        self.calls = []
        self.queries = []
        self.reads = []
        self.fail_after = None

    async def status(self):
        return {'encerrado': False, 'agora': '2026-12-07T08:00:00'}

    async def query(self, base, **args):
        self.queries.append((base, deepcopy(args)))
        rows = deepcopy(self.sources[base])
        filters = args.get('filtro', {})
        return [r for r in rows if all(str(v) in str(r.get(k, '')) for k, v in filters.items())]

    async def read_event(self, event_id, **args):
        self.reads.append(event_id)
        return deepcopy(self.events[event_id])

    async def call(self, tool, **args):
        self.calls.append((tool, deepcopy(args)))
        if tool == 'registrar_conversa':
            result = {'status': 'ok'}
        elif tool == 'responder_evento':
            target = self.events[args['evento_id']]
            target['status'] = 'respondido'
            target['acoes'].append({'ferramenta': tool, 'agente': 'triagista'})
            result = {'status': 'ok', 'mensagem': 'Resposta enviada.'}
        elif tool == 'registrar':
            row = dict(deepcopy(args['dados']), id='CH-TEST-1', pedidos=[args['evento_id']])
            self.sources[args['base']].append(row)
            result = {'status': 'ok', 'registro': row}
        elif tool == 'listar_eventos':
            rows = [{k: e.get(k) for k in ('id', 'de', 'assunto', 'status', 'responde_a')}
                    for e in self.events.values() if e['status'] == args.get('status')]
            result = {'status': 'ok', 'eventos': rows, 'tem_mais': False}
        else:
            raise AssertionError(f'Unexpected fake-world call: {tool}')
        if tool == self.fail_after:
            self.fail_after = None
            raise WorldError(504, 'The response was lost after the effect.', ambiguous=True)
        return result


class FakeAgents:
    def __init__(self):
        self.calls = 0
        self.last_latency = 0
        self.invocations = []

    async def classify(self, context):
        self.calls += 1
        self.invocations.append(('triagista', 'classify'))
        return Classification(sector='Infraestrutura', category='duvida',
                              destination='especialista_tecnico', reason='FAQ de VPN aplicável.')

    async def propose(self, role, context):
        self.calls += 1
        self.invocations.append((role, 'propose'))
        return Proposal(**proposal())

    async def review(self, context):
        self.calls += 1
        self.invocations.append(('triagista', 'review'))
        return Review(verdict='approved', reason='Fonte e escopo conferidos.')


class ConfigurationTests(unittest.TestCase):
    def test_other_model_environment_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {'LLM_MODEL': 'unapproved-model'}):
                with self.assertRaisesRegex(ValueError, 'gpt-4o-mini'):
                    Settings(api_key='unit-test-placeholder', data_dir=Path(directory))

    def test_other_model_constructor_is_rejected_even_without_environment_override(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ):
                os.environ.pop('LLM_MODEL', None)
                with self.assertRaisesRegex(ValueError, 'gpt-4o-mini'):
                    Settings(api_key='unit-test-placeholder', model='unapproved-model',
                             data_dir=Path(directory))


class ModelBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_failure_never_changes_model_or_falls_back(self):
        posted = []

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, url, **args):
                posted.append(args['json'])
                return type('Response', (), {'status_code': 429})()

        settings = type('FakeSettings', (), {
            'api_key': 'unit-test-placeholder', 'base_url': 'https://unused.invalid/v1',
        })()
        agents = Agents(settings)
        with patch('triagem_n1.agents.httpx.AsyncClient', return_value=Client()):
            with patch('triagem_n1.agents.asyncio.sleep', new=AsyncMock()):
                with self.assertRaises(ModelError):
                    await agents.classify({'events': []})
        self.assertEqual(len(posted), 3)
        self.assertEqual({item['model'] for item in posted}, {'gpt-4o-mini'})


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {'LLM_MODEL': 'gpt-4o-mini'}):
            settings = Settings(api_key='unit-test-placeholder',
                                base_url='https://unused.invalid/v1',
                                mcp_url='http://127.0.0.1:1/mcp',
                                run_id='unit-test-round', data_dir=Path(self.temp.name),
                                auto_start=False)
        self.runtime = Runtime(settings)
        self.world = FakeWorld()
        self.agents = FakeAgents()
        self.runtime.world = self.world
        self.runtime.agents = self.agents
        self.runtime.now = '2026-12-07T08:00:00'

    async def asyncTearDown(self):
        await self.runtime.stop()
        self.temp.cleanup()

    def seed_case(self, **changes):
        plan = proposal()
        case = dict(id='CAS-TEST', subject='Como usar VPN', sender='pessoa.teste@t4h.exemplo',
                    event_ids=['MAN-TEST-1'], events=[event()], world_records=[],
                    status='novo', version=fingerprint(plan), proposal=plan,
                    review={'verdict': 'approved'}, needs_human=False, summary='Dúvida de VPN')
        case.update(changes)
        return self.runtime.store.put_case(case)

    async def test_decision_with_old_version_does_not_record_or_queue(self):
        case = self.seed_case(status='aguardando_aprovacao', needs_human=True)
        with self.assertRaisesRegex(ValueError, 'mudou'):
            await self.runtime.decide(case['id'], 'old-version', 'Pessoa', 'approve')
        self.assertEqual(self.runtime.store.decisions(case['id']), [])
        self.assertEqual(self.runtime.queue.qsize(), 0)
        self.assertEqual(self.runtime.store.get_case(case['id'])['status'], 'aguardando_aprovacao')

    async def test_approval_is_consumed_once_and_double_click_is_blocked(self):
        case = self.seed_case(status='aguardando_aprovacao', needs_human=True)
        first = await self.runtime.decide(case['id'], case['version'], 'Pessoa', 'approve')
        with self.assertRaisesRegex(ValueError, 'incompatível'):
            await self.runtime.decide(case['id'], case['version'], 'Pessoa', 'approve')
        self.assertEqual(first['status'], 'aprovado')
        self.assertEqual(len(self.runtime.store.decisions(case['id'])), 1)
        self.assertEqual(self.runtime.queue.qsize(), 1)

    async def test_rejection_is_preserved_without_queueing_execution(self):
        case = self.seed_case(status='aguardando_aprovacao', needs_human=True)
        rejected = await self.runtime.decide(case['id'], case['version'], 'Pessoa', 'reject', 'Sem autorização')
        self.assertEqual(rejected['status'], 'recusado')
        self.assertEqual(rejected['approval']['decision'], 'reject')
        self.assertEqual(self.runtime.queue.qsize(), 0)
        self.assertEqual(self.world.calls, [])

    async def test_rejection_cancels_only_a_request_created_by_this_case(self):
        case = self.seed_case(status='aguardando_aprovacao', world_records=[{'base': 'chamados', 'id': 'CH-TEST-NEW'}])
        self.world.sources['chamados'].append({'id': 'CH-TEST-NEW', 'solicitante': case['sender'], 'status': 'aguardando aprovação'})
        self.runtime.store.operation('creation', case['id'], 'registrar', {}, 'confirmed', {'registro': {'id': 'CH-TEST-NEW'}})
        self.runtime.operation = AsyncMock(return_value={'status': 'ok'})
        result = await self.runtime.decide(case['id'], case['version'], 'Gestor de teste', 'reject', 'Não autorizado')
        self.assertEqual(result['status'], 'recusado')
        self.runtime.operation.assert_awaited_once()
        args = self.runtime.operation.call_args.kwargs
        self.assertEqual(args['id'], 'CH-TEST-NEW')
        self.assertEqual(args['dados']['status'], 'cancelado')
        self.assertIn('Gestor de teste', args['dados']['observacao'])

    async def test_rejection_does_not_cancel_a_preexisting_linked_ticket(self):
        case = self.seed_case(status='aguardando_aprovacao', world_records=[{'base': 'chamados', 'id': 'CH-0901'}])
        self.runtime.operation = AsyncMock()
        await self.runtime.decide(case['id'], case['version'], 'Gestor de teste', 'reject')
        self.runtime.operation.assert_not_awaited()

    async def test_rejection_preserves_human_decision_when_ticket_changed(self):
        case = self.seed_case(status='aguardando_aprovacao', world_records=[{'base': 'chamados', 'id': 'CH-TEST-NEW'}])
        self.world.sources['chamados'].append({'id': 'CH-TEST-NEW', 'solicitante': 'outra.pessoa@t4h.exemplo', 'status': 'aberto'})
        self.runtime.store.operation('creation', case['id'], 'registrar', {}, 'confirmed', {'registro': {'id': 'CH-TEST-NEW'}})
        self.runtime.operation = AsyncMock()
        result = await self.runtime.decide(case['id'], case['version'], 'Gestor de teste', 'reject')
        self.assertEqual(result['approval']['decision'], 'reject')
        self.assertTrue(result['needs_human'])
        self.runtime.operation.assert_not_awaited()

    async def test_exclusive_human_case_cannot_be_approved(self):
        case = self.seed_case(status='escalado', exclusive=True, needs_human=True)
        with self.assertRaisesRegex(ValueError, 'incompatível'):
            await self.runtime.decide(case['id'], case['version'], 'Pessoa', 'approve')
        accepted = await self.runtime.decide(case['id'], case['version'], 'Pessoa', 'acknowledge')
        self.assertEqual(accepted['status'], 'com_humano')
        self.assertEqual(accepted['human_owner'], 'Pessoa')
        self.assertEqual(self.world.calls, [])

    async def test_faq_uses_exact_world_text_and_journals_confirmed_reply(self):
        case = self.seed_case()
        await self.runtime.execute(case['id'], self.world.sources)
        replies = [args for tool, args in self.world.calls if tool == 'responder_evento']
        faq = next(f for f in self.world.sources['faq'] if f['id'] == 'FAQ-01')
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]['mensagem'], faq['resposta'])
        self.assertNotEqual(replies[0]['mensagem'], case['proposal']['response'])
        self.assertEqual(self.runtime.store.get_case(case['id'])['status'], 'respondido')
        self.assertEqual(self.runtime.store.list_operations(case['id'])[0]['status'], 'confirmed')
        self.assertEqual(self.runtime.store.lessons()[0]['source']['id'], 'FAQ-01')

    async def test_confirmed_operation_is_returned_without_another_remote_call(self):
        case = self.seed_case()
        args = dict(evento_id='MAN-TEST-1', mensagem='Resposta de teste', aguardar_resposta=False)
        first = await self.runtime.operation(case, 'resposta', 'responder_evento', **args)
        second = await self.runtime.operation(case, 'resposta', 'responder_evento', **args)
        self.assertEqual(first, second)
        self.assertEqual(len(self.world.calls), 1)
        self.assertEqual(len(self.runtime.store.list_operations(case['id'])), 1)

    async def test_same_proposal_can_reply_once_to_each_related_event(self):
        case = self.seed_case()
        first_args = dict(evento_id='MAN-TEST-1', mensagem='Resposta de teste', aguardar_resposta=False)
        await self.runtime.operation(case, 'resposta', 'responder_evento', **first_args)
        reply = event('MAN-TEST-2', responde_a='MAN-TEST-1')
        self.world.events[reply['id']] = reply
        updated = self.runtime.store.put_case(dict(
            id=case['id'], event_ids=case['event_ids'] + [reply['id']],
            events=case['events'] + [reply]))
        self.assertEqual(updated['version'], case['version'])
        second_args = dict(first_args, evento_id=reply['id'])
        await self.runtime.operation(updated, 'resposta', 'responder_evento', **second_args)
        await self.runtime.operation(updated, 'resposta', 'responder_evento', **second_args)
        replies = [args for tool, args in self.world.calls if tool == 'responder_evento']
        self.assertEqual([args['evento_id'] for args in replies], ['MAN-TEST-1', 'MAN-TEST-2'])
        self.assertEqual(len(self.runtime.store.list_operations(case['id'])), 2)

    async def test_changed_payload_for_same_event_is_not_reported_as_previous_success(self):
        case = self.seed_case()
        args = dict(evento_id='MAN-TEST-1', mensagem='Resposta revisada', aguardar_resposta=False)
        await self.runtime.operation(case, 'resposta', 'responder_evento', **args)
        with self.assertRaisesRegex(ValueError, 'intenção registrada mudou'):
            await self.runtime.operation(case, 'resposta', 'responder_evento',
                                         **dict(args, mensagem='Texto diferente sem nova versão'))
        self.assertEqual(len(self.world.calls), 1)
        self.assertEqual(len(self.runtime.store.list_operations(case['id'])), 1)

    async def test_legacy_confirmed_reply_is_preserved_and_new_event_gets_its_own_intent(self):
        case = self.seed_case()
        args = dict(evento_id='MAN-TEST-1', mensagem='Resposta antiga', aguardar_resposta=False)
        legacy_key = f'{case["id"]}:{case["version"]}:resposta'
        self.runtime.store.operation(legacy_key, case['id'], 'responder_evento', args,
                                     'confirmed', {'status': 'ok', 'legacy': True})
        reused = await self.runtime.operation(case, 'resposta', 'responder_evento', **args)
        self.assertTrue(reused['legacy'])
        self.assertEqual(self.world.calls, [])
        new_event = event('MAN-TEST-2', responde_a='MAN-TEST-1')
        self.world.events[new_event['id']] = new_event
        updated = self.runtime.store.put_case(dict(id=case['id'],
            event_ids=case['event_ids'] + [new_event['id']], events=case['events'] + [new_event]))
        await self.runtime.operation(updated, 'resposta', 'responder_evento',
                                     **dict(args, evento_id=new_event['id']))
        self.assertEqual(len(self.world.calls), 1)
        self.assertEqual(len(self.runtime.store.list_operations(case['id'])), 2)
        self.assertEqual(self.runtime.store.get_operation(legacy_key)['status'], 'confirmed')

    async def test_existing_creation_cannot_create_again_for_a_new_related_event(self):
        case = self.seed_case()
        args = dict(base='chamados', dados={'tipo': 'Outros pedidos', 'campos': {'descricao': 'Teste'}},
                    evento_id='MAN-TEST-1')
        await self.runtime.operation(case, 'registro', 'registrar', **args)
        new_event = event('MAN-TEST-2', responde_a='MAN-TEST-1')
        updated = self.runtime.store.put_case(dict(id=case['id'],
            event_ids=case['event_ids'] + [new_event['id']], events=case['events'] + [new_event]))
        with self.assertRaisesRegex(ValueError, 'associe o novo evento'):
            await self.runtime.operation(updated, 'registro', 'registrar',
                                         **dict(args, evento_id=new_event['id']))
        self.assertEqual([tool for tool, args in self.world.calls], ['registrar'])
        self.assertEqual(len(self.runtime.store.list_operations(case['id'])), 1)

    async def test_event_arriving_during_reply_baseline_blocks_the_stale_write(self):
        case = self.seed_case()
        read_event = self.world.read_event

        async def delayed_baseline(event_id, **args):
            value = await read_event(event_id, **args)
            new_event = event('MAN-TEST-2', responde_a='MAN-TEST-1')
            self.runtime.store.put_case(dict(id=case['id'],
                event_ids=case['event_ids'] + [new_event['id']], events=case['events'] + [new_event]))
            return value

        self.world.read_event = delayed_baseline
        with self.assertRaises(StaleCase):
            await self.runtime.operation(case, 'resposta', 'responder_evento',
                evento_id='MAN-TEST-1', mensagem='Resposta antiga', aguardar_resposta=False)
        self.assertEqual(self.world.calls, [])
        self.assertEqual(self.runtime.store.list_operations(case['id']), [])

    async def test_unconfirmed_previous_operation_is_not_blindly_repeated(self):
        case = self.seed_case()
        args = dict(base='chamados', dados={'tipo': 'Outros pedidos', 'campos': {'descricao': 'Teste'}},
                    evento_id='MAN-TEST-1')
        key = f'{case["id"]}:{case["version"]}:registro'
        self.runtime.store.operation(key, case['id'], 'registrar', args, 'ambiguous')
        with self.assertRaises(AmbiguousOperation):
            await self.runtime.operation(case, 'registro', 'registrar', **args)
        self.assertEqual(self.world.calls, [])
        self.assertEqual(self.runtime.store.get_operation(key)['status'], 'ambiguous')

    async def test_504_after_creation_is_reconciled_without_second_creation(self):
        case = self.seed_case()
        self.world.fail_after = 'registrar'
        result = await self.runtime.operation(case, 'registro', 'registrar', base='chamados',
            dados={'tipo': 'Outros pedidos', 'campos': {'descricao': 'Teste'}}, evento_id='MAN-TEST-1')
        self.assertTrue(result['reconciliado'])
        self.assertEqual(result['registro']['id'], 'CH-TEST-1')
        self.assertEqual([tool for tool, args in self.world.calls], ['registrar'])
        self.assertEqual(self.runtime.store.list_operations(case['id'])[0]['status'], 'confirmed')

    async def test_response_is_joined_to_case_by_responde_a_and_not_duplicated(self):
        first = event('PED-ORIGINAL', assunto='Pedido de reembolso', corpo='Solicito reembolso')
        self.runtime.ingest(first)
        case = self.runtime.store.list_cases()[0]
        self.runtime.store.put_case(dict(id=case['id'], status='aguardando_dados', approval={'version': 'old'}))
        reply = event('PED-ORIGINAL-R1', assunto='RE: pedido', corpo='Valor e comprovante informados',
                      responde_a='PED-ORIGINAL')
        self.runtime.ingest(reply)
        self.runtime.ingest(reply)
        cases = self.runtime.store.list_cases()
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]['event_ids'], ['PED-ORIGINAL', 'PED-ORIGINAL-R1'])
        self.assertEqual(len(cases[0]['events']), 2)
        self.assertIsNone(cases[0]['approval'])
        self.assertEqual(cases[0]['status'], 'novo')

    async def test_lost_collect_response_recovers_read_event_only_once(self):
        self.world.events = {'PED-LOST': event('PED-LOST')}
        await self.runtime._recover()
        await self.runtime._recover()
        cases = self.runtime.store.list_cases()
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]['event_ids'], ['PED-LOST'])
        self.assertEqual(self.world.reads, ['PED-LOST'])
        self.assertEqual(self.runtime.queue.qsize(), 1)

    async def test_analysis_invokes_specialist_then_a_separate_triage_review(self):
        self.runtime.ingest(event())
        case = self.runtime.store.list_cases()[0]
        await self.runtime.analyze(case['id'])
        self.assertEqual(self.agents.invocations,
            [('triagista', 'classify'), ('especialista_tecnico', 'propose'), ('triagista', 'review')])
        traces = self.runtime.store.traces(case['id'])
        self.assertEqual([t['stage'] for t in traces], ['classificacao', 'proposta', 'revisao'])
        consulted_roles = {args['agent'] for base, args in self.world.queries}
        self.assertEqual(consulted_roles, {'triagista', 'especialista_tecnico'})
        self.assertEqual(self.runtime.store.get_case(case['id'])['status'], 'respondido')

    async def test_complement_during_analysis_invalidates_old_plan_and_requeues_case(self):
        original = event('PED-CONCURRENT', assunto='Como usar VPN')
        reply = event('PED-CONCURRENT-R1', assunto='RE: Como usar VPN',
                      corpo='Complemento que chegou durante a primeira análise.',
                      responde_a=original['id'])
        self.world.events = {original['id']: original, reply['id']: reply}
        entered_first_classification = asyncio.Event()
        release_first_classification = asyncio.Event()
        classified_contexts = []
        classify = self.agents.classify

        async def delayed_classify(context):
            classified_contexts.append([e['id'] for e in context['events']])
            if len(classified_contexts) == 1:
                entered_first_classification.set()
                await release_first_classification.wait()
            return await classify(context)

        self.agents.classify = delayed_classify
        self.runtime.ingest(original)
        case_id = self.runtime.store.list_cases()[0]['id']
        self.runtime.running = True
        worker = asyncio.create_task(self.runtime._work())
        self.runtime.tasks.append(worker)
        await asyncio.wait_for(entered_first_classification.wait(), timeout=2)
        try:
            self.runtime.ingest(reply)
            self.assertIn(case_id, self.runtime.dirty)
            self.assertFalse(any(tool == 'responder_evento' for tool, args in self.world.calls))
        finally:
            release_first_classification.set()

        async def wait_for_reanalysis_to_finish():
            while True:
                case = self.runtime.store.get_case(case_id)
                if len(classified_contexts) >= 2 and case['status'] == 'respondido':
                    return case
                await asyncio.sleep(.01)

        case = await asyncio.wait_for(wait_for_reanalysis_to_finish(), timeout=3)
        self.assertEqual(classified_contexts,
                         [[original['id']], [original['id'], reply['id']]])
        self.assertEqual(case['event_ids'], [original['id'], reply['id']])
        self.assertEqual(len(case['events']), 2)
        replies = [args for tool, args in self.world.calls if tool == 'responder_evento']
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]['evento_id'], reply['id'])
        self.assertEqual(len(self.runtime.store.list_operations(case_id)), 1)
        self.assertNotIn(case_id, self.runtime.dirty)


if __name__ == '__main__':
    unittest.main()
