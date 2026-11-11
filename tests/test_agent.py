import io
import json
import os
import subprocess
import sys
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from src.agent import run
from src.models import LiveModel, ModelError


FIXED = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
REQUEST = {"topic": "绿色出行", "budget": 12, "deadline": "2026-12-31"}


def action(name, **kwargs):
    args = {"reason": "测试动作", **kwargs}
    return {"role": "assistant", "content": None, "tool_calls": [{
        "id": "call-1", "type": "function", "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
    }]}


class ScriptedModel:
    def __init__(self, actions):
        self.actions = iter(actions)
        self.messages = []

    def complete(self, messages, state):
        self.messages.append(json.loads(json.dumps(messages, ensure_ascii=False)))
        return next(self.actions)


class AgentTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path(__file__).resolve().parent.parent / ".runtime" / "tests"
        temp_root.mkdir(parents=True, exist_ok=True)
        name = uuid.uuid4().hex
        self.db_path = temp_root / (name + ".db")
        self.input_path = temp_root / (name + ".json")
        self.config_path = temp_root / (name + ".env")
        self.addCleanup(self.db_path.unlink, missing_ok=True)
        self.addCleanup(self.input_path.unlink, missing_ok=True)
        self.addCleanup(self.config_path.unlink, missing_ok=True)
        config_patch = mock.patch("src.models.DOTENV_PATH", self.config_path)
        config_patch.start()
        self.addCleanup(config_patch.stop)
        self.db = str(self.db_path)

    def run_agent(self, request=None, **kwargs):
        return run(request if request is not None else REQUEST, self.db, "mock", now=lambda: FIXED, **kwargs)

    def test_normal_and_cost_accounting(self):
        result = self.run_agent()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["plan"]["material"]["id"], "green-premium")
        self.assertEqual(result["plan"]["estimated_cost"], "12.00")
        self.assertEqual(result["plan"]["cost_breakdown"], {"spent": "2.00", "material_license": "6.00", "editing": "4.00"})
        self.assertEqual(result["plan"]["remaining_work"][0]["status"], "pending")
        self.assertFalse(result["model_api_cost_included"])

    def test_missing_fields_do_not_call_model_or_create_database(self):
        for field in ("budget", "deadline"):
            with self.subTest(field=field):
                request = {k: v for k, v in REQUEST.items() if k != field}
                result = self.run_agent(request)
                self.assertEqual(result["status"], "needs_input")
                self.assertEqual(result["missing_fields"], [field])
                self.assertEqual((result["model_calls_total"], result["tool_calls_total"]), (0, 0))
                self.assertFalse(Path(self.db).exists())

    def test_validation_rejects_invalid_budget_and_naive_datetime(self):
        for budget in (-1, True, "NaN", "1.234"):
            self.assertEqual(self.run_agent({**REQUEST, "budget": budget})["status"], "invalid_input")
        result = self.run_agent({**REQUEST, "deadline": "2026-12-01T12:00:00"})
        self.assertEqual(result["status"], "invalid_input")
        self.assertFalse(Path(self.db).exists())

    def test_over_budget_observes_12_then_selects_8(self):
        result = self.run_agent({**REQUEST, "budget": 10})
        estimates = result["steps"]["estimate_cost"]["attempts"]
        self.assertEqual([x["result"]["estimate"]["total"] for x in estimates], ["12.00", "8.00"])
        self.assertEqual(result["plan"]["material"]["id"], "green-basic")
        self.assertEqual(result["plan"]["estimated_cost"], "8.00")
        self.assertEqual(result["spent"], "2.00")
        actions = [x for x in result["trace"] if x["event"] == "model_action"]
        self.assertEqual([x["tool"] for x in actions], ["search_materials", "generate_script", "estimate_cost", "estimate_cost", "save_plan"])
        self.assertEqual(actions[3]["arguments"]["material_id"], "green-basic")

    def test_no_feasible_candidate_reports_gap(self):
        result = self.run_agent({**REQUEST, "budget": 5})
        self.assertEqual(result["status"], "incomplete")
        self.assertIsNone(result["plan"])
        self.assertEqual(result["incomplete_details"]["lowest_estimated_total"], "8.00")
        self.assertEqual(result["incomplete_details"]["budget_gap"], "3.00")
        self.assertEqual(result["incomplete_details"]["evaluated_materials"], 2)

    def test_budget_gap_is_available_even_if_script_cannot_start(self):
        result = self.run_agent({**REQUEST, "budget": 0})
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["steps"]["generate_script"]["status"], "failed")
        self.assertEqual(result["incomplete_details"]["lowest_estimated_total"], "8.00")
        self.assertEqual(result["incomplete_details"]["budget_gap"], "8.00")
        self.assertIn("理论最低值", result["incomplete_details"]["scope"])

    def test_tool_failure_kept_and_backup_used_without_losing_search(self):
        result = self.run_agent(fail_once="generate_script")
        attempts = result["steps"]["generate_script"]["attempts"]
        self.assertEqual([x["status"] for x in attempts], ["failed", "success"])
        self.assertEqual(attempts[0]["result"]["code"], "injected_failure")
        self.assertEqual(result["script"]["variant"], "backup")
        self.assertIn("备用开场", result["script"]["content"])
        self.assertEqual(len(result["steps"]["search_materials"]["attempts"]), 1)
        self.assertEqual(result["spent"], "2.00")
        self.assertEqual(result["status"], "completed")

    def test_partial_material_result_is_preserved(self):
        result = self.run_agent(partial_once="search_materials")
        attempts = result["steps"]["search_materials"]["attempts"]
        self.assertEqual([x["status"] for x in attempts], ["partial", "success"])
        self.assertEqual(len(attempts[0]["result"]["materials"]), 1)
        self.assertEqual(result["status"], "completed")

    def test_deadline_blocks_saving_even_with_sufficient_budget(self):
        result = self.run_agent({**REQUEST, "deadline": "2026-01-01T00:30:00+00:00"})
        self.assertEqual(result["status"], "incomplete")
        self.assertIsNone(result["plan"])
        self.assertFalse(result["latest_quote"]["fits_deadline"])
        self.assertGreater(result["incomplete_details"]["deadline_gap_minutes"], 0)

    def test_expired_deadline_and_unknown_material(self):
        expired = self.run_agent({**REQUEST, "deadline": "2025-12-31T23:59:59+00:00"})
        self.assertEqual(expired["status"], "incomplete")
        self.assertEqual(expired["model_calls_total"], 0)
        absent = self.run_agent({**REQUEST, "topic": "海洋天文"})
        self.assertEqual(absent["status"], "incomplete")
        self.assertIsNone(absent["plan"])
        self.assertEqual(absent["steps"]["generate_script"]["status"], "pending")

    def test_repeat_returns_saved_plan_without_calling_model_again(self):
        first = self.run_agent()
        second = self.run_agent()
        self.assertTrue(second["reused"])
        self.assertEqual(second["task_id"], first["task_id"])
        self.assertEqual(second["plan"], first["plan"])
        self.assertEqual(second["tool_calls_total"], first["tool_calls_total"])
        self.assertEqual(second["trace"], first["trace"])

    def test_repeat_warns_if_remaining_work_no_longer_fits_deadline(self):
        request = {**REQUEST, "deadline": "2026-01-01T03:00:00+00:00"}
        first = self.run_agent(request)
        later = datetime(2026, 1, 1, 2, 30, tzinfo=timezone.utc)
        repeated = run(request, self.db, "mock", now=lambda: later)
        self.assertEqual(first["status"], "completed")
        self.assertTrue(repeated["reused"])
        self.assertFalse(repeated["deadline_still_feasible"])
        self.assertIn("无法在截止前完成", repeated["warning"])

    def test_resume_after_failure_reuses_successful_steps(self):
        first = self.run_agent(fail_once="generate_script", max_calls=2)
        self.assertEqual(first["status"], "incomplete")
        self.assertEqual(first["steps"]["generate_script"]["status"], "failed")
        again_without_resume = self.run_agent(fail_once="generate_script")
        self.assertTrue(again_without_resume["resume_available"])
        self.assertEqual(again_without_resume["tool_calls_total"], 2)
        resumed = self.run_agent(resume=True, fail_once="generate_script")
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(len(resumed["steps"]["search_materials"]["attempts"]), 1)
        self.assertEqual([a["status"] for a in resumed["steps"]["generate_script"]["attempts"]], ["failed", "success"])

    def test_call_cap_stops_infinite_model_loop(self):
        class Repeating:
            def complete(self, messages, state):
                return action("search_materials")
        result = self.run_agent(model_override=Repeating(), max_calls=3)
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("上限", result["reason"])
        self.assertEqual((result["model_calls_total"], result["tool_calls_total"]), (3, 3))
        self.assertEqual(result["steps"]["search_materials"]["attempts"][-1]["status"], "reused")

    def test_guard_rejects_save_for_unestimated_material(self):
        model = ScriptedModel([
            action("search_materials"), action("generate_script", variant="standard"),
            action("estimate_cost", material_id="green-premium"), action("save_plan", material_id="green-basic"), action("finish"),
        ])
        result = self.run_agent(model_override=model)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["steps"]["save_plan"]["status"], "failed")
        self.assertEqual(result["steps"]["save_plan"]["attempts"][0]["result"]["code"], "missing_current_quote")
        self.assertIsNone(result["plan"])

    def test_guard_rejects_saving_over_budget_plan(self):
        model = ScriptedModel([
            action("search_materials"), action("generate_script", variant="standard"),
            action("estimate_cost", material_id="green-premium"), action("save_plan", material_id="green-premium"), action("finish"),
        ])
        result = self.run_agent({**REQUEST, "budget": 10}, model_override=model)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["steps"]["save_plan"]["attempts"][0]["result"]["code"], "budget_exceeded")
        self.assertEqual(result["steps"]["save_plan"]["attempts"][0]["result"]["budget_gap"], "2.00")
        self.assertIsNone(result["plan"])

    def test_invalid_action_is_observed_and_never_executed(self):
        bad = action("generate_script", variant="unrecognized")
        model = ScriptedModel([bad, action("finish")])
        result = self.run_agent(model_override=model)
        self.assertEqual(result["steps"]["generate_script"]["status"], "failed")
        self.assertEqual(result["steps"]["generate_script"]["attempts"][0]["result"]["code"], "invalid_action")
        self.assertEqual(result["spent"], "0.00")
        self.assertEqual(json.loads(model.messages[1][-1]["content"])["code"], "invalid_action")

    def test_live_missing_configuration_is_explicit_not_mock(self):
        with mock.patch.dict(os.environ, {"MODEL_BASE_URL": "", "MODEL_API_KEY": "", "MODEL_NAME": ""}):
            result = run(REQUEST, self.db, "live", now=lambda: FIXED)
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("MODEL_API_KEY", result["reason"])
        self.assertEqual(result["model_calls_total"], 0)
        self.assertIsNone(result["plan"])

    def test_live_mode_never_replays_a_completed_mock_plan(self):
        offline = self.run_agent()
        with mock.patch.dict(os.environ, {"MODEL_BASE_URL": "", "MODEL_API_KEY": "", "MODEL_NAME": ""}):
            live = run(REQUEST, self.db, "live", now=lambda: FIXED)
        self.assertEqual(offline["status"], "completed")
        self.assertEqual(live["status"], "incomplete")
        self.assertNotEqual(live["task_id"], offline["task_id"])
        self.assertIsNone(live["plan"])

    def test_live_rejects_unencrypted_remote_endpoint(self):
        env = {"MODEL_BASE_URL": "http://example.invalid/v1", "MODEL_API_KEY": "dummy-key", "MODEL_NAME": "test-model"}
        with mock.patch.dict(os.environ, env):
            result = run(REQUEST, self.db, "live", now=lambda: FIXED)
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("HTTPS", result["reason"])
        self.assertEqual(result["model_calls_total"], 0)

    def test_dotenv_configuration_and_environment_override(self):
        self.config_path.write_text('API_KEY="dummy-key"\nMODEL_ID="deepseek-flash "\nBASE_URL="https://api.deepseek.com"\n', encoding="utf-8")
        with mock.patch.dict(os.environ, {}, clear=True):
            model = LiveModel()
            self.assertEqual(model.model, "deepseek-flash")
            self.assertEqual(model.url, "https://api.deepseek.com/chat/completions")
            with mock.patch.dict(os.environ, {"MODEL_NAME": "override-model"}):
                self.assertEqual(LiveModel().model, "override-model")

    def test_dotenv_placeholder_and_malformed_file_fail_without_secret_leak(self):
        self.config_path.write_text('API_KEY="sk-..."\nMODEL_ID="deepseek-flash"\nBASE_URL="https://api.deepseek.com"\n', encoding="utf-8")
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ModelError) as error:
                LiveModel()
            self.assertIn("API_KEY", str(error.exception))
            self.config_path.write_text('API_KEY="private-unclosed\n', encoding="utf-8")
            with self.assertRaises(ModelError) as error:
                LiveModel()
            self.assertNotIn("private-unclosed", str(error.exception))

    def test_parallel_actions_are_rejected_and_returned_to_model(self):
        first = action("search_materials")
        first["tool_calls"].append(action("generate_script", variant="standard")["tool_calls"][0])
        first["tool_calls"][1]["id"] = "call-2"
        model = ScriptedModel([first, action("finish")])
        result = self.run_agent(model_override=model)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["steps"]["search_materials"]["status"], "pending")
        self.assertEqual(result["steps"]["generate_script"]["status"], "pending")
        observations = [json.loads(x["content"]) for x in model.messages[1][-2:]]
        self.assertEqual([x["code"] for x in observations], ["multiple_actions", "multiple_actions"])
        actions = [event for event in result["trace"] if event["event"] == "model_action"]
        self.assertEqual([event["tool"] for event in actions[:2]], ["search_materials", "generate_script"])
        self.assertEqual(actions[1]["arguments"]["variant"], "standard")

    def test_live_protocol_returns_observation_and_keeps_reasoning_out_of_trace(self):
        responses = [
            action("search_materials"), action("generate_script", variant="standard"),
            action("estimate_cost", material_id="green-premium"), action("estimate_cost", material_id="green-basic"),
            action("save_plan", material_id="green-basic"),
        ]
        sent = []

        def urlopen_fake(request, timeout):
            body = json.loads(request.data.decode("utf-8"))
            sent.append(body)
            if len(sent) > 1:
                self.assertEqual(body["messages"][-1]["role"], "tool")
                self.assertEqual(body["messages"][-2]["reasoning_content"], "private-marker")
            if len(sent) == 4:
                observation = json.loads(body["messages"][-1]["content"])
                self.assertEqual(observation["estimate"]["total"], "12.00")
                self.assertFalse(observation["estimate"]["fits_budget"])
            message = responses[len(sent) - 1]
            message["reasoning_content"] = "private-marker"
            return io.BytesIO(json.dumps({"model": "reported-test-model", "choices": [{"message": message}]}).encode("utf-8"))

        env = {"MODEL_BASE_URL": "https://example.invalid/v1", "MODEL_API_KEY": "dummy-key", "MODEL_NAME": "test-model"}
        fake_opener = mock.Mock()
        fake_opener.open.side_effect = urlopen_fake
        with mock.patch.dict(os.environ, env), mock.patch("src.models.build_opener", return_value=fake_opener):
            result = run({**REQUEST, "budget": 10}, self.db, "live", now=lambda: FIXED)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["plan"]["estimated_cost"], "8.00")
        self.assertEqual(len(sent), 5)
        self.assertTrue(any(event.get("reported_model") == "reported-test-model" for event in result["trace"]))
        self.assertNotIn("private-marker", json.dumps(result, ensure_ascii=False))
        self.assertNotIn("dummy-key", json.dumps(result, ensure_ascii=False))

    def test_cli_outputs_utf8_json(self):
        self.input_path.write_text(json.dumps(REQUEST, ensure_ascii=False), encoding="utf-8")
        proc = subprocess.run([sys.executable, "-m", "src.main", "--input", str(self.input_path), "--mode", "mock", "--state", self.db], capture_output=True, check=True)
        output = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(output["request"]["topic"], "绿色出行")
        self.assertEqual(output["status"], "completed")


if __name__ == "__main__":
    unittest.main()
