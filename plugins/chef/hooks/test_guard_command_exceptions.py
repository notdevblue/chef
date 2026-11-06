import unittest

import mission_guard


class CommandExceptionTests(unittest.TestCase):
    def adapter(self, exception=".*"):
        return {"guard": {"deny": [{"pattern": r"\bdeliver\b", "reason": "approval required", "exceptCommand": [exception]}]}}

    def test_literal_queries_and_output(self):
        commands = [
            'grep -n "publish-tool deliver" policy.md',
            "rg -n -F 'publish-tool deliver' notes.txt",
            'echo "publish-tool deliver 17"',
            "Write-Output 'publish-tool deliver 17'",
            r'rg -n "publish-tool deliver" C:\notes\policy.md',
        ]
        for tool in ("Bash", "PowerShell"):
            for command in commands:
                with self.subTest(tool=tool, command=command):
                    self.assertFalse(mission_guard.judge_command(self.adapter(), command, tool).denied)

    def test_dynamic_and_embedded_execution_stays_denied(self):
        commands = [
            "publish-tool deliver 17",
            "$CLI deliver 17",
            "CLI=publish-tool; $CLI deliver 17",
            "$CLI = 'publish-tool'; & $CLI deliver 17",
            'echo "$(publish-tool deliver 17)"',
            'echo `publish-tool deliver 17`',
            "echo publish-tool deliver 17 | sh",
            "echo publish-tool deliver 17 | Invoke-Expression",
            "echo publish-tool deliver 17 && sh",
            "echo publish-tool deliver 17; sh",
            "echo publish-tool deliver 17\nsh",
            "echo publish-tool deliver 17 > run.sh",
            "rg --pre publish-tool deliver notes.txt",
            "rg --pre=publish-tool deliver notes.txt",
            r"rg deliver \--pre publish-tool",
            r"rg deliver \-\-pre publish-tool",
            'python -c "import os; os.system(\\\"publish-tool deliver 17\\\")"',
            "sh -c 'publish-tool deliver 17'",
            "& echo publish-tool deliver 17",
            r'echo %PROGRAM% deliver 17',
            "echo publish-tool deliver 17 <(sh)",
            "echo (publish-tool deliver 17)",
            "echo 'publish-tool deliver 17'|sh",
        ]
        for tool in ("Bash", "PowerShell"):
            for command in commands:
                with self.subTest(tool=tool, command=command):
                    self.assertTrue(mission_guard.judge_command(self.adapter(), command, tool).denied)

    def test_full_match_is_required(self):
        self.assertTrue(mission_guard.judge_command(self.adapter("deliver"), 'echo "publish-tool deliver"').denied)

    def test_rule_exception_does_not_disable_other_rules(self):
        adapter = self.adapter()
        adapter["guard"]["deny"].append({"pattern": "publish-tool", "reason": "separate policy"})
        self.assertEqual("separate policy", mission_guard.judge_command(adapter, 'echo "publish-tool deliver"').reason)

    def test_existing_fragment_exceptions_remain_compatible(self):
        adapter = self.adapter()
        rule = adapter["guard"]["deny"][0]
        rule.pop("exceptCommand")
        rule["except"] = [r'echo "publish-tool deliver"']
        self.assertFalse(mission_guard.judge_command(adapter, 'echo "publish-tool deliver"').denied)

    def test_invalid_command_exception_is_not_applied(self):
        for value in (".*", ["["], [1]):
            with self.subTest(value=value):
                adapter = self.adapter()
                adapter["guard"]["deny"][0]["exceptCommand"] = value
                problems = []
                self.assertTrue(mission_guard.judge_command(adapter, 'echo "publish-tool deliver"', problems=problems).denied)
                self.assertTrue(any("exceptCommand" in problem for problem in problems))


if __name__ == "__main__":
    unittest.main()
