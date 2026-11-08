"""Static API interface verification: legal designs pass, broken ones are reported."""

from copy import deepcopy
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from infrastructure.stages.backend.context import load_context
from infrastructure.stages.backend.contracts import (ApiContractError, contract_problems, creates_by_entity,
    format_contract_summary, format_traceability_summary, requirement_traceability, validate_contracts)
from infrastructure.stages.backend.primitives import ObligationCoverageError, apply_decomposition
from infrastructure.utils.files import save_json
from test_backend_contracts import prepare_artifacts


def object_schema(properties=None, **extra):
    """Build the inline object schema the API IR uses for one request or response part."""
    return {"type": "object", "properties": properties or {}, **extra}


def api(name="listBooks", method="GET", path="/api/books", params=None, read=None, write=None):
    """Build one complete API IR member the way step 06 asks the model to return it."""
    return {
        "description": "Perform the business operation",
        "read": list(read or []),
        "write": list(write or []),
        "signature": {"name": name, "method": method, "path": path},
        "request": {"params": object_schema({key: {"type": "string"} for key in (params or [])}),
            "query": object_schema(), "body": object_schema(), "headers": object_schema()},
        "response": {
            "success": {"status": 200, "body": object_schema({"ok": {"type": "boolean"}}), "headers": object_schema()},
            "errors": [{"status": 400, "body": object_schema({"error": {"type": "string"}}), "headers": object_schema()}],
        },
        "precondition": [],
        "postcondition": [{"id": "Q-1", "description": "Return the requested values", "status": 200}],
    }


def design(apis, requirements=None):
    """Associate every API with the single requirement that uses it."""
    return {"apis": apis, "requirements": requirements if requirements is not None else {"REQ-1": list(apis)}}


def fields(problems):
    """Read the reported (api_id, field) pairs so assertions stay readable."""
    return [(item["api_id"], item["field"]) for item in problems]


def book_design():
    """Cover the obligation fixture's writer and reader sets with two valid contracts."""
    writer = api(name="saveNote", method="POST", path="/api/notes", write=["Note.id", "Note.text"])
    reader = api(name="countNotes", path="/api/notes/count", read=["Note.id", "Note.text"])
    return {"apis": {"API-SAVE": writer, "API-COUNT": reader},
        "requirements": {"ROOT": [], "REQ-A": ["API-SAVE"], "REQ-B": ["API-SAVE"], "REQ-C": ["API-COUNT"]}}


def proposal_for(value, rid):
    """Return the complete proposal for one requirement, as the design model would."""
    ids = value["requirements"][rid]
    return {"apis": {aid: deepcopy(value["apis"][aid]) for aid in ids}, "requirement_apis": list(ids)}


def create_api(name, path, write, required, optional=()):
    """Build one create operation the way a registration or sign-in contract declares it."""
    member = api(name=name, method="POST", path=path, write=list(write))
    member["request"]["body"] = object_schema(
        {key: {"type": "string"} for key in [*required, *optional]}, required=list(required))
    member["response"]["success"]["status"] = 201
    return member


#: The requirements a registration / sign-in / sign-out design serves.
ACCOUNT_TREE = ("REQ-1", "REQ-2", "REQ-3", "REQ-4", "REQ-5")


def account_design(extra=None, requirements=None):
    """Design registration, sign-out and sign-in as the three distinct business goals.

    Registration both writes the account and opens a session; it writes more account columns,
    so it creates the account. Sign-in writes only a session, so it creates that second entity.
    """
    apis = {
        "API-register": create_api("registerAccount", "/api/register",
            ["account.id", "account.username", "account.name", "account.passport_number",
             "account.password_hash", "auth_session.token"],
            ["username", "name", "passport_number", "password"]),
        "API-login": create_api("loginAccount", "/api/login",
            ["auth_session.id", "auth_session.username"], ["username", "password"]),
        "API-logout": api(name="logoutAccount", method="POST", path="/api/logout", write=[]),
        "API-session": api(name="currentSession", path="/api/session", read=["account.username"]),
    }
    apis.update(extra or {})
    mapped = requirements if requirements is not None else {"REQ-1": ["API-register"],
        "REQ-2": ["API-session"], "REQ-3": ["API-login"], "REQ-4": ["API-logout"]}
    return {"apis": apis, "requirements": mapped}


def with_duplicate(member, api_id="API-register-account", rid="REQ-5"):
    """Add a second registration to the account design, mapped to its own requirement."""
    value = account_design({api_id: member})
    value["requirements"][rid] = [api_id]
    return value



class ValidContractTests(unittest.TestCase):
    def test_bookstack_route_parameters_are_accepted(self):
        nested = api(name="readBookPage", path="/api/libraries/{libraryId}/books/:bookTitle/pages/{pageNumber}",
            params=("libraryId", "bookTitle", "pageNumber"))
        self.assertEqual(contract_problems(design({"API-PAGE": nested, "API-LIST": api()}), tree={"REQ-1"}), [])

    def test_static_api_without_parameters_is_accepted(self):
        self.assertEqual(contract_problems(design({"API-LIST": api()}), tree={"REQ-1"}), [])

    def test_design_without_any_operation_is_accepted(self):
        value = design({}, {"ROOT": [], "REQ-1": []})
        self.assertEqual(contract_problems(value, tree={"ROOT", "REQ-1"}), [])
        self.assertIn("VERIFIED 0 API CONTRACT(S)", format_contract_summary(value))

    def test_apis_that_is_not_an_object_is_rejected(self):
        problems = contract_problems({"apis": [], "requirements": {}}, tree={"REQ-1"})
        self.assertEqual(fields(problems), [("", "apis")])

    def test_parameter_spellings_may_be_mixed_and_reordered(self):
        member = api(name="readPage", path="/api/books/:bookTitle/pages/{pageNumber}",
            params=("pageNumber", "bookTitle"))
        self.assertEqual(contract_problems(design({"API-PAGE": member}), tree={"REQ-1"}), [])

    def test_legal_optional_schema_keywords_are_not_errors(self):
        member = api(name="saveNote", method="POST", path="/api/notes")
        member["request"]["query"] = {"type": "object", "title": "Filters", "description": "Search filters",
            "additionalProperties": False, "required": ["search"],
            "properties": {"search": {"type": "string", "format": "regex", "default": ".*", "examples": ["note"]}}}
        member["request"]["body"] = {"type": "object", "$schema": "https://json-schema.org/draft/2020-12/schema",
            "required": ["text"],
            "properties": {"text": {"type": "string", "minLength": 1, "enum": ["a", "b"], "nullable": False,
                "pattern": "^.+$", "description": "Note text"}}}
        member["precondition"] = [{"id": "P-1", "description": "Text is nonempty", "error_status": 400, "extra": True}]
        member["postcondition"] = [{"id": "Q-1", "description": "Save one note", "status": 201, "extra": True}]
        member["response"]["errors"][0]["body"]["properties"] = {"error": {"type": "string", "examples": ["empty"]}}
        self.assertEqual(contract_problems(design({"API-SAVE": member}), tree={"REQ-1"}), [])

    def test_contract_summary_names_every_verified_route(self):
        text = format_contract_summary(design({"API-BOOK": api(name="readBook", path="/api/books/{bookTitle}",
            params=("bookTitle",))}))
        self.assertIn("API-BOOK", text)
        self.assertIn("/api/books/{bookTitle}", text)
        self.assertIn("params=bookTitle", text)


class InvalidContractTests(unittest.TestCase):
    def test_path_parameter_missing_from_request_params(self):
        problems = contract_problems(design({"API-BOOK": api(name="readBook", path="/api/books/{bookTitle}")}),
            tree={"REQ-1"})
        self.assertEqual(fields(problems), [("API-BOOK", "request.params")])
        self.assertIn("bookTitle", problems[0]["reason"])
        self.assertIn("request.params.properties", problems[0]["fix"])

    def test_declared_parameter_absent_from_path(self):
        problems = contract_problems(design({"API-LIST": api(params=("bookTitle",))}), tree={"REQ-1"})
        self.assertEqual(fields(problems), [("API-LIST", "request.params")])
        self.assertIn("never supplies", problems[0]["reason"])

    def test_invalid_http_method_is_rejected(self):
        problems = contract_problems(design({"API-LIST": api(method="FETCH")}), tree={"REQ-1"})
        self.assertEqual(fields(problems), [("API-LIST", "signature.method")])
        self.assertIn("FETCH", problems[0]["reason"])
        self.assertIn("GET", problems[0]["fix"])

    def test_duplicate_method_and_path_is_rejected(self):
        value = design({"API-SAVE": api(name="saveNote", method="POST", path="/api/notes"),
            "API-NOTES": api(name="createNote", method="POST", path="/api/notes")})
        problems = contract_problems(value, tree={"REQ-1"})
        self.assertEqual(fields(problems), [("API-NOTES", "signature.path")])
        self.assertIn("duplicates", problems[0]["reason"])
        self.assertIn("API-SAVE", problems[0]["reason"])

    def test_ambiguous_parameter_positions_are_rejected(self):
        value = design({"API-BOOK": api(name="readBook", path="/api/books/{bookId}", params=("bookId",)),
            "API-TITLE": api(name="readTitle", path="/api/books/:bookTitle", params=("bookTitle",))})
        problems = contract_problems(value, tree={"REQ-1"})
        self.assertEqual(fields(problems), [("API-TITLE", "signature.path")])
        self.assertIn("ambiguous", problems[0]["reason"])

    def test_relative_path_is_rejected(self):
        problems = contract_problems(design({"API-LIST": api(path="api/books")}), tree={"REQ-1"})
        self.assertEqual(fields(problems), [("API-LIST", "signature.path")])
        self.assertIn("absolute", problems[0]["reason"])

    def test_malformed_paths_are_rejected(self):
        for path in ("/api/books/{bookTitle", "/api/books/:", "/api/books?title=x", "/api//books"):
            with self.subTest(path=path):
                problems = contract_problems(design({"API-LIST": api(path=path)}), tree={"REQ-1"})
                self.assertEqual(fields(problems), [("API-LIST", "signature.path")])

    def test_duplicate_function_name_is_rejected(self):
        value = design({"API-LIST": api(name="read", path="/api/books"),
            "API-COUNT": api(name="read", path="/api/notes")})
        problems = contract_problems(value, tree={"REQ-1"})
        self.assertEqual(fields(problems), [("API-COUNT", "signature.name")])
        self.assertIn("API-LIST", problems[0]["reason"])

    def test_api_ids_differing_only_in_case_conflict(self):
        value = design({"API-Note": api(name="readNote", path="/api/notes"),
            "api-note": api(name="readNotes", path="/api/notes/all")})
        problems = contract_problems(value, tree={"REQ-1"})
        self.assertEqual(fields(problems), [("api-note", "api_id")])

    def test_unknown_requirement_reference_is_rejected(self):
        value = design({"API-LIST": api()}, {"REQ-1": ["API-LIST"], "REQ-404": []})
        problems = contract_problems(value, tree={"REQ-1"})
        self.assertEqual(fields(problems), [("REQ-404", "requirements.REQ-404")])
        self.assertIn("does not exist", problems[0]["reason"])

    def test_unknown_api_reference_in_requirements_is_rejected(self):
        value = design({"API-LIST": api()}, {"REQ-1": ["API-LIST", "API-GHOST"]})
        problems = contract_problems(value, tree={"REQ-1"})
        self.assertEqual(fields(problems), [("REQ-1", "requirements.REQ-1")])
        self.assertIn("API-GHOST", problems[0]["reason"])

    def test_api_that_no_requirement_references_is_rejected(self):
        value = design({"API-LIST": api(), "API-EXTRA": api(name="readExtra", path="/api/extra")},
            {"REQ-1": ["API-LIST"]})
        problems = contract_problems(value, tree={"REQ-1"})
        self.assertEqual(fields(problems), [("API-EXTRA", "requirements")])

    def test_missing_required_properties_are_reported_one_by_one(self):
        problems = contract_problems(design({"API-LIST": {"signature": {"name": "read", "method": "GET",
            "path": "/api/books"}, "read": [], "write": []}}), tree={"REQ-1"})
        self.assertEqual([field for _, field in fields(problems)],
            ["description", "request", "response", "precondition", "postcondition"])

    def test_non_object_request_schema_is_rejected(self):
        member = api()
        member["request"]["body"] = {"type": "array", "items": {"type": "string"}}
        problems = contract_problems(design({"API-SAVE": member}), tree={"REQ-1"})
        self.assertEqual(fields(problems), [("API-SAVE", "request.body")])

    def test_required_names_must_be_declared_properties(self):
        member = api()
        member["request"]["body"] = object_schema({"text": {"type": "string"}}, required=["text", "title"])
        problems = contract_problems(design({"API-SAVE": member}), tree={"REQ-1"})
        self.assertEqual(fields(problems), [("API-SAVE", "request.body.required")])
        self.assertIn("title", problems[0]["reason"])

    def test_status_codes_outside_their_branch_are_rejected(self):
        member = api()
        member["response"]["success"]["status"] = 500
        member["response"]["errors"][0]["status"] = 200
        problems = contract_problems(design({"API-SAVE": member}), tree={"REQ-1"})
        self.assertEqual(fields(problems),
            [("API-SAVE", "response.success.status"), ("API-SAVE", "response.errors[0].status")])

    def test_error_reports_api_field_reason_and_repair_for_every_problem(self):
        with self.assertRaises(ApiContractError) as caught:
            validate_contracts(design({"API-LIST": api(method="FETCH")}), tree={"REQ-1"})
        error = caught.exception
        self.assertEqual(error.feedback["problem_count"], 1)
        self.assertEqual(error.feedback["problems"][0]["api_id"], "API-LIST")
        self.assertEqual(error.feedback["problems"][0]["field"], "signature.method")
        self.assertIn("API-LIST", str(error))
        self.assertIn("signature.method", str(error))
        self.assertIn("Fix:", str(error))

    def test_a_required_body_value_the_flow_cannot_build_is_rejected(self):
        member = api(name="saveNote", method="POST", path="/api/notes")
        member["request"]["body"] = object_schema({"payload": object_schema()}, required=["payload"])
        problems = contract_problems(design({"API-SAVE": member}), tree={"REQ-1"})
        self.assertEqual(fields(problems), [("API-SAVE", "request.body.properties.payload")])
        self.assertIn("no value the HTTP flow can send", problems[0]["reason"])
        self.assertIn("make the property optional", problems[0]["fix"])

    def test_a_required_value_with_a_declared_literal_is_sendable(self):
        member = api(name="saveNote", method="POST", path="/api/notes")
        member["request"]["body"] = object_schema({"nationality": {"enum": ["China", "France"]}},
            required=["nationality"])
        self.assertEqual(contract_problems(design({"API-SAVE": member}), tree={"REQ-1"}), [])

    def test_validation_reports_without_rewriting_the_design(self):
        value = design({"API-BOOK": api(name="readBook", path="/api/books/{bookTitle}")})
        before = deepcopy(value)
        with self.assertRaises(ApiContractError):
            validate_contracts(value, tree={"REQ-1"})
        self.assertEqual(value, before)


class SemanticReuseTests(unittest.TestCase):
    """One entity, one create: a narrower duplicate must not bypass the full registration.

    Registration and sign-in both open a session, yet only registration creates the account,
    so the rule has to keep them apart while rejecting a second, narrower account create.
    """

    def test_full_registration_sign_in_and_sign_out_are_accepted(self):
        self.assertEqual(contract_problems(account_design(), tree=ACCOUNT_TREE), [])

    def test_a_narrower_second_registration_is_rejected_with_the_api_to_reuse(self):
        duplicate = create_api("registerSimpleAccount", "/api/register-account",
            ["account.username", "account.password_hash"], ["username", "password"])
        problems = contract_problems(with_duplicate(duplicate), tree=ACCOUNT_TREE)
        self.assertEqual(fields(problems), [("API-register-account", "signature.method"),
            ("API-register-account", "request.body")])
        self.assertIn("second create for account", problems[0]["reason"])
        self.assertIn("API-register", problems[0]["fix"])
        self.assertIn("requirement_apis", problems[0]["fix"])
        self.assertIn("does not accept name, passport_number", problems[1]["reason"])
        self.assertIn("persists only", problems[1]["reason"])
        self.assertIn("account.passport_number", problems[1]["reason"])
        self.assertIn("API-register", problems[1]["fix"])

    def test_a_registration_that_makes_a_required_field_optional_is_rejected(self):
        duplicate = create_api("registerLooseAccount", "/api/register-loose",
            ["account.id", "account.username", "account.name", "account.passport_number",
             "account.password_hash"],
            ["username", "name", "passport_number"], optional=["password"])
        problems = contract_problems(with_duplicate(duplicate), tree=ACCOUNT_TREE)
        self.assertEqual(fields(problems), [("API-register-account", "signature.method"),
            ("API-register-account", "request.body")])
        self.assertIn("accepts password as optional", problems[1]["reason"])
        self.assertIn("may invent password", problems[1]["fix"])

    def test_an_identical_second_registration_is_reported_once(self):
        duplicate = create_api("registerAccountAgain", "/api/register-again",
            ["account.id", "account.username", "account.name", "account.passport_number",
             "account.password_hash"],
            ["username", "name", "passport_number", "password"])
        problems = contract_problems(with_duplicate(duplicate), tree=ACCOUNT_TREE)
        self.assertEqual(fields(problems), [("API-register-account", "signature.method")])

    def test_a_wider_registration_proposed_second_must_extend_the_accepted_create(self):
        # The narrow registration arrives first, so the requirement that needs the full
        # identity cannot reuse it as it stands: consolidating has to widen the accepted
        # API, otherwise the only route left is the narrow one and a client would fill the
        # identity fields in by itself.
        narrow = create_api("registerSimpleAccount", "/api/register-account",
            ["account.username", "account.password_hash"], ["username", "password"])
        value = account_design()
        value["apis"] = {"API-register-account": narrow, **value["apis"]}
        value["requirements"] = {"REQ-5": ["API-register-account"], **value["requirements"]}
        problems = contract_problems(value, tree=ACCOUNT_TREE)
        self.assertEqual(fields(problems), [("API-register", "signature.method"),
            ("API-register", "request.body")])
        self.assertIn("second create for account", problems[0]["reason"])
        self.assertIn("API-register-account", problems[0]["fix"])
        self.assertIn("requirement_apis", problems[0]["fix"])
        self.assertIn("persists only account.password_hash, account.username", problems[1]["reason"])
        self.assertIn("Extend 'API-register-account'", problems[1]["fix"])
        self.assertIn("its required body also carries name, passport_number", problems[1]["fix"])
        self.assertIn("persists account.id, account.name, account.passport_number", problems[1]["fix"])
        self.assertIn("may fabricate name, passport_number", problems[1]["fix"])

    def test_a_registration_that_persists_fewer_columns_is_rejected(self):
        duplicate = create_api("registerExtra", "/api/register-extra",
            ["account.username", "account.password_hash"],
            ["username", "name", "passport_number", "password"])
        problems = contract_problems(with_duplicate(duplicate), tree=ACCOUNT_TREE)
        self.assertEqual(fields(problems), [("API-register-account", "signature.method"),
            ("API-register-account", "request.body")])
        self.assertIn("persists only account.id, account.name, account.passport_number",
            problems[1]["reason"])

    def test_two_session_writes_without_a_shared_field_stay_distinct(self):
        # A refresh driven by a stored token shares no body field with sign-in, so it is a
        # second operation over the same entity instead of a narrower duplicate of it.
        value = account_design({"API-refresh": create_api("refreshSession", "/api/session/refresh",
            ["auth_session.token"], ["refresh_token"])})
        value["requirements"]["REQ-5"] = ["API-refresh"]
        self.assertEqual(contract_problems(value, tree=ACCOUNT_TREE), [])

    def test_each_entity_is_created_by_the_api_that_writes_most_of_it(self):
        self.assertEqual(creates_by_entity(account_design()),
            {"account": "API-register", "auth_session": "API-login"})
        self.assertEqual(creates_by_entity({"apis": {"API-list": api()}}), {})
        self.assertEqual(creates_by_entity({}), {})


class TraceabilityTests(unittest.TestCase):
    """The Requirement <-> API trace must show reuse instead of hiding it in one list."""

    def test_one_api_serving_two_requirements_is_reported_in_both_directions(self):
        trace = requirement_traceability(book_design())
        self.assertEqual(trace["requirements"]["REQ-A"], ["API-SAVE"])
        self.assertEqual(trace["requirements"]["ROOT"], [])
        self.assertEqual(trace["apis"]["API-SAVE"], ["REQ-A", "REQ-B"])
        self.assertEqual(trace["apis"]["API-COUNT"], ["REQ-C"])

    def test_the_summary_names_shared_apis_and_requirements_without_apis(self):
        text = format_traceability_summary(book_design())
        self.assertIn("TRACEABILITY 4 REQUIREMENT(S) -> 2 API(S)", text)
        self.assertIn("(no API)", text)
        self.assertIn("REUSED APIS (many-to-many edges)", text)
        self.assertIn("API-SAVE", text)

    def test_a_malformed_design_yields_an_empty_trace(self):
        self.assertEqual(requirement_traceability({"apis": [], "requirements": []}),
            {"requirements": {}, "apis": {}})
        self.assertEqual(requirement_traceability(None), {"requirements": {}, "apis": {}})


class StageIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name)
        self.arc, _, _ = prepare_artifacts(self.output)
        self.stage = import_module("07_check_backend_apis")

    def store(self, value):
        """Write the design artifact the way step 06 saves it."""
        save_json(self.arc / "backend_apis.json", value)

    def run_stage07(self):
        with patch.object(self.stage, "DemoConsole", return_value=Mock()):
            return self.stage.check_backend_apis(self.arc)

    def test_stage_07_verifies_a_valid_design_and_returns_it(self):
        value = book_design()
        self.store(value)
        self.assertEqual(self.run_stage07(), value)

    def test_stage_07_rejects_an_invalid_method_with_the_api_and_field(self):
        value = book_design()
        value["apis"]["API-COUNT"]["signature"]["method"] = "FETCH"
        self.store(value)
        with self.assertRaises(ApiContractError) as caught:
            self.run_stage07()
        self.assertEqual(caught.exception.feedback["problems"][0]["api_id"], "API-COUNT")
        self.assertEqual(caught.exception.feedback["problems"][0]["field"], "signature.method")

    def test_stage_07_rejects_a_path_parameter_mismatch(self):
        value = book_design()
        value["apis"]["API-COUNT"]["signature"]["path"] = "/api/notes/{noteId}/count"
        self.store(value)
        with self.assertRaises(ApiContractError) as caught:
            self.run_stage07()
        self.assertEqual(caught.exception.feedback["problems"][0]["field"], "request.params")

    def test_stage_07_still_rejects_missing_obligation_coverage(self):
        value = book_design()
        value["apis"]["API-SAVE"]["write"] = []
        self.store(value)
        with self.assertRaises(ObligationCoverageError):
            self.run_stage07()

    def test_stage_06_returns_contract_feedback_to_the_design_model(self):
        stage = import_module("06_design_backend_apis")
        value = book_design()
        self.store(value)
        requests = []

        def generate(instructions, payload):
            requests.append(deepcopy(payload))
            proposal = proposal_for(value, payload["requirement"]["id"])
            if "correction" not in payload and payload["requirement"]["id"] == "REQ-A":
                proposal["apis"]["API-SAVE"]["signature"]["method"] = "FETCH"
                proposal["apis"]["API-SAVE"]["signature"]["path"] = "notes"
            return proposal

        with patch.object(stage, "TextModel", return_value=Mock(generate_json=generate)), \
                patch.object(stage, "DemoConsole", return_value=Mock()):
            result = stage.design_backend_apis(self.arc)
        feedback = requests[1]["correction"]["details"]
        self.assertEqual([(item["api_id"], item["field"]) for item in feedback["problems"]],
            [("API-SAVE", "signature.method"), ("API-SAVE", "signature.path")])
        self.assertEqual(len(requests), 4)
        self.assertEqual(result["apis"]["API-SAVE"]["signature"],
            {"name": "saveNote", "method": "POST", "path": "/api/notes"})

    def test_obligation_coverage_is_still_reported_before_the_interface(self):
        tree, _, obligations = load_context(self.arc)
        value = book_design()
        broken = api(name="saveNote", method="FETCH", path="notes", write=[])
        with self.assertRaises(ObligationCoverageError):
            apply_decomposition(value, {"apis": {"API-SAVE": broken}, "requirement_apis": ["API-SAVE"]},
                "REQ-A", obligations, tree=tree)


if __name__ == "__main__":
    unittest.main()

