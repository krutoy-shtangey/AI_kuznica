import ssl
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.main import create_app
from backend.quality_mail import MailRetry, MailUncertain, SMTPMailer, SMTPSettings, deliver_one
from shared.contracts import RetrievalError
from test_agent_api import CLASH, TECH_IMPL, FakeModel, Search, draft, source

ROOT = Path(__file__).resolve().parents[1]
ADMIN = {"Authorization": "Bearer " + "fictional-quality-admin-for-tests-" * 2}
SECRET = ADMIN["Authorization"].removeprefix("Bearer ")
QUESTION = "Кто выдаёт жетоны на учебный телескоп?"
EXPERT_TEXT = "Жетоны на учебный телескоп выдаёт смотритель павильона."


class Mailbox:
    def __init__(self, error=None):
        self.messages = []
        self.error = error

    def send(self, appeal):
        self.messages.append(appeal)
        if self.error:
            raise self.error


class QualityTests(unittest.TestCase):
    def setUp(self):
        directory = ROOT / "storage"
        directory.mkdir(exist_ok=True)
        temp = TemporaryDirectory(dir=directory)
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "test.sqlite3"

    def new(self, *replies, search=None, mailer=None, secret=SECRET, start=True):
        model = FakeModel(*replies)
        app = create_app(
            model=model, db_path=self.path, retrieval=search, demo_mode=search is None,
            quality_secret=secret, quality_actor="demo-reviewer", mailer=mailer, workers=False,
        )
        client = TestClient(app)
        if start:
            client.__enter__()
            self.addCleanup(client.__exit__, None, None, None)
        return client, model, app

    def dialogue(self, client):
        response = client.post("/dialogues")
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        return f"/dialogues/{body['dialogue_id']}", {"Authorization": f"Bearer {body['access_token']}"}

    def send(self, client, dialogue, text=QUESTION):
        path, token = dialogue
        response = client.post(path + "/messages", json={"message": text}, headers=token)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def appeal(self, client, dialogue, turn_id, expect=201):
        path, token = dialogue
        response = client.post(path + "/appeals", json={"turn_id": turn_id}, headers=token)
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    def detail(self, client, card):
        response = client.get(f"/quality/appeals/{card['id']}", headers=ADMIN)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def create_card(self, client):
        dialogue = self.dialogue(client)
        result = self.send(client, dialogue)
        self.assertEqual(result["result"]["result_type"], "NO_ANSWER")
        return self.appeal(client, dialogue, result["turn_id"])

    def set_draft(self, client, card, text=EXPERT_TEXT, **scope):
        version = self.detail(client, card)["version"]
        response = client.put(f"/quality/appeals/{card['id']}/draft",
                              json={"version": version, "text": text, **scope}, headers=ADMIN)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def action(self, client, card, name, expect=200):
        version = self.detail(client, card)["version"]
        response = client.post(f"/quality/appeals/{card['id']}/{name}", json={"version": version}, headers=ADMIN)
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    @staticmethod
    def evidence(card, text=EXPERT_TEXT):
        return {"source_id": f"expert_answer:{card['id']}", "quote": text}

    def put_expert_into_model(self, model, card, text=EXPERT_TEXT):
        model.replies = iter([draft("ANSWER", self.evidence(card, text))])

    def test_no_answer_creation_and_disabled_notification(self):
        client, model, _ = self.new()
        card = self.create_card(client)
        self.assertEqual(card["notification"], {"status": "disabled", "attempts": 0})
        detail = self.detail(client, card)
        self.assertEqual((detail["original_question"], detail["reason"]), (QUESTION, "NO_ANSWER"))
        self.assertEqual(model.calls, [])

    def test_conflict_appeal_records_scope_and_sources(self):
        client, _, _ = self.new(draft("CONFLICT", TECH_IMPL, CLASH), search=Search([TECH_IMPL, CLASH]))
        dialogue = self.dialogue(client)
        answer = self.send(client, dialogue, "Кто регистрирует пульт на реализации в Технической дирекции?")
        card = self.appeal(client, dialogue, answer["turn_id"])
        detail = self.detail(client, card)
        self.assertEqual(detail["reason"], "CONFLICT")
        self.assertEqual((detail["department"], detail["stage"]), ("tech", "implementation"))
        self.assertEqual(set(detail["considered_source_ids"]), {TECH_IMPL.source_id, CLASH.source_id})

    def test_idempotent_appeal_and_dialogue_permissions(self):
        client, _, _ = self.new()
        dialog = self.dialogue(client)
        turn = self.send(client, dialog)["turn_id"]
        first = self.appeal(client, dialog, turn)
        self.assertEqual(first, self.appeal(client, dialog, turn, 200))
        other = self.dialogue(client)
        self.appeal(client, other, turn, 409)
        self.assertEqual(client.get(other[0] + f"/appeals/{first['id']}", headers=other[1]).status_code, 404)

    def test_transport_failure_keeps_card_and_retry_state(self):
        mailbox = Mailbox(MailRetry())
        client, _, app = self.new(mailer=mailbox)
        card = self.create_card(client)
        deliver_one(app.state.quality)
        detail = self.detail(client, card)
        self.assertEqual(detail["status"], "new")
        self.assertEqual(detail["notification"], {"status": "retry", "attempts": 1})
        for _ in range(2):
            with app.state.quality.store.write() as db:
                db.execute("UPDATE quality_mail SET due=0")
            deliver_one(app.state.quality)
        self.assertEqual(self.detail(client, card)["notification"], {"status": "failed", "attempts": 3})

    def test_ambiguous_delivery_is_never_resent(self):
        mailbox = Mailbox(MailUncertain())
        client, _, app = self.new(mailer=mailbox)
        card = self.create_card(client)
        deliver_one(app.state.quality)
        deliver_one(app.state.quality)
        self.assertEqual(len(mailbox.messages), 1)
        self.assertEqual(self.detail(client, card)["notification"]["status"], "uncertain")

    def test_draft_is_saved_and_invisible(self):
        client, model, _ = self.new()
        card = self.create_card(client)
        saved = self.set_draft(client, card)
        self.assertEqual((saved["status"], saved["draft"]["author"]), ("draft", "demo-reviewer"))
        self.assertEqual(self.send(client, self.dialogue(client))["result"]["result_type"], "NO_ANSWER")
        self.assertEqual(model.calls, [])

    def test_publish_and_answer_in_new_dialogue(self):
        client, model, _ = self.new()
        card = self.create_card(client)
        self.set_draft(client, card)
        published = self.action(client, card, "approve")
        self.assertTrue(published["publication"]["published"])
        self.assertEqual(published["publication"]["approved_by"], "demo-reviewer")
        self.put_expert_into_model(model, card)
        reply = self.send(client, self.dialogue(client))
        self.assertEqual(reply["result"]["result_type"], "ANSWER")
        self.assertIn("Разъяснение службы качества", reply["result"]["text"])
        self.assertEqual(reply["sources"][0]["appeal_id"], card["id"])
        self.assertEqual(reply["sources"][0]["approved_at"], published["publication"]["approved_at"])
        known = client.get("/quality/explanations", headers=ADMIN).json()["items"]
        self.assertEqual([item["id"] for item in known], [card["id"]])

    def test_edit_and_revoke_with_monotone_revisions(self):
        client, model, app = self.new()
        card = self.create_card(client)
        self.set_draft(client, card)
        self.action(client, card, "approve")
        updated = "Жетоны на учебный телескоп выдаёт кассир павильона."
        self.set_draft(client, card, updated)
        self.put_expert_into_model(model, card)
        first = self.dialogue(client)
        self.assertIn(EXPERT_TEXT, self.send(client, first)["result"]["text"])
        edition = self.action(client, card, "approve")
        self.assertEqual(edition["publication"]["revision"], 2)
        self.put_expert_into_model(model, card, updated)
        self.assertIn(updated, self.send(client, self.dialogue(client))["result"]["text"])
        revocation = self.action(client, card, "revoke")
        self.assertFalse(revocation["publication"]["published"])
        self.assertEqual(revocation["publication"]["revision"], 3)
        app.state.quality.retrieval._update(f"expert_answer:{card['id']}", 1, "{}")
        self.assertEqual(self.send(client, self.dialogue(client))["result"]["result_type"], "NO_ANSWER")
        self.assertIsNone(client.get(first[0] + "/messages", headers=first[1]).json()["messages"][0]["result"])

    def test_regulation_expert_conflict_is_returned_as_conflict(self):
        client, model, _ = self.new()
        # The initial wording is absent from the synthetic corpus.
        card = self.create_card(client)
        self.set_draft(client, card, "Акт испытаний опытного макета подписывает куратор.", department="tech",
                       stage="implementation")
        self.action(client, card, "approve")
        official = {"source_id": "regulation:demo-tech:4.1",
                    "quote": "Акт испытаний опытного макета подписывает руководитель стенда."}
        model.replies = iter([draft("CONFLICT", official,
                                    self.evidence(card, "Акт испытаний опытного макета подписывает куратор."))])
        reply = self.send(client, self.dialogue(client),
                          "Кто подписывает акт испытаний опытного макета в Технической дирекции на реализации?")
        self.assertEqual(reply["result"]["result_type"], "CONFLICT")
        self.assertEqual({item["source_type"] for item in reply["sources"]}, {"regulation", "expert_answer"})

    def test_expert_answer_cannot_bypass_matching_official_source(self):
        client, model, _ = self.new()
        card = self.create_card(client)
        text = "Акт испытаний опытного макета подписывает куратор."
        self.set_draft(client, card, text, department="tech", stage="implementation")
        self.action(client, card, "approve")
        self.put_expert_into_model(model, card, text)
        path, token = self.dialogue(client)
        response = client.post(path + "/messages", headers=token, json={
            "message": "Кто подписывает акт испытаний опытного макета в Технической дирекции на реализации?"
        })
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["code"], "source_review_required")

    def test_missing_draft_scope_inherits_appeal_scope(self):
        client, _, _ = self.new()
        dialog = self.dialogue(client)
        result = self.send(client, dialog, QUESTION + " В Дирекции ИБ на пресейле.")
        card = self.appeal(client, dialog, result["turn_id"])
        saved = self.set_draft(client, card)
        self.assertEqual((saved["draft"]["department"], saved["draft"]["stage"]), ("ib", "presale"))

    def test_admin_denied_without_secret_and_login_rate_limit(self):
        client, _, _ = self.new(secret="")
        self.assertEqual(client.get("/quality/appeals", headers=ADMIN).status_code, 503)
        configured, _, _ = self.new()
        card = self.create_card(configured)
        url = f"/quality/appeals/{card['id']}/approve"
        for _ in range(5):
            self.assertEqual(configured.post(url, json={"version": 1}).status_code, 401)
        self.assertEqual(configured.post(url, json={"version": 1}).status_code, 429)

    def test_restart_preserves_published_knowledge_and_appeal(self):
        client, _, _ = self.new(start=False)
        with client:
            card = self.create_card(client)
            self.set_draft(client, card)
            self.action(client, card, "approve")
        second, model, _ = self.new()
        self.assertEqual(self.detail(second, card)["publication"]["state"], "indexed")
        self.put_expert_into_model(model, card)
        self.assertEqual(self.send(second, self.dialogue(second))["result"]["result_type"], "ANSWER")

    def test_index_failure_is_not_published_then_retry_recovers(self):
        client, model, app = self.new()
        card = self.create_card(client)
        self.set_draft(client, card)
        quality = app.state.quality
        with patch.object(quality.retrieval, "upsert_expert_answer", side_effect=RetrievalError):
            result = self.action(client, card, "approve", 202)
            self.assertFalse(result["publication"]["published"])
            self.assertEqual(self.send(client, self.dialogue(client))["result"]["result_type"], "NO_ANSWER")
            for _ in range(2):
                with quality.store.write() as db:
                    db.execute("UPDATE quality_revisions SET due=0")
                quality.process_index()
        self.assertEqual(self.detail(client, card)["publication"]["state"], "failed")
        self.action(client, card, "retry-index")
        self.put_expert_into_model(model, card)
        self.assertEqual(self.send(client, self.dialogue(client))["result"]["result_type"], "ANSWER")

    def test_long_source_retains_relevant_tail_original_id_and_offsets(self):
        quote = "Учебный гироскоп выдаёт архивариус."
        text = ("Этот раздел описывает общие положения стенда.\n" * 80) + quote
        hit = source("44", text)
        client, model, _ = self.new(
            draft("ANSWER", {"source_id": hit.source_id, "quote": quote}), search=Search([hit])
        )
        response = self.send(client, self.dialogue(client), "Кто выдаёт гироскоп?")
        evidence = response["sources"][0]
        self.assertGreater(evidence["excerpt_start"], 1400)
        self.assertEqual(text[evidence["excerpt_start"]:evidence["excerpt_end"]], evidence["excerpt"])
        self.assertIn(quote, response["result"]["text"])
        self.assertEqual(model.calls[0][1]["sources"][0]["source_id"], hit.source_id)

    def test_conditions_clarify_then_include_and_reject_mismatch(self):
        client, model, _ = self.new()
        card = self.create_card(client)
        self.set_draft(client, card, conditions="вид=учебный")
        self.action(client, card, "approve")
        dialogue = self.dialogue(client)
        self.assertEqual(self.send(client, dialogue)["result"]["result_type"], "CLARIFY")
        self.assertEqual(model.calls, [])
        self.put_expert_into_model(model, card)
        self.assertEqual(self.send(client, dialogue, "Учебный")["result"]["result_type"], "ANSWER")
        self.assertEqual(model.calls[-1][1]["confirmed_facts"], {"вид": "учебный"})
        other = self.dialogue(client)
        self.send(client, other)
        self.assertEqual(self.send(client, other, "Не учебный")["result"]["result_type"], "NO_ANSWER")

    def test_appeal_and_mail_limits_survive_duplicate_requests(self):
        mailbox = Mailbox()
        client, _, app = self.new(mailer=mailbox)
        dialogue = self.dialogue(client)
        for _ in range(3):
            turn = self.send(client, dialogue)["turn_id"]
            self.appeal(client, dialogue, turn)
            self.appeal(client, dialogue, turn, 200)
        self.appeal(client, dialogue, self.send(client, dialogue)["turn_id"], 429)
        for _ in range(6):
            deliver_one(app.state.quality)
        self.assertEqual(len(mailbox.messages), 3)
        self.assertEqual(len({item["id"] for item in mailbox.messages}), 3)

    def test_technical_error_cannot_be_registered(self):
        client, _, _ = self.new(search=Search(RetrievalError("synthetic failure")))
        dialogue = self.dialogue(client)
        path, bearer = dialogue
        self.assertEqual(client.post(path + "/messages", json={"message": QUESTION}, headers=bearer).status_code, 503)
        turn = client.get(path + "/messages", headers=bearer).json()["messages"][0]["id"]
        self.appeal(client, dialogue, turn, 409)

    @staticmethod
    def smtp_fixture(implicit):
        return (
            SMTPSettings("stub.invalid", 465 if implicit else 587, "quality", "fake",
                         "sender@example.invalid", "quality@example.invalid", not implicit, implicit,
                         "https://demo.example.invalid"),
            {"id": "demo", "question": QUESTION, "reason": "NO_ANSWER", "clarifications": (),
             "facts": {}, "department": None, "stage": None},
        )

    def test_smtp_modes_load_trusted_cas_and_use_a_fixed_envelope(self):
        for implicit in (True, False):
            with self.subTest(implicit=implicit):
                config, appeal = self.smtp_fixture(implicit)
                # An empty platform trust store reproduces the macOS Python setup.
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                self.assertEqual(context.cert_store_stats()["x509_ca"], 0)
                with (patch("backend.quality_mail.ssl.create_default_context", return_value=context),
                      patch("backend.quality_mail.smtplib.SMTP_SSL") as ssl_connect,
                      patch("backend.quality_mail.smtplib.SMTP") as connect):
                    SMTPMailer(config).send(appeal)

                self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
                self.assertTrue(context.check_hostname)
                self.assertGreater(context.cert_store_stats()["x509_ca"], 0)
                if implicit:
                    connect.assert_not_called()
                    ssl_connect.assert_called_once_with("stub.invalid", 465, timeout=10, context=context)
                    smtp = ssl_connect.return_value
                else:
                    ssl_connect.assert_not_called()
                    connect.assert_called_once_with("stub.invalid", 587, timeout=10)
                    smtp = connect.return_value
                    smtp.starttls.assert_called_once_with(context=context)

                calls = [call[0] for call in smtp.method_calls]
                if not implicit:
                    self.assertLess(calls.index("starttls"), calls.index("login"))
                self.assertLess(calls.index("login"), calls.index("send_message"))
                message = smtp.send_message.call_args.args[0]
                self.assertEqual(smtp.send_message.call_args.kwargs["to_addrs"], ["quality@example.invalid"])
                self.assertIn("https://demo.example.invalid/docs", message.get_content())
                self.assertEqual(message["Message-ID"], "<quality-demo@example.invalid>")

    def test_smtp_certificate_failure_never_authenticates_or_sends(self):
        for implicit in (True, False):
            with self.subTest(implicit=implicit):
                config, appeal = self.smtp_fixture(implicit)
                with (patch("backend.quality_mail.smtplib.SMTP_SSL") as ssl_connect,
                      patch("backend.quality_mail.smtplib.SMTP") as connect):
                    error = ssl.SSLCertVerificationError("synthetic certificate failure")
                    if implicit:
                        ssl_connect.side_effect = error
                    else:
                        connect.return_value.starttls.side_effect = error
                    with self.assertRaises(MailRetry):
                        SMTPMailer(config).send(appeal)
                    for constructor in (ssl_connect, connect):
                        constructor.return_value.login.assert_not_called()
                        constructor.return_value.send_message.assert_not_called()
