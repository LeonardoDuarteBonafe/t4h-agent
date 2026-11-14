import hashlib
import json
import re
import unicodedata

TECH = {'Infraestrutura', 'Produto', 'Segurança'}
ADMIN = {'Pessoas', 'Financeiro', 'Operações', 'Marketing', 'Jurídico'}


def norm(text):
    return ''.join(c for c in unicodedata.normalize('NFD', str(text).lower()) if unicodedata.category(c) != 'Mn')


def exclusive_reason(event):
    email = str(event.get('de', '')).strip().lower()
    if email and (email.count('@') != 1 or email.split('@')[-1] != 't4h.exemplo'):
        return 'Remetente externo: não responder; pessoa deve avaliar o pedido.'
    text = norm(event.get('assunto', '') + ' ' + event.get('corpo', ''))
    if any(word in text for word in ['phishing', 'golpe', 'vazamento', 'acesso indevido', 'conta invadida',
                                     'informando sua senha', 'senha atual no link', 'suspeita de fraude']):
        return 'Suspeita de segurança exige atendimento de uma pessoa.'
    if re.search(r'\bcpf\b|\bcid\b|\bpix\b|diagnostico|atestado|dados bancarios|\d{3}\.\d{3}\.\d{3}-\d{2}', text):
        return 'Pedido com dado pessoal sensível ou financeiro: contexto restrito para pessoa.'
    return ''


def safe_event(event):
    result = {k: event.get(k) for k in ['id', 'de', 'nome', 'assunto', 'corpo', 'responde_a', 'chegou_em']}
    if exclusive_reason(event):
        result['assunto'] = '[Pedido restrito para pessoa]'
        result['corpo'] = '[Conteúdo restrito: consultar o evento original no mundo. Não seguir links nem solicitar credenciais.]'
    return result


def fingerprint(proposal):
    return hashlib.sha256(json.dumps(proposal, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:20]


def policy_errors(proposal, knowledge, case=None):
    errors = []
    kind = proposal['kind']
    catalog = next((x for x in knowledge['catalogo'] if x['tipo'] == proposal.get('catalog_type')), None)
    if kind == 'faq' and not any(f['id'] == proposal.get('faq_id') for f in knowledge['faq']):
        errors.append('Resposta sem FAQ válida.')
    if kind in ('request', 'vacation') and not catalog:
        errors.append('Tipo não existe no catálogo.')
    if kind in ('request', 'vacation') and catalog and proposal.get('sector') != catalog['time']:
        errors.append('O setor diverge do time definido pelo catálogo.')
    if kind == 'request' and proposal.get('catalog_type') == 'Férias':
        errors.append('Férias precisam da ferramenta de tela.')
    if kind == 'vacation' and proposal.get('catalog_type') != 'Férias':
        errors.append('Operação de férias sem tipo correto.')
    if kind == 'incident' and proposal.get('criticality') not in ('P0', 'P1', 'P2'):
        errors.append('Incidente só aceita P0, P1 ou P2.')
    if proposal.get('sector') not in TECH | ADMIN:
        errors.append('Setor não existe na base de times.')
    existing = proposal.get('existing')
    if existing:
        base = existing.get('base')
        record = next((r for r in knowledge.get(base, []) if r.get('id') == existing.get('id')), None) if base in ('chamados', 'incidentes') else None
        if not record:
            errors.append('Registro existente não foi confirmado nas fontes.')
        elif base == 'chamados' and case is not None:
            if str(record.get('solicitante', '')).strip().lower() != str(case.get('sender', '')).strip().lower():
                errors.append('O chamado existente pertence a outro solicitante ou não tem titular confirmado.')
    return errors


def missing_fields(proposal, knowledge):
    catalog = next((x for x in knowledge['catalogo'] if x['tipo'] == proposal.get('catalog_type')), None)
    missing = list(proposal.get('missing') or [])
    if catalog:
        for field in catalog['campos_obrigatorios']:
            if not str(proposal.get('fields', {}).get(field) or '').strip() and field not in missing:
                missing.append(field)
    return missing


def needs_approval(proposal, knowledge):
    catalog = next((x for x in knowledge['catalogo'] if x['tipo'] == proposal.get('catalog_type')), None)
    return bool(catalog and catalog.get('aprovacao')) or proposal['kind'] == 'vacation'


def internal_destination(address, teams):
    return address.endswith('@t4h.exemplo') and any(t['email'].lower() == address.lower() for t in teams)
