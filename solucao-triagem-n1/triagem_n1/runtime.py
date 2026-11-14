from __future__ import annotations
import asyncio
from collections import Counter
from datetime import datetime, timezone
import itertools
import json
import re
import time
from uuid import uuid4

from .agents import Agents, ModelError
from .browser import VacationBrowser, BrowserError
from .policy import (TECH, ADMIN, exclusive_reason, safe_event, norm, fingerprint,
                     policy_errors, missing_fields, needs_approval, internal_destination)
from .store import Store
from .world import World, WorldError


class AmbiguousOperation(Exception):
    pass


class StaleCase(Exception):
    pass


class Runtime:
    def __init__(self, settings):
        self.settings = settings
        self.store = Store(settings.data_dir / 'triagem.sqlite3', settings.run_id)
        self.world = World(settings.mcp_url)
        self.agents = Agents(settings)
        self.browser = VacationBrowser(self.world.base_url + '/legado/ferias', settings.data_dir / 'evidencias', settings.headless)
        self.running = False
        self.ended = False
        self.world_connected = False
        self.error = ''
        self.now = ''
        self.queue = asyncio.PriorityQueue()
        self.sequence = itertools.count()
        self.queued = set()
        self.dirty = set()
        self.tasks = []
        self.case_locks = {}
        self.write_lock = asyncio.Lock()
        self.last_recover = 0

    def snapshot(self):
        cases = self.store.list_cases()
        counts = Counter(c.get('status', 'novo') for c in cases)
        return dict(running=self.running, run_id=self.settings.run_id,
                    world_connected=self.world_connected, model='gpt-4o-mini',
                    cases=cases, error=self.error, agora=self.now,
                    stats=dict(total=len(cases), pending=sum(bool(c.get('needs_human')) for c in cases),
                               completed=sum(counts[x] for x in ['respondido', 'encaminhado', 'resolvido']),
                               queue=self.queue.qsize(), model_calls=self.agents.calls,
                               last_latency=self.agents.last_latency, statuses=dict(counts)))

    async def start(self):
        if self.running:
            return self.snapshot()
        if not self.settings.api_key:
            raise ValueError('Configure a chave da API no arquivo .env antes de iniciar.')
        status = await self.world.status()
        if status.get('encerrado'):
            raise ValueError('O mundo encerrou. Inicie outra rodada com outro RUN_ID.')
        self.world_connected = True
        self.ended = False
        self.error = ''
        self.now = status.get('agora', '')
        self.running = True
        for case in self.store.list_cases():
            if case['status'] in ('novo', 'analisando'):
                self.enqueue(case['id'])
            elif case['status'] == 'aprovado':
                self.enqueue(case['id'], 'execute')
        self.tasks = [asyncio.create_task(self._collect()), asyncio.create_task(self._work())]
        return self.snapshot()

    async def stop(self):
        self.running = False
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []
        return self.snapshot()

    def enqueue(self, case_id, job='analyze', priority=10):
        key = (case_id, job)
        if key not in self.queued:
            self.queued.add(key)
            self.queue.put_nowait((priority, next(self.sequence), case_id, job))

    def _correlate(self, event):
        for case in self.store.list_cases():
            if event['id'] in case.get('event_ids', []):
                return case, True
            if event.get('responde_a') in case.get('event_ids', []):
                return case, False
        words = set(re.findall(r'\w+', norm(event.get('assunto', '')))) - {'re', 'resposta', 'complemento', 'pedido', 'sobre'}
        candidates = []
        for case in self.store.list_cases():
            if case.get('sender') != event.get('de') or case.get('status') not in ('aguardando_dados', 'aguardando_aprovacao', 'escalado') or case.get('exclusive'):
                continue
            if any(eid.lower() in norm(event.get('corpo', '') + event.get('assunto', '')) for eid in case['event_ids']):
                return case, False
            previous = set(re.findall(r'\w+', norm(case['subject'])))
            if len(words & previous) >= 1:
                candidates.append(case)
        return (candidates[0], False) if len(candidates) == 1 else (None, False)

    def ingest(self, event):
        case, seen = self._correlate(event)
        if seen:
            return
        if case is None:
            case = dict(id='CAS-' + uuid4().hex[:8].upper(), subject=safe_event(event).get('assunto', ''),
                        sender=event.get('de', ''), event_ids=[], events=[], world_records=[],
                        version='', proposal=None, summary='', sector='', criticality=None)
        reason = exclusive_reason(event)
        case['event_ids'].append(event['id'])
        case['events'].append(safe_event(event))
        case.update(status='novo', error='', needs_human=False, approval=None)
        if reason:
            case.update(exclusive=True, exclusive_reason=reason)
        self.store.put_case(case)
        if (case['id'], 'analyze') in self.queued or (case['id'], 'execute') in self.queued:
            self.dirty.add(case['id'])
        priority = 0 if reason else 1 if any(x in norm(event.get('corpo', '')) for x in ['todos os clientes', 'fora do ar', 'muitos clientes']) else 10
        self.enqueue(case['id'], priority=priority)

    async def _recover(self):
        offset = 0
        while True:
            result = await self.world.call('listar_eventos', motivo='Recuperar eventos lidos após interrupção', status='lido', limite=200, offset=offset)
            for event in result.get('eventos', []):
                known, _ = self._correlate(event)
                if not known or event['id'] not in known['event_ids']:
                    self.ingest(await self.world.read_event(event['id']))
            if not result.get('tem_mais'):
                break
            offset += len(result.get('eventos', []))

    async def _collect(self):
        while self.running:
            try:
                if time.monotonic() - self.last_recover > 45:
                    await self._recover()
                    self.last_recover = time.monotonic()
                result = await self.world.call('proximo_evento', motivo='Buscar novo pedido na fila', esperar_segundos=1)
                self.world_connected = True
                self.now = result.get('agora', self.now)
                if result.get('encerrado'):
                    self.ended = True
                    self.running = False
                    self.error = 'Rodada encerrada; pendências preservadas e escritas suspensas.'
                    return
                if result.get('evento'):
                    self.ingest(result['evento'])
                else:
                    await asyncio.sleep(.4)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.world_connected = False
                self.error = str(exc)
                self.last_recover = 0
                await asyncio.sleep(2)

    async def _work(self):
        while self.running:
            try:
                _, _, case_id, job = await asyncio.wait_for(self.queue.get(), 1)
            except TimeoutError:
                continue
            try:
                lock = self.case_locks.setdefault(case_id, asyncio.Lock())
                async with lock:
                    if job == 'execute':
                        await self.execute(case_id)
                    else:
                        await self.analyze(case_id)
            except asyncio.CancelledError:
                raise
            except StaleCase:
                self.store.put_case(dict(id=case_id, status='novo', needs_human=False))
                self.dirty.add(case_id)
            except Exception as exc:
                status = 'ambiguo' if isinstance(exc, AmbiguousOperation) or getattr(exc, 'ambiguous', False) else 'erro'
                self.store.put_case(dict(id=case_id, status=status, error=str(exc), needs_human=True))
                self.store.trace(case_id, 'triagista', 'falha', {'mensagem': str(exc), 'status': status})
            finally:
                self.queued.discard((case_id, job))
                self.queue.task_done()
                if case_id in self.dirty:
                    self.dirty.discard(case_id)
                    self.enqueue(case_id, priority=1)

    async def knowledge(self, agent):
        data = {}
        for base in ['faq', 'catalogo', 'times', 'chamados', 'incidentes']:
            data[base] = await self.world.query(base, agent=agent, motivo='Consultar fontes para análise e revisão do atendimento')
        return data

    async def conversation(self, case_id, de, para, summary):
        try:
            await self.world.call('registrar_conversa', agent=de, motivo='Espelhar comunicação real dos agentes',
                                  de=de, para=para, mensagem=f'{case_id}: {summary}'[:1900])
        except WorldError:
            pass  # Audit failure does not repeat the business action.

    async def analyze(self, case_id):
        case = self.store.get_case(case_id)
        self.store.put_case(dict(id=case_id, status='analisando', error='', needs_human=False))
        if case.get('exclusive'):
            return await self.escalate(case_id, case.get('exclusive_reason', 'Caso exclusivo de pessoa.'), exclusive=True)
        knowledge = await self.knowledge('triagista')
        context = dict(events=case['events'], sources=knowledge, lessons=self.store.lessons(),
                       agora=self.now, known_records=case.get('world_records', []))
        classified = (await self.agents.classify(context)).model_dump()
        self.store.trace(case_id, 'triagista', 'classificacao', classified)
        self.store.put_case(dict(id=case_id, sector=classified['sector'], criticality=classified.get('criticality'), summary=classified['reason']))
        if case.get('exclusive') or classified['destination'] == 'humano':
            return await self.escalate(case_id, case.get('exclusive_reason') or classified['reason'], exclusive=True)
        agent = 'especialista_tecnico' if classified['sector'] in TECH else 'especialista_administrativo'
        context['classification'] = classified
        context['sources'] = await self.knowledge(agent)
        proposal = (await self.agents.propose(agent, context)).model_dump()
        if proposal.get('faq_id') and any(f['id'] == proposal['faq_id'] for f in knowledge['faq']):
            proposal['sources'] = list(dict.fromkeys(proposal['sources'] + [proposal['faq_id']]))
        self.store.trace(case_id, agent, 'proposta', proposal)
        await self.conversation(case_id, agent, 'triagista', proposal['reason'])
        for attempt in range(2):
            review = (await self.agents.review({**context, 'proposal': proposal})).model_dump()
            self.store.trace(case_id, 'triagista', 'revisao', review)
            if review['verdict'] != 'correct' or attempt:
                break
            context['previous_proposal'] = proposal
            context['corrections'] = review['corrections']
            proposal = (await self.agents.propose(agent, context)).model_dump()
            self.store.trace(case_id, agent, 'correcao', proposal)
        if self.store.get_case(case_id)['event_ids'] != case['event_ids']:
            raise StaleCase('Chegou novo evento durante a análise; reprocessando contexto atualizado.')
        version = fingerprint(proposal)
        self.store.put_case(dict(id=case_id, proposal=proposal, version=version, review=review,
                                sector=proposal['sector'], criticality=proposal.get('criticality'),
                                summary=proposal['summary']))
        await self.conversation(case_id, 'triagista', agent, f'Revisão: {review["verdict"]}. {review["reason"]}')
        errors = policy_errors(proposal, knowledge, case)
        if review['verdict'] != 'approved' or errors:
            return await self.escalate(case_id, '; '.join(errors) or review['reason'])
        await self.execute(case_id, knowledge)

    async def escalate(self, case_id, reason, exclusive=False):
        self.store.put_case(dict(id=case_id, status='escalado', needs_human=True, summary=reason, exclusive=exclusive))
        self.store.trace(case_id, 'triagista', 'escala_humana', {'motivo': reason, 'canal': 'painel próprio'})

    async def _reconcile(self, tool, args, previous=None):
        eid = args.get('evento_id')
        if tool == 'registrar':
            records = await self.world.query(args['base'])
            matches = [r for r in records if eid in r.get('pedidos', []) and
                       (not args['dados'].get('tipo') or args['dados']['tipo'] == r.get('tipo'))]
            if len(matches) == 1:
                return {'status': 'ok', 'registro': matches[0], 'reconciliado': True}
        elif tool == 'enviar_mensagem':
            records = await self.world.query('mensagens', filtro={'evento_id': eid})
            matches = [r for r in records if r.get('para') == args['para'] and r.get('mensagem') == args['mensagem']]
            if len(matches) == 1:
                return {'status': 'ok', 'mensagem_id': matches[0]['id'], 'reconciliado': True}
        elif tool == 'atualizar':
            records = await self.world.query(args['base'], filtro={'id': args['id']})
            for r in records:
                if r['id'] != args['id']:
                    continue
                checks = []
                for key, value in args['dados'].items():
                    checks.append(all(v in r.get('pedidos', []) for v in (value if isinstance(value, list) else [value])) if key == 'pedidos' else r.get(key) == value)
                if all(checks):
                    return {'status': 'ok', 'registro': r, 'reconciliado': True}
        elif tool == 'responder_evento' and previous:
            event = await self.world.read_event(eid)
            before = (previous.get('result') or {}).get('baseline')
            actions = [x for x in event.get('acoes', []) if x.get('ferramenta') == 'responder_evento' and x.get('agente') == 'triagista']
            if before is not None and len(actions) == before + 1:
                return {'status': 'ok', 'mensagem': 'Resposta confirmada pelo novo registro de ação.', 'reconciliado': True}
        return None

    async def operation(self, case, label, tool, **args):
        if self.ended:
            raise WorldError(410, 'O cenário encerrou; nenhuma escrita é permitida.')
        async with self.write_lock:
            current = self.store.get_case(case['id'])
            if current['event_ids'] != case['event_ids'] or current['version'] != case['version']:
                raise StaleCase('O contexto mudou antes da execução.')
            legacy_key = f'{case["id"]}:{case["version"]}:{label}'
            # Replies, notifications and links belong to a particular incoming
            # event even when the model returns an identical proposal hash.
            # Creation is deliberately case-scoped: a new related event must
            # not silently create the same ticket/incident a second time.
            key = legacy_key if tool == 'registrar' else (
                f'{legacy_key}:{fingerprint({"tool": tool, "evento_id": args.get("evento_id")})}'
            )
            previous = self.store.get_operation(key)
            if previous is None and key != legacy_key:
                legacy = self.store.get_operation(legacy_key)
                if legacy and legacy['tool'] == tool and legacy['args'] == args:
                    # Honor an exact pre-upgrade intent instead of executing it
                    # again under the new event-scoped journal key.
                    key, previous = legacy_key, legacy
                elif legacy and legacy['args'].get('evento_id') == args.get('evento_id'):
                    raise ValueError('Uma intenção registrada mudou sem nova revisão; execução bloqueada.')
            if previous and (previous['tool'] != tool or previous['args'] != args):
                raise ValueError('Uma intenção registrada mudou; revise o plano ou associe o novo evento ao registro existente.')
            if previous and previous['status'] == 'confirmed':
                return previous['result']
            if previous:
                recovered = await self._reconcile(tool, args, previous)
                if recovered:
                    self.store.operation(key, case['id'], tool, args, 'confirmed', recovered)
                    return recovered
                if previous['status'] != 'failed' or (previous.get('result') or {}).get('codigo') not in (400, 404, 405, 409, 422, 503):
                    raise AmbiguousOperation('Operação anterior sem confirmação suficiente. Requer conferência humana; repetição bloqueada.')
            baseline = None
            if tool == 'responder_evento':
                event = await self.world.read_event(args['evento_id'])
                baseline = len([x for x in event.get('acoes', []) if x.get('ferramenta') == tool and x.get('agente') == 'triagista'])
            current = self.store.get_case(case['id'])
            if current['event_ids'] != case['event_ids'] or current['version'] != case['version']:
                raise StaleCase('O contexto mudou antes do envio da operação.')
            self.store.operation(key, case['id'], tool, args, 'retrying' if previous else 'intent', {'baseline': baseline})
            try:
                result = await self.world.call(tool, agent='triagista', motivo=case.get('summary', '')[:200], **args)
            except asyncio.CancelledError:
                self.store.operation(key, case['id'], tool, args, 'ambiguous', {'baseline': baseline})
                raise
            except WorldError as exc:
                if exc.ambiguous:
                    previous = self.store.get_operation(key)
                    recovered = await self._reconcile(tool, args, previous)
                    if recovered:
                        self.store.operation(key, case['id'], tool, args, 'confirmed', recovered)
                        return recovered
                    self.store.operation(key, case['id'], tool, args, 'ambiguous', {'baseline': baseline, 'codigo': exc.code})
                    raise AmbiguousOperation('O mundo não confirmou o efeito. Repetição automática bloqueada.') from None
                self.store.operation(key, case['id'], tool, args, 'failed', {'codigo': exc.code, 'mensagem': str(exc)})
                raise
            self.store.operation(key, case['id'], tool, args, 'confirmed', result)
            return result

    async def _reply(self, case, message, wait=False, label='resposta'):
        if not case['sender'].lower().endswith('@t4h.exemplo'):
            raise ValueError('Resposta externa bloqueada pela aplicação.')
        return await self.operation(case, label, 'responder_evento', evento_id=case['event_ids'][-1], mensagem=message[:2000], aguardar_resposta=wait)

    async def _route(self, case, knowledge, label='encaminhamento'):
        proposal = case['proposal']
        team = next((t for t in knowledge['times'] if t['time'] == proposal['sector']), None)
        if not team or not internal_destination(team['email'], knowledge['times']):
            raise ValueError('Destinatário não autorizado pela base de times.')
        records = ', '.join(r.get('id', r.get('protocol', '')) for r in case.get('world_records', []))
        return await self.operation(case, label, 'enviar_mensagem', para=team['email'], assunto=case['subject'][:150],
                mensagem=f'{proposal["summary"]}\nReferências: {records}. Evento: {case["event_ids"][-1]}. Encaminhamento após triagem e revisão.', evento_id=case['event_ids'][-1])

    async def execute(self, case_id, knowledge=None):
        case = self.store.get_case(case_id)
        if case.get('exclusive'):
            return await self.escalate(case_id, case.get('summary', 'Caso exclusivo de pessoa.'), True)
        p = case['proposal']
        if not p or case.get('review', {}).get('verdict') != 'approved':
            raise ValueError('Execução sem proposta revisada bloqueada.')
        if fingerprint(p) != case['version']:
            raise ValueError('A proposta mudou; revisão e aprovação precisam ser refeitas.')
        k = knowledge or await self.knowledge('triagista')
        errors = policy_errors(p, k, case)
        if errors:
            return await self.escalate(case_id, '; '.join(errors))
        if p['kind'] == 'human' or (p.get('needs_human') and p['kind'] != 'incident' and not needs_approval(p, k)):
            return await self.escalate(case_id, p['reason'], True)
        missing = missing_fields(p, k)
        if missing or p['kind'] == 'need_info':
            missing = missing or ['detalhes necessários para classificar o pedido']
            signature = '|'.join(sorted(missing))
            if case.get('asked_signature') == signature:
                return await self.escalate(case_id, 'O complemento não esclareceu os dados pedidos. Pessoa deve avaliar a próxima pergunta.')
            await self._reply(case, 'Para continuar seu pedido, informe: ' + ', '.join(missing) + '.', True, 'pedir_dados')
            self.store.put_case(dict(id=case_id, status='aguardando_dados', needs_human=False, asked_signature=signature))
            return
        if p['kind'] == 'faq':
            faq = next(x for x in k['faq'] if x['id'] == p['faq_id'])
            await self._reply(case, faq['resposta'])
            self.store.put_case(dict(id=case_id, status='respondido', needs_human=False))
            self.store.add_lesson(f'Dúvida atendida com {faq["id"]}: {faq["pergunta"]}', {'base': 'faq', 'id': faq['id']})
            return
        approval = case.get('approval')
        approved = bool(approval and approval['version'] == case['version'] and approval['decision'] == 'approve')
        required = needs_approval(p, k)
        if required and not approved:
            if p['kind'] == 'request' and not case.get('world_records'):
                result = await self.operation(case, 'registro', 'registrar', base='chamados', evento_id=case['event_ids'][-1],
                    dados={'tipo': p['catalog_type'], 'campos': p['fields'], 'resumo': p['summary'], 'status': 'aguardando aprovação', 'observacao': 'Decisão humana pendente no painel do time.'})
                case['world_records'] = [{'base': 'chamados', 'id': result['registro']['id']}]
                self.store.put_case(dict(id=case_id, world_records=case['world_records']))
            self.store.put_case(dict(id=case_id, status='aguardando_aprovacao', needs_human=True))
            return
        records = list(case.get('world_records', []))
        if p['kind'] == 'vacation':
            if not approved:
                raise ValueError('Férias sem autorização bloqueadas.')
            key = f'{case_id}:{case["version"]}:ferias'
            previous = self.store.get_operation(key)
            data = dict(agente='triagista', email=case['sender'], inicio=p['fields']['data_de_inicio'],
                        fim=p['fields']['data_de_fim'], evento=case['event_ids'][-1], agora=self.now)
            if previous and previous['status'] == 'confirmed':
                result = previous['result']
            else:
                if previous:
                    raise AmbiguousOperation('Lançamento de férias anterior precisa de conferência antes de repetir.')
                self.store.operation(key, case_id, 'tela/ferias', data)
                try:
                    result = await self.browser.submit(data)
                except Exception as exc:
                    self.store.operation(key, case_id, 'tela/ferias', data, 'ambiguous' if getattr(exc, 'ambiguous', False) else 'failed', {'mensagem': str(exc)})
                    raise
                self.store.operation(key, case_id, 'tela/ferias', data, 'confirmed', result)
            records = [{'base': 'ferias', 'id': result['protocol'], 'evidence': result.get('evidence')}]
        elif p['kind'] in ('request', 'incident', 'followup'):
            existing = p.get('existing')
            if existing:
                allowed_base = existing.get('base')
                if allowed_base not in ('chamados', 'incidentes') or not any(r['id'] == existing.get('id') for r in k[allowed_base]):
                    raise ValueError('Registro existente não foi confirmado no mundo.')
                await self.operation(case, 'vinculo', 'atualizar', base=allowed_base, id=existing['id'], evento_id=case['event_ids'][-1], dados={'pedidos': case['event_ids']})
                records = [existing]
            elif records and approved:
                rec = records[0]
                await self.operation(case, 'aprovacao', 'atualizar', base=rec['base'], id=rec['id'], evento_id=case['event_ids'][-1],
                    dados={'status': 'aberto', 'aprovador': approval['actor'], 'observacao': f'Aprovação humana registrada para a versão {case["version"]}. {approval.get("note", "")}'[:2000]})
            elif not records:
                if p['kind'] == 'incident':
                    base, data = 'incidentes', {'criticidade': p['criticality'], 'resumo': p['summary'], 'produto': p['fields'].get('produto', '')}
                else:
                    base, data = 'chamados', {'tipo': p['catalog_type'], 'campos': p['fields'], 'resumo': p['summary']}
                result = await self.operation(case, 'registro', 'registrar', base=base, dados=data, evento_id=case['event_ids'][-1])
                records = [{'base': base, 'id': result['registro']['id']}]
        case['world_records'] = records
        self.store.put_case(dict(id=case_id, world_records=records))
        if p['kind'] != 'vacation':
            await self._route(case, k)
        if p['kind'] == 'incident' and p.get('criticality') in ('P0', 'P1'):
            await self.escalate(case_id, f'Incidente {p["criticality"]} registrado. Pessoa deve assumir imediatamente: {p["summary"]}')
        else:
            self.store.put_case(dict(id=case_id, status='resolvido' if p['kind'] == 'vacation' else 'encaminhado', needs_human=False))
        ids = ', '.join(r['id'] for r in records)
        if ids:
            await self._reply(case, f'Seu pedido foi registrado/associado a {ids}. ' + ('Lançamento de férias confirmado.' if p['kind'] == 'vacation' else f'Encaminhado a {p["sector"]}; o envio não representa resolução técnica.'), label='confirmacao')

    async def decide(self, case_id, version, actor, decision, note=''):
        if len(actor.strip()) < 2:
            raise ValueError('Identifique a pessoa responsável pela decisão.')
        async with self.case_locks.setdefault(case_id, asyncio.Lock()):
            case = self.store.get_case(case_id)
            if not case:
                raise ValueError('Caso não encontrado.')
            if str(version) != str(case.get('version')):
                raise ValueError('A proposta mudou; abra a versão atual antes de decidir.')
            if decision == 'acknowledge' and case['status'] == 'escalado':
                self.store.decision(case_id, version, actor, decision, note)
                self.store.put_case(dict(id=case_id, status='com_humano', needs_human=False, human_owner=actor))
            elif decision in ('approve', 'reject') and case['status'] == 'aguardando_aprovacao' and not case.get('exclusive'):
                record = self.store.decision(case_id, version, actor, decision, note)
                self.store.put_case(dict(id=case_id, approval=record, status='aprovado' if decision == 'approve' else 'recusado', needs_human=False))
                if decision == 'approve':
                    self.enqueue(case_id, 'execute', 0)
                else:
                    # Cancel only requests this case actually created, following
                    # this explicit human decision. Never cancel a linked ticket.
                    created = {op['result'].get('registro', {}).get('id')
                               for op in self.store.list_operations(case_id)
                               if op['tool'] == 'registrar' and op['status'] == 'confirmed' and op.get('result')}
                    try:
                        for rec in case.get('world_records', []):
                            if rec.get('base') != 'chamados' or rec['id'] not in created:
                                continue
                            rows = await self.world.query('chamados', filtro={'id': rec['id']})
                            real = next((r for r in rows if r['id'] == rec['id']), None)
                            if not real or real.get('solicitante', '').lower() != case['sender'].lower() or real.get('status') != 'aguardando aprovação':
                                raise ValueError('Recusa salva; chamado mudou e precisa de conferência humana.')
                            await self.operation(case, 'recusa', 'atualizar', base='chamados', id=rec['id'],
                                evento_id=case['event_ids'][-1], dados={'status': 'cancelado',
                                'observacao': f'Recusa humana de {actor}; versão {version}. {note}'[:2000]})
                    except Exception as exc:
                        self.store.put_case(dict(id=case_id, needs_human=True, error=str(exc)))
            else:
                raise ValueError('Decisão incompatível com o estado atual; nenhuma ação foi repetida.')
        return self.store.get_case(case_id)
