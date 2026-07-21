import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import execution_service


class ParseResultTests(unittest.TestCase):
    def test_parses_json_success_and_account_data(self):
        payload = '{"status":"success","message":"done","accounts":[]}'

        status, summary, error, data = execution_service._parse_result_with_data(
            payload, "", 0
        )

        self.assertEqual(status, "success")
        self.assertEqual(summary, "done")
        self.assertIsNone(error)
        self.assertEqual(data["accounts"], [])

    def test_nonzero_process_exit_is_failure(self):
        status, _, error, _ = execution_service._parse_result_with_data(
            "", "boom", 1
        )

        self.assertEqual(status, "failed")
        self.assertEqual(error, "boom")


class ExecutionSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_path_outside_scripts_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(execution_service, "BASE_DIR", Path(temp_dir)),
                patch.object(
                    execution_service,
                    "get_script",
                    return_value={"path": "../outside.py"},
                ),
            ):
                with self.assertRaisesRegex(ValueError, "scripts directory"):
                    await execution_service._execute_script_once(1)

    async def test_kills_script_after_timeout(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            scripts_dir = Path(temp_dir) / "scripts"
            scripts_dir.mkdir()
            script_path = scripts_dir / "slow.py"
            script_path.write_text(
                textwrap.dedent(
                    """
                    import time
                    time.sleep(5)
                    print('{"status":"success"}')
                    """
                ),
                encoding="utf-8",
            )

            with (
                patch.object(execution_service, "BASE_DIR", Path(temp_dir)),
                patch.object(
                    execution_service,
                    "get_script",
                    return_value={"path": "scripts/slow.py"},
                ),
                patch.object(execution_service, "add_execution", return_value=7),
                patch.object(execution_service, "update_execution") as update_execution,
            ):
                result = await execution_service._execute_script_once(
                    1, timeout_seconds=0.05
                )

        self.assertEqual(result["status"], "failed")
        self.assertIn("timed out", result["error"])
        update_execution.assert_called_once()


if __name__ == "__main__":
    unittest.main()
