#!/usr/bin/env python3
"""The `config` mode: the one writer of the Machine Config, and its proof.

Every write of a model or an effort is proved against the Lane it is written
for before it lands, so the file never names a model that Lane will not accept.
The proof is the Bridge's own health probe, driven here through the harness's
Lane stubs, so no vendor is contacted and no network is needed.
"""

import contextlib
import io
import os
import pathlib
import tempfile
import tomllib
import unittest
from unittest import mock

from bridge_harness import FakePaneTestCase


PROBE_REPLY = "TUI_REVIEW_BRIDGE_OK"
#: The probe carries no axis of its own, so it delivers whatever --axis says,
#: and the config mode leaves that option at its default.
PROBE_AXIS = "both"
#: A model the stubbed vendor answers for, in the vendor's own words.
REFUSED_MODEL = "a-model-this-vendor-refuses"
REFUSED_MODEL_DETAIL = f"unknown model: {REFUSED_MODEL}"


class ConfigModeTestCase(FakePaneTestCase):
    def setUp(self):
        super().setUp()
        # The proof is the Bridge's *existing* probe, so it is run on the
        # Bridge's own clocks rather than on a pair this mode invented. Those
        # clocks are a review's, and a suite that waited one out would take
        # hours over a Lane that refused in milliseconds.
        for name, seconds in (
            ("DEFAULT_TIMEOUT_SECONDS", 5),
            ("DEFAULT_STARTUP_TIMEOUT_SECONDS", 5),
        ):
            self.enter(mock.patch.object(self.bridge, name, seconds))

    def run_config(self, *argv):
        """The config mode as its caller reaches it: argv in, exit code and text out."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = self.bridge.run_config(list(argv))
            except SystemExit as refusal:
                # A command line the parser refuses, as a shell sees it.
                code = refusal.code
        return code, out.getvalue(), err.getvalue()

    def written(self):
        """The Machine Config as it now stands on disk, parsed."""
        return tomllib.loads(
            self.machine_config.read_text(encoding="utf-8")
        )

    def state(self, printed):
        """The printed state as a label-to-line mapping."""
        return {
            line.split(":", 1)[0]: line.split(": ", 1)[1]
            for line in printed.splitlines()
            if ":" in line
        }


class ProbedWriteTests(ConfigModeTestCase):
    def test_a_probe_that_succeeds_writes_the_value_and_prints_it(self):
        self.codex.finish(PROBE_REPLY)

        code, printed, errors = self.run_config("codex", "gpt-5.6-sol", "max")

        self.assertEqual(code, 0, errors)
        self.assertEqual(
            self.written(),
            {"lane": "codex", "codex": {"model": "gpt-5.6-sol", "effort": "max"}},
        )
        self.assertEqual(
            self.state(printed),
            {
                "Lane": "codex (config)",
                "Model": "gpt-5.6-sol (config)",
                "Effort": "max (config)",
            },
        )

    def test_the_probe_runs_on_the_target_lane_with_the_candidate_values(self):
        """The values proved are the ones about to be written, not the ones in place."""
        self.write_machine_config('lane = "claude"\n\n[codex]\nmodel = "old"\n')
        self.codex.finish(PROBE_REPLY)

        code, _printed, errors = self.run_config("codex", "gpt-5.6-sol", "max")

        self.assertEqual(code, 0, errors)
        session = self.codex.first_session
        self.assertEqual(session.model, "gpt-5.6-sol")
        self.assertEqual(session.effort, "max")

    def test_an_omitted_position_is_proved_at_the_value_it_keeps(self):
        """A candidate is the Lane's whole state, not only the position named."""
        self.write_machine_config('lane = "codex"\n\n[codex]\neffort = "max"\n')
        self.codex.finish(PROBE_REPLY)

        code, _printed, errors = self.run_config("codex", "gpt-5.6-sol")

        self.assertEqual(code, 0, errors)
        self.assertEqual(self.codex.first_session.effort, "max")
        self.assertEqual(
            self.written()["codex"], {"model": "gpt-5.6-sol", "effort": "max"}
        )

    def refuse_the_model(self, detail=REFUSED_MODEL_DETAIL):
        """Have the claude Lane's reviewer end the way a refused one really does.

        The vendor's words are in `result`; `subtype` is the generic word the
        CLI ends on. A fake that put the detail in `subtype` would let a config
        mode that printed only the Bridge's own reason pass.
        """
        self.claude.answer_with(
            {
                "session_id": "claude-probe",
                "result": detail,
                "is_error": True,
                "subtype": "error_during_execution",
                "permission_denials": [],
            },
            axis=PROBE_AXIS,
        )

    def test_a_probe_that_fails_writes_nothing_and_prints_the_vendors_error(self):
        self.write_machine_config('lane = "codex"\n')
        self.refuse_the_model()

        code, printed, errors = self.run_config("claude", REFUSED_MODEL)

        self.assertEqual(code, 1)
        self.assertIn(REFUSED_MODEL_DETAIL, errors)
        self.assertEqual(printed, "")
        self.assertEqual(
            self.machine_config.read_text(encoding="utf-8"), 'lane = "codex"\n'
        )

    def test_a_probe_that_fails_leaves_an_absent_file_absent(self):
        self.refuse_the_model()

        code, _printed, _errors = self.run_config("claude", REFUSED_MODEL)

        self.assertEqual(code, 1)
        self.assertFalse(self.machine_config.exists())

    def test_an_unknown_model_reaches_the_probe_rather_than_a_list(self):
        """Nothing here knows what models exist; the Lane answers that question."""
        self.codex.finish(PROBE_REPLY)

        code, _printed, errors = self.run_config(
            "codex", "a-model-nobody-here-has-heard-of"
        )

        self.assertEqual(code, 0, errors)
        self.assertEqual(
            self.codex.first_session.model, "a-model-nobody-here-has-heard-of"
        )

    def test_the_probe_fires_no_lifecycle_hook_of_this_machine(self):
        """Proving a value is not a review, so the machine's hooks stay silent."""
        marker = self.root / "hook-fired"
        self.write_machine_config(
            "[hooks]\n"
            f'review-start = "touch {marker}"\n'
        )
        self.codex.finish(PROBE_REPLY)

        code, _printed, errors = self.run_config("codex", "gpt-5.6-sol")

        self.assertEqual(code, 0, errors)
        self.assertFalse(marker.exists())


class UnprobedWriteTests(ConfigModeTestCase):
    def test_setting_only_the_lane_runs_no_probe(self):
        code, printed, errors = self.run_config("codex")

        self.assertEqual(code, 0, errors)
        self.assertEqual(self.codex.launched_panes, [])
        self.assertEqual(self.claude.launched, [])
        self.assertEqual(self.written(), {"lane": "codex"})
        self.assertEqual(self.state(printed)["Lane"], "codex (config)")

    def test_switching_the_lane_leaves_that_lanes_model_and_effort_alone(self):
        self.write_machine_config(
            'lane = "codex"\n\n[claude]\nmodel = "opus"\neffort = "high"\n'
        )

        code, printed, errors = self.run_config("claude")

        self.assertEqual(code, 0, errors)
        self.assertEqual(self.codex.launched_panes, [])
        self.assertEqual(
            self.written()["claude"], {"model": "opus", "effort": "high"}
        )
        self.assertEqual(
            self.state(printed),
            {
                "Lane": "claude (config)",
                "Model": "opus (config)",
                "Effort": "high (config)",
            },
        )

    def test_clearing_a_value_runs_no_probe(self):
        self.write_machine_config(
            'lane = "codex"\n\n[codex]\nmodel = "gpt-5.6-sol"\neffort = "max"\n'
        )

        code, printed, errors = self.run_config("codex", "-")

        self.assertEqual(code, 0, errors)
        self.assertEqual(self.codex.launched_panes, [])
        self.assertEqual(self.written()["codex"], {"effort": "max"})
        self.assertEqual(self.state(printed)["Model"], "none (vendor)")

    def test_clearing_both_values_leaves_the_lane_table_out_entirely(self):
        self.write_machine_config(
            'lane = "codex"\n\n[codex]\nmodel = "gpt-5.6-sol"\neffort = "max"\n'
        )

        code, printed, errors = self.run_config("codex", "-", "-")

        self.assertEqual(code, 0, errors)
        self.assertEqual(self.codex.launched_panes, [])
        self.assertEqual(self.written(), {"lane": "codex"})
        self.assertEqual(self.state(printed)["Effort"], "none (vendor)")

    def test_clearing_one_value_while_setting_the_other_still_probes(self):
        self.write_machine_config('lane = "codex"\n\n[codex]\nmodel = "old"\n')
        self.codex.finish(PROBE_REPLY)

        code, _printed, errors = self.run_config("codex", "-", "max")

        self.assertEqual(code, 0, errors)
        session = self.codex.first_session
        self.assertIsNone(session.model)
        self.assertEqual(session.effort, "max")
        self.assertEqual(self.written()["codex"], {"effort": "max"})


class WritePlacementTests(ConfigModeTestCase):
    def test_writing_into_an_absent_file_works(self):
        self.assertFalse(self.machine_config.exists())

        code, _printed, errors = self.run_config("codex")

        self.assertEqual(code, 0, errors)
        self.assertTrue(self.machine_config.exists())

    def test_writing_into_a_directory_that_does_not_exist_yet_works(self):
        with tempfile.TemporaryDirectory() as home:
            path = pathlib.Path(home) / "nested" / "review-switch" / "config.toml"
            with mock.patch.dict(
                os.environ, {"REVIEW_SWITCH_CONFIG": str(path)}
            ):
                code, _printed, errors = self.run_config("codex")

            self.assertEqual(code, 0, errors)
            self.assertEqual(
                tomllib.loads(path.read_text(encoding="utf-8")), {"lane": "codex"}
            )

    def test_a_write_preserves_every_key_it_was_not_asked_to_change(self):
        self.write_machine_config(
            'lane = "claude"\n'
            "\n"
            "[claude]\n"
            'model = "opus"\n'
            'effort = "high"\n'
            "\n"
            "[codex]\n"
            'effort = "low"\n'
            "\n"
            "[hooks]\n"
            'child-launch = "note-child"\n'
            'review-end = "printf \'%s\' \\"it\'s done\\""\n'
        )
        self.codex.finish(PROBE_REPLY)

        code, _printed, errors = self.run_config("codex", "gpt-5.6-sol")

        self.assertEqual(code, 0, errors)
        self.assertEqual(
            self.written(),
            {
                "lane": "codex",
                "claude": {"model": "opus", "effort": "high"},
                "codex": {"model": "gpt-5.6-sol", "effort": "low"},
                "hooks": {
                    "child-launch": "note-child",
                    "review-end": "printf '%s' \"it's done\"",
                },
            },
        )

    def test_a_written_file_is_read_back_by_the_bridges_own_reader(self):
        """What is written is proved parseable before it lands, not after."""
        self.write_machine_config(
            "[hooks]\n"
            'axis-end = "log \\\\ \\"quoted\\"\\n\\ttabbed"\n'
        )
        original = self.written()["hooks"]["axis-end"]

        code, _printed, errors = self.run_config("claude")

        self.assertEqual(code, 0, errors)
        config = self.bridge.read_machine_config(os.environ)
        self.assertEqual(config.hooks["axis-end"], original)

    def test_a_value_carrying_a_character_toml_leaves_bare_still_writes(self):
        """What the reader accepts, the writer has to be able to write back.

        TOML's unescaped set stops at `~` and resumes at U+0080, so a value
        carrying DELETE reads fine and, written back bare, would produce a file
        this Bridge's own reader refuses — and no Lane could then be switched.
        """
        awkward = "log \x7f done"
        self.write_machine_config(
            "[hooks]\n" f'axis-end = "log \\u007f done"\n'
        )
        self.assertEqual(self.written()["hooks"]["axis-end"], awkward)

        code, _printed, errors = self.run_config("codex")

        self.assertEqual(code, 0, errors)
        self.assertEqual(self.written()["hooks"]["axis-end"], awkward)

    def test_a_file_that_will_not_parse_stops_the_write(self):
        self.write_machine_config("lane = [\n")

        code, printed, errors = self.run_config("codex")

        self.assertEqual(code, 1)
        self.assertEqual(printed, "")
        self.assertIn("not valid TOML", errors)
        self.assertEqual(self.machine_config.read_text(encoding="utf-8"), "lane = [\n")


class PrintedStateTests(ConfigModeTestCase):
    def test_with_no_arguments_it_prints_the_resolved_lane_model_and_effort(self):
        self.write_machine_config(
            'lane = "codex"\n\n[claude]\nmodel = "opus"\n'
            '\n[codex]\nmodel = "gpt-5.6-sol"\neffort = "max"\n'
        )

        code, printed, errors = self.run_config()

        self.assertEqual(code, 0, errors)
        self.assertEqual(
            printed.splitlines(),
            [
                "Lane: codex (config)",
                "Model: gpt-5.6-sol (config)",
                "Effort: max (config)",
            ],
        )

    def test_printing_writes_nothing(self):
        code, _printed, errors = self.run_config()

        self.assertEqual(code, 0, errors)
        self.assertFalse(self.machine_config.exists())

    def test_a_machine_that_configured_nothing_credits_no_layer(self):
        """No Lane means no vendor was reached, so none may be named as the source."""
        code, printed, errors = self.run_config()

        self.assertEqual(code, 0, errors)
        self.assertEqual(
            printed.splitlines(),
            [
                "Lane: none (unset)",
                "Model: none (unset)",
                "Effort: none (unset)",
            ],
        )

    def test_a_value_the_file_leaves_out_falls_to_the_vendor(self):
        self.write_machine_config('lane = "codex"\n\n[codex]\nmodel = "m"\n')

        code, printed, errors = self.run_config()

        self.assertEqual(code, 0, errors)
        self.assertEqual(self.state(printed)["Effort"], "none (vendor)")


class GrammarTests(ConfigModeTestCase):
    def test_no_flag_bypasses_the_probe(self):
        """The mode takes positions and nothing else, so there is no flag to add."""
        parser = self.bridge.build_config_parser()
        self.assertEqual(
            set(parser._option_string_actions), {"-h", "--help"}
        )

    def test_no_position_is_held_to_a_list_of_known_values(self):
        """Only the Lane is a closed set; a model and an effort are the vendor's."""
        parser = self.bridge.build_config_parser()
        choices = {
            action.dest: action.choices for action in parser._actions
        }
        self.assertEqual(set(choices["lane"]), set(self.bridge.LANES))
        self.assertIsNone(choices["model"])
        self.assertIsNone(choices["effort"])

    def test_an_unknown_lane_word_is_refused_before_anything_is_written(self):
        code, printed, errors = self.run_config("cc")

        self.assertEqual(code, 2)
        self.assertEqual(printed, "")
        self.assertIn("cc", errors)
        self.assertFalse(self.machine_config.exists())

    def test_a_fourth_position_is_refused(self):
        code, _printed, _errors = self.run_config("codex", "m", "e", "extra")

        self.assertEqual(code, 2)
        self.assertFalse(self.machine_config.exists())

    def test_the_mode_is_reached_by_the_first_token_like_the_private_ones(self):
        with mock.patch.object(self.bridge.sys, "argv", ["review-bridge", "config"]):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                code = self.bridge.main()

        self.assertEqual(code, 0)
        self.assertIn("Lane:", out.getvalue())


if __name__ == "__main__":
    unittest.main()
