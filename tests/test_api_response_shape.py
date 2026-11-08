"""The API proposal reader: path-level diagnostics, bounded repair, preserved records.

Every test here is deterministic. No model is called: ``propose_json`` receives a mock
model, and the accepted/rejected answers are literal JSON objects, including the shape
the real BookStack run returned when stage 06 stopped at REQ-5.6.
"""

from copy import deepcopy
import json
import unittest
from unittest.mock import Mock

from infrastructure.shared.agent.proposals import ResponseShapeError, propose_json
from infrastructure.stages.backend.primitives import (ObligationCoverageError, apply_decomposition,
    read_api_proposal)


def object_schema(properties=None):
    """Build the inline object schema the API IR uses for one request or response part."""
    return {"type": "object", "properties": properties or {}}


def note_api(name="listNotes", method="GET", path="/api/notes", read=("Note.text",), write=()):
    """Build one complete API definition the way step 06 asks the model to return it."""
    return {
        "description": "Perform the business operation",
        "read": list(read),
        "write": list(write),
        "signature": {"name": name, "method": method, "path": path},
        "request": {"params": object_schema(), "query": object_schema(),
            "body": object_schema(), "headers": object_schema()},
        "response": {"success": {"status": 200, "body": object_schema(), "headers": object_schema()},
            "errors": []},
        "precondition": [],
        "postcondition": [{"id": "Q-1", "description": "Return the requested records", "status": 200}],
    }


#: Read obligations of the two requirements the fixtures map, and an empty third one.
OBLIGATIONS = {"requirements": {
    "REQ-A": {"read": ["Note.text"], "write": []},
    "REQ-B": {"read": ["Note.text"], "write": []},
    "REQ-EMPTY": {"read": [], "write": []},
}}


def design():
    """Return an accepted design with nothing designed yet, as step 06 starts."""
    return {"apis": {}, "requirements": {}}


class ProposalShapeTests(unittest.TestCase):
    def test_complete_proposal_is_accepted_and_the_input_design_is_untouched(self):
        current = design()
        response = {"apis": {"API-LIST": note_api()}, "requirement_apis": ["API-LIST"]}
        result = apply_decomposition(current, response, "REQ-A", OBLIGATIONS)
        self.assertEqual(result["requirements"]["REQ-A"], ["API-LIST"])
        self.assertEqual(list(result["apis"]), ["API-LIST"])
        self.assertEqual(current, design())

    def test_missing_requirement_apis_names_the_top_level_path_and_the_read_section(self):
        response = {"apis": {"API-LIST": note_api()}}
        with self.assertRaises(ResponseShapeError) as failure:
            read_api_proposal(response, "REQ-A")
        error = failure.exception
        self.assertEqual(error.problem["path"], "requirement_apis")
        self.assertEqual(error.feedback["missing_field"], "requirement_apis")
        self.assertEqual(error.feedback["recognized"], {"apis": ["API-LIST"]})
        self.assertEqual(error.feedback["must_preserve"], ["apis.API-LIST"])
        self.assertIn("requirement_apis", str(error))
        self.assertIn("API-LIST", str(error))
        self.assertFalse(error.recoverable)
        self.assertEqual(sorted(error.feedback["example"]), ["apis", "requirement_apis"])
        self.assertIn("next to apis", error.feedback["fix"])

    def test_requirement_apis_nested_inside_apis_is_read_and_the_bogus_key_removed(self):
        # This is the shape the real REQ-5.6 answer returned after nine repairs.
        response = {"apis": {"API-LIST": note_api(), "requirement_apis": ["API-LIST"]}}
        proposal, notes = read_api_proposal(response, "REQ-A")
        self.assertEqual(proposal, {"apis": {"API-LIST": note_api()}, "requirement_apis": ["API-LIST"]})
        self.assertEqual(len(notes), 1)
        self.assertIn("apis.requirement_apis", notes[0])
        self.assertEqual(sorted(response["apis"]), ["API-LIST", "requirement_apis"])
        result = apply_decomposition(design(), response, "REQ-A", OBLIGATIONS)
        self.assertEqual(sorted(result["apis"]), ["API-LIST"])
        self.assertEqual(result["requirements"]["REQ-A"], ["API-LIST"])

    def test_a_requirement_keyed_section_nested_inside_apis_is_read_as_well(self):
        response = {"apis": {"API-LIST": note_api(),
            "requirement_apis": {"REQ-A": ["API-LIST"]}}}
        proposal, notes = read_api_proposal(response, "REQ-A")
        self.assertEqual(proposal["requirement_apis"], ["API-LIST"])
        self.assertEqual(sorted(proposal["apis"]), ["API-LIST"])
        self.assertTrue(any("apis.requirement_apis" in note for note in notes))
        # A section name is not a definition, so it can never register as an API ID.
        self.assertNotIn("requirement_apis", proposal["apis"])

    def test_one_named_envelope_is_unwrapped(self):
        response = {"response": {"apis": {"API-LIST": note_api()}, "requirement_apis": ["API-LIST"]}}
        proposal, notes = read_api_proposal(response, "REQ-A")
        self.assertEqual(proposal["requirement_apis"], ["API-LIST"])
        self.assertEqual(notes, [])
        updated = apply_decomposition(design(), response, "REQ-A", OBLIGATIONS)
        self.assertEqual(updated["requirements"]["REQ-A"], ["API-LIST"])

    def test_absent_apis_is_repaired_and_never_defaulted(self):
        with self.assertRaises(ResponseShapeError) as failure:
            read_api_proposal({"requirement_apis": []}, "REQ-A")
        error = failure.exception
        self.assertEqual(error.problem["path"], "apis")
        self.assertEqual(error.problem["found"], "missing")
        self.assertEqual(error.feedback["recognized"], {"top_level_keys": ["requirement_apis"]})

    def test_a_response_that_is_not_an_object_is_unusable_at_the_root(self):
        for response in ([], "apis", None, 7):
            with self.subTest(response=response):
                with self.assertRaises(ResponseShapeError) as failure:
                    read_api_proposal(response, "REQ-A")
                self.assertEqual(failure.exception.problem["path"], "$")
                self.assertIn("Return the complete object", failure.exception.problem["fix"])

    def test_empty_sections_are_legal_only_when_the_gates_accept_them(self):
        empty = {"apis": {}, "requirement_apis": []}
        result = apply_decomposition(design(), empty, "REQ-EMPTY", OBLIGATIONS)
        self.assertEqual(result["requirements"]["REQ-EMPTY"], [])
        # The same empty answer for a requirement with obligations stays a real failure,
        # so an empty collection is never turned into a pass by the reader.
        with self.assertRaises(ObligationCoverageError):
            apply_decomposition(design(), empty, "REQ-A", OBLIGATIONS)

    def test_a_section_of_the_wrong_type_is_reported_at_its_path(self):
        for value in ("API-LIST", 3, {"KEYS": ["API-LIST"]}):
            with self.subTest(value=value):
                with self.assertRaises(ResponseShapeError) as failure:
                    read_api_proposal({"apis": {}, "requirement_apis": value}, "REQ-A")
                self.assertTrue(failure.exception.problem["path"].startswith("requirement_apis"))

    def test_an_association_to_an_undefined_api_is_reported_by_index(self):
        with self.assertRaises(ResponseShapeError) as failure:
            read_api_proposal({"apis": {"API-LIST": note_api()},
                "requirement_apis": ["API-LIST", "API-MISSING"]}, "REQ-A")
        self.assertEqual(failure.exception.problem["path"], "requirement_apis[1]")
        self.assertIn("API-MISSING", failure.exception.problem["found"])
        self.assertEqual(failure.exception.feedback["must_preserve"], ["API-LIST"])

    def test_an_association_keyed_by_requirement_is_read_for_this_requirement(self):
        response = {"apis": {"API-LIST": note_api()}, "requirement_apis": {"REQ-A": ["API-LIST"]}}
        proposal, notes = read_api_proposal(response, "REQ-A")
        self.assertEqual(proposal["requirement_apis"], ["API-LIST"])
        self.assertIn("requirement_apis.REQ-A", notes[0])
        with self.assertRaises(ResponseShapeError) as failure:
            read_api_proposal(response, "REQ-B")
        self.assertEqual(failure.exception.problem["path"], "requirement_apis.REQ-B")

    def test_a_repeated_association_is_merged_once(self):
        proposal, notes = read_api_proposal(
            {"apis": {"API-LIST": note_api()}, "requirement_apis": ["API-LIST", "API-LIST"]}, "REQ-A")
        self.assertEqual(proposal["requirement_apis"], ["API-LIST"])
        self.assertEqual(notes, ["repeated API IDs in requirement_apis have been merged"])

    def test_a_slot_under_apis_that_is_not_a_definition_is_reported(self):
        with self.assertRaises(ResponseShapeError) as failure:
            read_api_proposal({"apis": {"API-LIST": ["not", "a", "definition"]},
                "requirement_apis": ["API-LIST"]}, "REQ-A")
        self.assertEqual(failure.exception.problem["path"], "apis.API-LIST")
        self.assertEqual(failure.exception.problem["found"], "list")


def accept_for(current, rid, obligations=OBLIGATIONS):
    """Wrap the real reader and the real gates the way step 06 does."""
    def accept(response):
        proposal, _ = read_api_proposal(response, rid, known=current["apis"])
        return apply_decomposition(current, proposal, rid, obligations)
    return accept


class RepairLoopTests(unittest.TestCase):
    def test_a_shape_failure_is_repaired_with_its_path_example_and_recognition(self):
        replies = [{"apis": {"API-LIST": note_api()}},
            {"apis": {"API-LIST": note_api()}, "requirement_apis": ["API-LIST"]}]
        requests = []

        def generate(instructions, request):
            requests.append(deepcopy(request))
            return replies[len(requests) - 1]

        result, accepted = propose_json(Mock(generate_json=generate), "Design", {},
            accept_for(design(), "REQ-A"), Mock(), "Backend API design REQ-A", 4)
        self.assertEqual(result["requirements"]["REQ-A"], ["API-LIST"])
        self.assertEqual(accepted, replies[-1])
        self.assertEqual(len(requests), 2)
        details = requests[1]["correction"]["details"]
        self.assertEqual(details["expected_path"], "requirement_apis")
        self.assertEqual(details["recognized"], {"apis": ["API-LIST"]})
        self.assertEqual(details["must_preserve"], ["apis.API-LIST"])
        self.assertEqual(sorted(details["example"]), ["apis", "requirement_apis"])
        self.assertEqual(requests[1]["correction"]["error_type"], "ResponseShapeError")

    def test_repeated_repairs_fail_closed_with_the_path_in_the_message(self):
        current = design()
        model = Mock(generate_json=Mock(return_value={"apis": {"API-LIST": note_api()}}))
        with self.assertRaises(RuntimeError) as failure:
            propose_json(model, "Design", {}, accept_for(current, "REQ-A"), Mock(),
                "Backend API design REQ-A", 3)
        message = str(failure.exception)
        self.assertIn("Backend API design REQ-A exhausted 3 proposals", message)
        self.assertIn("requirement_apis", message)
        self.assertIn("Last diagnosis", message)
        self.assertIn('"expected_path"', message)
        self.assertIsInstance(failure.exception.__cause__, ResponseShapeError)
        self.assertEqual(failure.exception.feedback["details"]["expected_path"], "requirement_apis")
        self.assertEqual(model.generate_json.call_count, 3)
        self.assertEqual(current, design())

    def test_incomplete_json_enters_repair_and_then_completes(self):
        replies = [json.JSONDecodeError("Expecting value", '{"apis": {', 0),
            {"apis": {"API-LIST": note_api()}, "requirement_apis": ["API-LIST"]}]
        requests = []

        def generate(instructions, request):
            requests.append(deepcopy(request))
            value = replies[len(requests) - 1]
            if isinstance(value, Exception):
                raise value
            return value

        result, _ = propose_json(Mock(generate_json=generate), "Design", {},
            accept_for(design(), "REQ-A"), Mock(), "Backend API design REQ-A", 3)
        self.assertEqual(result["requirements"]["REQ-A"], ["API-LIST"])
        self.assertEqual(requests[1]["correction"]["invalid_response"], '{"apis": {')
        self.assertEqual(requests[1]["correction"]["error_type"], "JSONDecodeError")

    def test_earlier_rejections_are_repeated_within_a_fixed_budget(self):
        requests = []

        def generate(instructions, request):
            requests.append((instructions, deepcopy(request)))
            return {}

        with self.assertRaises(RuntimeError):
            propose_json(Mock(generate_json=generate), "Design", {}, lambda value: value["apis"],
                Mock(), "Backend API design REQ-A", 9)
        histories = [request.get("correction", {}).get("previous_attempts", []) for _, request in requests]
        self.assertEqual([len(history) for history in histories], [0, 0, 1, 2, 3, 4, 5, 5, 5])
        self.assertEqual(histories[-1][0], {"attempt": 3, "error_type": "KeyError",
            "error": "Missing key 'apis' while reading the model response. Return the requested complete records."})
        for instructions, _ in requests[1:]:
            self.assertIn("correction.previous_attempts", instructions)


class PreservedRecordsTests(unittest.TestCase):
    def test_accepted_records_survive_a_later_rejected_proposal(self):
        accepted = apply_decomposition(design(),
            {"apis": {"API-LIST": note_api()}, "requirement_apis": ["API-LIST"]}, "REQ-A", OBLIGATIONS)
        snapshot = deepcopy(accepted)
        proposal = {"apis": {"API-OWN": note_api(name="ownNotes", path="/api/own")}}
        with self.assertRaises(RuntimeError) as failure:
            propose_json(Mock(generate_json=Mock(return_value=proposal)),
                "Design", {}, accept_for(accepted, "REQ-B"), Mock(), "Backend API design REQ-B", 2)
        self.assertIsInstance(failure.exception.__cause__, ResponseShapeError)
        self.assertEqual(accepted, snapshot)
        self.assertEqual(accepted["requirements"]["REQ-A"], ["API-LIST"])
        self.assertNotIn("API-OWN", accepted["apis"])

    def test_a_shared_api_is_reused_without_a_second_definition(self):
        current = apply_decomposition(design(),
            {"apis": {"API-LIST": note_api()}, "requirement_apis": ["API-LIST"]}, "REQ-A", OBLIGATIONS)
        result = apply_decomposition(current, {"apis": {}, "requirement_apis": ["API-LIST"]},
            "REQ-B", OBLIGATIONS)
        self.assertEqual(result["requirements"]["REQ-A"], ["API-LIST"])
        self.assertEqual(result["requirements"]["REQ-B"], ["API-LIST"])
        self.assertEqual(sorted(result["apis"]), ["API-LIST"])

    def test_failed_repairs_of_one_requirement_leave_earlier_mappings_intact(self):
        requests = []

        def generate(instructions, request):
            requests.append(deepcopy(request))
            rid = request["requirement"]
            if rid == "REQ-B" and "correction" not in request:
                return {"apis": {"API-OWN": note_api(name="ownNotes", path="/api/own")}}
            return {"apis": {"API-LIST": note_api()}, "requirement_apis": ["API-LIST"]}

        current = design()
        for rid in ("REQ-A", "REQ-B"):
            current, _ = propose_json(Mock(generate_json=generate), "Design", {"requirement": rid},
                accept_for(current, rid), Mock(), f"Backend API design {rid}", 3)
        self.assertEqual(current["requirements"]["REQ-A"], ["API-LIST"])
        self.assertEqual(current["requirements"]["REQ-B"], ["API-LIST"])
        self.assertEqual(sorted(current["apis"]), ["API-LIST"])
        self.assertEqual(len(requests), 3)
