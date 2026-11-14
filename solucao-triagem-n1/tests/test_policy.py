"""Business boundaries tested independently of the language model."""

import unittest

from triagem_n1.policy import (
    exclusive_reason, fingerprint, internal_destination, missing_fields,
    needs_approval, policy_errors, safe_event,
)


KNOWLEDGE = {
    "faq": [{"id": "FAQ-01", "resposta": "Orientação existente"}],
    "catalogo": [
        {"tipo": "Acesso a sistema", "time": "Segurança", "campos_obrigatorios": ["sistema", "nivel_de_acesso", "justificativa"], "aprovacao": "Gestor"},
        {"tipo": "Troca de equipamento", "time": "Infraestrutura", "campos_obrigatorios": ["item", "patrimonio", "defeito"], "aprovacao": None},
        {"tipo": "Férias", "time": "Pessoas", "campos_obrigatorios": ["data_de_inicio", "data_de_fim"], "aprovacao": "Gestor"},
    ],
}


def proposal(**overrides):
    result = {"kind": "request", "sector": "Segurança", "catalog_type": "Acesso a sistema", "fields": {}, "missing": []}
    result.update(overrides)
    return result


class PolicyTests(unittest.TestCase):
    def test_routine_access_is_not_automatically_security_incident(self):
        event = {"de": "pessoa@t4h.exemplo", "assunto": "Acesso CRM", "corpo": "Preciso de perfil de consulta para acompanhar clientes."}
        self.assertEqual(exclusive_reason(event), "")
        self.assertTrue(needs_approval(proposal(), KNOWLEDGE))

    def test_lookalike_external_domain_requires_person(self):
        self.assertTrue(exclusive_reason({"de": "suporte@t4h-exemplo.com", "assunto": "Senha", "corpo": "Valide o acesso"}))
        self.assertTrue(exclusive_reason({"de": "suporte@t4h.exemplo.evil.test", "assunto": "Suporte", "corpo": "Mensagem"}))

    def test_internal_security_suspicion_requires_person(self):
        self.assertTrue(exclusive_reason({"de": "pessoa@t4h.exemplo", "assunto": "Conta invadida", "corpo": "Identifiquei acesso indevido."}))

    def test_sensitive_body_is_not_sent_as_general_agent_context(self):
        event = {"id": "MAN-001", "de": "pessoa@t4h.exemplo", "assunto": "Documento", "corpo": "CPF 123.456.789-00 e diagnóstico privado"}
        sanitized = safe_event(event)
        self.assertNotIn("123.456.789-00", sanitized["corpo"])
        self.assertNotIn("diagnóstico privado", sanitized["corpo"])
        self.assertEqual(sanitized["id"], event["id"])

    def test_sensitive_subject_is_not_sent_as_general_agent_context(self):
        event = {"id": "MAN-002", "de": "pessoa@t4h.exemplo", "assunto": "Meu CPF 123.456.789-00", "corpo": "Preciso de ajuda."}
        sanitized = safe_event(event)
        self.assertNotIn("123.456.789-00", sanitized["assunto"])

    def test_unknown_faq_cannot_authorize_a_response(self):
        errors = policy_errors(proposal(kind="faq", faq_id="FAQ-INVENTADA", catalog_type=None), KNOWLEDGE)
        self.assertTrue(errors)
        self.assertEqual(policy_errors(proposal(kind="faq", faq_id="FAQ-01", catalog_type=None), KNOWLEDGE), [])

    def test_vacation_must_use_the_browser_operation(self):
        self.assertTrue(policy_errors(proposal(sector="Pessoas", catalog_type="Férias"), KNOWLEDGE))
        self.assertEqual(policy_errors(proposal(kind="vacation", sector="Pessoas", catalog_type="Férias"), KNOWLEDGE), [])

    def test_incident_rejects_p3_or_unknown_criticality(self):
        for level in (None, "P3", "P4", "URGENTE"):
            with self.subTest(level=level):
                self.assertTrue(policy_errors(proposal(kind="incident", sector="Produto", catalog_type=None, criticality=level), KNOWLEDGE))
        for level in ("P0", "P1", "P2"):
            with self.subTest(level=level):
                self.assertEqual(policy_errors(proposal(kind="incident", sector="Produto", catalog_type=None, criticality=level), KNOWLEDGE), [])

    def test_catalog_sector_mismatch_blocks_wrong_routing(self):
        errors = policy_errors(proposal(sector="Financeiro"), KNOWLEDGE)
        self.assertTrue(errors, "Pedido de acesso pertence a Segurança, não Financeiro.")

    def test_missing_fields_are_complete_and_not_duplicated(self):
        item = proposal(fields={"sistema": "CRM"}, missing=["justificativa"])
        result = missing_fields(item, KNOWLEDGE)
        self.assertEqual(set(result), {"nivel_de_acesso", "justificativa"})
        self.assertEqual(result.count("justificativa"), 1)

    def test_unknown_or_incomplete_catalog_cannot_authorize_request(self):
        self.assertTrue(policy_errors(proposal(catalog_type="Tipo inventado"), KNOWLEDGE))
        self.assertEqual(policy_errors(proposal(fields={"sistema": "CRM", "nivel_de_acesso": "leitura", "justificativa": "Carteira"}), KNOWLEDGE), [])

    def test_approval_depends_on_catalog_and_vacation(self):
        self.assertTrue(needs_approval(proposal(), KNOWLEDGE))
        self.assertFalse(needs_approval(proposal(sector="Infraestrutura", catalog_type="Troca de equipamento"), KNOWLEDGE))
        self.assertTrue(needs_approval(proposal(kind="vacation", sector="Pessoas", catalog_type="Férias"), KNOWLEDGE))

    def test_destination_must_be_known_internal_team(self):
        teams = [{"email": "produto@t4h.exemplo"}]
        self.assertTrue(internal_destination("produto@t4h.exemplo", teams))
        for address in ("produto@outside.example", "desconhecido@t4h.exemplo", "produto@t4h.exemplo.evil.test"):
            with self.subTest(address=address):
                self.assertFalse(internal_destination(address, teams))

    def test_fingerprint_ignores_object_key_order_but_changes_with_plan(self):
        self.assertEqual(fingerprint({"setor": "Segurança", "acesso": "leitura"}), fingerprint({"acesso": "leitura", "setor": "Segurança"}))
        self.assertNotEqual(fingerprint({"acesso": "leitura"}), fingerprint({"acesso": "escrita"}))

    def test_linked_ticket_must_belong_to_sender(self):
        k = {**KNOWLEDGE, 'chamados': [{'id': 'CH-1', 'solicitante': 'dono@t4h.exemplo'}]}
        p = proposal(kind='followup', existing={'base': 'chamados', 'id': 'CH-1'})
        self.assertTrue(policy_errors(p, k, {'sender': 'outro@t4h.exemplo'}))
        self.assertEqual(policy_errors(p, k, {'sender': 'dono@t4h.exemplo'}), [])

    def test_incident_may_link_reports_from_different_people(self):
        k = {**KNOWLEDGE, 'incidentes': [{'id': 'INC-1'}]}
        p = proposal(kind='incident', criticality='P0', existing={'base': 'incidentes', 'id': 'INC-1'})
        self.assertEqual(policy_errors(p, k, {'sender': 'outro@t4h.exemplo'}), [])


if __name__ == "__main__":
    unittest.main()
