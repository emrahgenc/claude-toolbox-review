"""End-to-end tests for the Toolbox Review scripts against a synthetic home folder.

Run: python3 -m unittest discover -s tests -v
"""
import glob
import importlib.util
import json
import os
import shutil
import tempfile
import unittest

import fakehome
from fakehome import run


def load_config_module():
    spec = importlib.util.spec_from_file_location("tr_config_test", os.path.join(fakehome.SCRIPTS, "config.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="toolbox-review-test-")
        cls.env, cls.paths = fakehome.build(cls.tmp)
        out = run("collect_episodes.py", ["--since", "7d"], cls.env).stdout
        cls.collect_out = out
        runs = sorted(glob.glob(os.path.join(cls.paths["data"], "runs", "*")))
        assert runs, out
        cls.run_dir = runs[-1]
        cls.run_id = os.path.basename(cls.run_dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def json(self, name):
        with open(os.path.join(self.run_dir, name), encoding="utf-8") as fh:
            return json.load(fh)


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="toolbox-review-cfg-")
        self.env, self.paths = fakehome.build(self.tmp)
        self.saved = {k: os.environ.get(k) for k in ("HOME", "TOOLBOX_REVIEW_DATA")}
        os.environ.update(HOME=self.paths["home"], TOOLBOX_REVIEW_DATA=self.paths["data"])
        os.makedirs(self.paths["data"], exist_ok=True)

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_user_config_overrides_defaults_key_by_key(self):
        fakehome.write(os.path.join(self.paths["data"], "config.json"), json.dumps(
            {"_note": "ignored", "language": "tr", "forge": {"type": "github"}}))
        cfg = load_config_module().config()
        self.assertEqual(cfg["language"], "tr")
        self.assertEqual(cfg["forge"], {"type": "github", "host": ""})   # nested keys merge
        self.assertIn("prod_markers", cfg)                               # untouched defaults stay
        self.assertNotEqual(cfg.get("_note"), "ignored")                 # "_" keys are comments, never settings

    def test_budget_override(self):
        fakehome.write(os.path.join(self.paths["data"], "budgets.json"), json.dumps({"retention_days": 30}))
        b = load_config_module().budgets()
        self.assertEqual(b["retention_days"], 30)
        self.assertEqual(b["skill_md_lines"]["max"], 500)

    def test_charter_falls_back_to_template(self):
        mod = load_config_module()
        self.assertTrue(mod.charter_path().endswith(os.path.join("templates", "CHARTER.md")))
        fakehome.write(os.path.join(self.paths["data"], "CHARTER.md"), "# mine\n")
        self.assertEqual(mod.charter_path(), os.path.join(self.paths["data"], "CHARTER.md"))

    def test_discovers_personal_and_skills_folder_plugin_components(self):
        mod = load_config_module()
        skills = mod.personal_skills()
        self.assertIn("demo-skill", skills)
        self.assertIn("kit:helper", skills)
        agents = mod.personal_agents()
        self.assertIn("solo", agents)
        self.assertIn("kit:checker", agents)

    def test_exclude(self):
        fakehome.write(os.path.join(self.paths["data"], "config.json"), json.dumps(
            {"maintained": {"exclude": ["kit:helper"]}}))
        self.assertNotIn("kit:helper", load_config_module().personal_skills())

    def test_safe_name(self):
        self.assertEqual(load_config_module().safe_name("kit:helper"), "kit__helper")


class CollectTest(Base):
    def test_one_episode_with_correction_push_and_approval(self):
        s = self.json("summary.json")
        row = s["skills"]["demo-skill"]
        self.assertEqual(row["episodes"], 1)
        self.assertEqual(row["signal_classes"].get("correction"), 1)
        self.assertEqual(row["signal_classes"].get("approval"), 1)   # seen through the leading <system-reminder>
        self.assertEqual(row["actions"].get("git_push"), 1)
        self.assertEqual(row["actions"].get("writes_without_approval"), 1)
        self.assertGreater(row["first_turn_cost_median"], 0)

    def test_episode_file_and_stable_id(self):
        eps = self.json(os.path.join("episodes", "demo-skill.json"))
        self.assertEqual(len(eps), 1)
        self.assertRegex(eps[0]["id"], r"^\d{6}-\d{4}-0a1b2c3d$")
        self.assertEqual(eps[0]["args"], "quarterly")

    def test_feedback_memory_is_linked(self):
        fb = self.json("summary.json")["feedback_memories"]
        self.assertTrue(any("short-replies" in json.dumps(f) for f in fb), fb)

    def test_mark_reviewed(self):
        out = run("collect_episodes.py", ["--mark-reviewed", self.run_id], self.env).stdout
        self.assertIn("reviewed_until", out)


class SensorsTest(Base):
    @classmethod
    def setUpClass(cls):
        super(SensorsTest, cls).setUpClass()
        cls.outputs = {}
        for script, args in (("inventory.py", []), ("health.py", []), ("security.py", []),
                             ("forge_sensor.py", []), ("git_sensor.py", []), ("scorecard.py", None)):
            a = [cls.run_dir] if args is None else ["--run-dir", cls.run_dir] + args
            cls.outputs[script] = run(script, a, cls.env).stdout

    def test_inventory(self):
        inv = self.json("inventory.json")
        names = {s["name"] for s in inv["personal_skills"]}
        self.assertTrue({"demo-skill", "kit:helper"} <= names, names)
        self.assertIn("approx_tokens", inv["always_loaded"])

    def test_health_writes_counts(self):
        self.assertIn("counts", self.json("health.json"))

    def test_security_finds_secret_without_printing_it(self):
        sec = self.json("security.json")
        dump = json.dumps(sec) + self.outputs["security.py"]
        self.assertNotIn(fakehome.fake_secret(), dump)
        self.assertNotIn(fakehome.fake_secret()[8:20], dump)
        hits = [c for c in sec["checks"] if c["area"] == "secrets" and c["status"] == "FAIL"]
        self.assertTrue(any("anthropic key" in c["reason"] for c in hits), self.outputs["security.py"])
        reasons = " ".join(c["reason"] for c in sec["checks"] if c["status"] != "OK")
        self.assertIn("any shell command", reasons)

    def test_forge_none(self):
        self.assertIn("none configured", self.outputs["forge_sensor.py"])
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "forge.json")))

    def test_git_ai_share(self):
        g = self.json("git.json")
        self.assertEqual(g["total"]["commits"], 2, self.outputs["git_sensor.py"])
        self.assertEqual(g["total"]["ai_commits"], 1)

    def test_scorecard(self):
        rows = {r["key"]: r for r in self.json("scorecard.json")["rows"]}
        self.assertIn("always_loaded_tokens", rows)
        self.assertEqual(rows["writes_without_approval"]["value"], 1)
        self.assertEqual(rows["security_fail"]["status"], "OVER")
        self.assertIn("| Measure |", self.outputs["scorecard.py"])


class LedgerTest(Base):
    def test_life_cycle_menu_and_target(self):
        recs = [
            {"component": "demo-skill", "title": "Ask before pushing", "kind": "correctness", "impact": "high",
             "effort": "S", "door": "reversible", "complexity": "+", "plain_title": "Ask before every push",
             "plain_effect": "Nothing leaves your machine without a yes.", "plain_cost": "~10 min; easy to undo.",
             "plain_group": "security", "recommended": True},
            {"component": "kit:helper", "title": "Shorten description", "kind": "tokens", "impact": "düşük",
             "effort": "XS", "door": "reversible", "complexity": "-", "plain_title": "Make a description shorter",
             "plain_effect": "Every session loads a little less.", "plain_cost": "~2 min.", "plain_group": "hafiflet"},
        ]
        f = os.path.join(self.tmp, "recs.json")
        fakehome.write(f, json.dumps(recs))
        ids = run("ledger.py", ["add", "--run", self.run_id, "--file", f], self.env).stdout.split()
        self.assertEqual(len(ids), 2)

        menu = run("ledger.py", ["menu"], self.env).stdout
        self.assertLess(menu.index("Ask before every push"), menu.index("Make a description shorter"))

        run("ledger.py", ["set", ids[0], "approved"], self.env)
        run("ledger.py", ["set", ids[0], "applied", "--commit", "none"], self.env)
        t = run("ledger.py", ["target", ids[0], "skill:demo-skill:writes_without_approval", "--goal", "0",
                              "--baseline-run", self.run_id], self.env).stdout
        self.assertIn("baseline", t)
        check = run("ledger.py", ["check", self.run_id], self.env).stdout
        self.assertIn("not enough runs", check)

        run("ledger.py", ["set", ids[1], "rejected", "--reason", "not worth it"], self.env)
        shown = json.loads(run("ledger.py", ["show", "--component", "kit:helper", "--json"], self.env).stdout)
        self.assertEqual(shown[0]["status"], "rejected")

    def test_meta(self):
        run("ledger.py", ["meta", "last_scout_version", "2.9.0"], self.env)
        self.assertEqual(run("ledger.py", ["meta", "last_scout_version"], self.env).stdout.strip(), "2.9.0")


class MetricsExamsRetentionTest(Base):
    def test_metric(self):
        out = run("metrics.py", [self.run_dir, "skill:demo-skill:pushes_per_episode"], self.env).stdout.split()
        self.assertEqual(out, ["1.0", "1"])

    def test_exams_empty(self):
        run("exams.py", ["list"], self.env)

    def test_retention_dry_run_deletes_nothing(self):
        run("retention.py", [], self.env)
        self.assertTrue(os.path.isdir(os.path.join(self.run_dir, "episodes")))


if __name__ == "__main__":
    unittest.main()
