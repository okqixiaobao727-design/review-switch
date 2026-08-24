#!/usr/bin/env python3
"""The Bridge's command line: what it accepts, rejects, and defaults to."""

import contextlib
import io
import unittest

from bridge_harness import load_bridge


class ArgumentTests(unittest.TestCase):
    """What the Bridge's own command line accepts, rejects, and defaults to."""

    def setUp(self):
        self.bridge = load_bridge()

    def test_parser_accepts_the_review_preparation_inputs(self):
        args = self.bridge.parse_args(
            [
                "--reviewer",
                "codex",
                "--base",
                "main",
                "--spec",
                "docs/feature.md",
                "--axis",
                "standards",
            ]
        )

        self.assertEqual(args.base, "main")
        self.assertEqual(args.spec, "docs/feature.md")
        self.assertEqual(args.axis, "standards")

    def test_parser_defaults_to_both_axes(self):
        args = self.bridge.parse_args(
            ["--reviewer", "codex", "--base", "main", "--spec", "docs/feature.md"]
        )

        self.assertEqual(args.axis, "both")

    def test_parser_accepts_optional_model_and_effort_for_each_axis(self):
        args = self.bridge.parse_args(
            [
                "--reviewer",
                "codex",
                "--base",
                "main",
                "--spec",
                "docs/feature.md",
                "--standards-model",
                "standards-model",
                "--standards-effort",
                "standards-effort",
                "--spec-model",
                "spec-model",
                "--spec-effort",
                "spec-effort",
            ]
        )

        self.assertEqual(args.standards_model, "standards-model")
        self.assertEqual(args.standards_effort, "standards-effort")
        self.assertEqual(args.spec_model, "spec-model")
        self.assertEqual(args.spec_effort, "spec-effort")

    def test_per_axis_model_and_effort_default_to_none(self):
        args = self.bridge.parse_args(
            ["--reviewer", "codex", "--base", "main", "--spec", "docs/feature.md"]
        )

        self.assertIsNone(args.standards_model)
        self.assertIsNone(args.standards_effort)
        self.assertIsNone(args.spec_model)
        self.assertIsNone(args.spec_effort)

    def test_parser_rejects_the_old_free_text_target(self):
        with self.assertRaises(SystemExit):
            self.bridge.parse_args(
                [
                    "--reviewer", "codex",
                    "--base", "main",
                    "--spec", "docs/feature.md",
                    "review HEAD",
                ]
            )

    def test_parser_exposes_explicit_resume_handle(self):
        args = self.bridge.build_parser().parse_args(
            [
                "--reviewer",
                "codex",
                "--base",
                "main",
                "--spec",
                "docs/feature.md",
                "--axis",
                "spec",
                "--resume-session",
                "session-ticket-50",
            ]
        )
        self.assertEqual(args.resume_session, "session-ticket-50")

    def test_parser_carries_no_environment_specific_probe(self):
        with self.assertRaises(SystemExit):
            self.bridge.build_parser().parse_args(
                ["--reviewer", "codex", "--network-probe", "HEAD"]
            )

    def test_health_probes_need_no_review_preparation_inputs(self):
        for flag in ("--probe", "--browser-probe"):
            with self.subTest(flag=flag):
                args = self.bridge.parse_args(["--reviewer", "codex", flag])

                self.assertTrue(args.probe or args.browser_probe)
                self.assertIsNone(args.base)
                self.assertIsNone(args.spec)


class RecoveryParserTests(unittest.TestCase):
    def setUp(self):
        self.bridge = load_bridge()

    def test_recovery_needs_no_preparation_inputs(self):
        args = self.bridge.parse_args(["--reviewer", "codex", "--recover-session"])

        self.assertTrue(args.recover_session)
        self.assertIsNone(args.base)
        self.assertIsNone(args.spec)

    def test_a_review_still_requires_its_fixed_point(self):
        with self.assertRaises(SystemExit):
            self.bridge.parse_args(
                ["--reviewer", "codex", "--cwd", "/workspace/ticket-13"]
            )

    def test_recovery_and_resume_are_exclusive(self):
        with self.assertRaises(SystemExit):
            self.bridge.parse_args(
                [
                    "--reviewer", "codex",
                    "--recover-session",
                    "--resume-session", "session-13",
                ]
            )

    def test_recovery_takes_no_free_text_target(self):
        with self.assertRaises(SystemExit):
            self.bridge.parse_args(
                ["--reviewer", "codex", "--recover-session", "HEAD"]
            )


class ReviewerParserTests(unittest.TestCase):
    """The argument that names the Lane, checked before any Lane opens."""

    def setUp(self):
        self.bridge = load_bridge()

    def parse_failure(self, argv):
        """The message a rejected command line leaves on stderr."""
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                self.bridge.parse_args(argv)
        return stderr.getvalue()

    def test_the_named_reviewer_reaches_the_run(self):
        args = self.bridge.parse_args(["--reviewer", "codex", "--base", "main"])

        self.assertEqual(args.reviewer, "codex")

    def test_a_review_without_a_reviewer_is_refused_by_name(self):
        message = self.parse_failure(["--base", "main"])

        self.assertIn("--reviewer", message)

    def test_an_unknown_reviewer_is_refused_and_told_the_known_ones(self):
        message = self.parse_failure(
            ["--reviewer", "gemini", "--base", "main"]
        )

        self.assertIn("--reviewer", message)
        self.assertIn("gemini", message)
        self.assertIn("codex", message)

    def test_recovery_and_health_probes_name_a_reviewer_too(self):
        for argv in (["--recover-session"], ["--probe"], ["--browser-probe"]):
            with self.subTest(argv=argv):
                self.assertIn("--reviewer", self.parse_failure(argv))


class LifecycleHookParserTests(unittest.TestCase):
    """The arguments the protocol is handed in through, and the ones it retired."""

    def setUp(self):
        self.bridge = load_bridge()

    def test_the_bridge_takes_no_argument_naming_a_caller_log_or_ticket(self):
        for argument in ("--machine-log", "--ticket"):
            with self.subTest(argument=argument):
                with self.assertRaises(SystemExit):
                    self.bridge.parse_args(
                        [
                            "--reviewer", "codex",
                            "--base", "main",
                            argument, "anything",
                        ]
                    )

    def test_each_lifecycle_point_takes_one_command(self):
        args = self.bridge.parse_args([
            "--reviewer", "codex",
            "--base", "main",
            "--on-child-launch", "notify launch",
            "--on-review-start", "notify start",
            "--on-axis-end", "notify axis",
            "--on-review-end", "notify end",
        ])

        self.assertEqual(args.on_child_launch, "notify launch")
        self.assertEqual(args.on_review_start, "notify start")
        self.assertEqual(args.on_axis_end, "notify axis")
        self.assertEqual(args.on_review_end, "notify end")

    def test_a_point_left_out_carries_no_command(self):
        args = self.bridge.parse_args(["--reviewer", "codex", "--base", "main"])

        self.assertIsNone(args.on_child_launch)
        self.assertIsNone(args.on_review_start)
        self.assertIsNone(args.on_axis_end)
        self.assertIsNone(args.on_review_end)


class LaneArgumentTests(unittest.TestCase):
    """The command line the second Lane adds to."""

    def setUp(self):
        self.bridge = load_bridge()

    def test_the_reviewer_argument_accepts_both_lanes(self):
        for reviewer in ("codex", "claude"):
            args = self.bridge.parse_args(
                ["--reviewer", reviewer, "--base", "main"]
            )
            self.assertEqual(args.reviewer, reviewer)

    def test_an_account_is_optional_and_defaults_to_none(self):
        args = self.bridge.parse_args(["--reviewer", "claude", "--base", "main"])
        self.assertIsNone(args.account)

        args = self.bridge.parse_args(
            ["--reviewer", "claude", "--base", "main", "--account", "/profiles/a"]
        )
        self.assertEqual(args.account, "/profiles/a")


if __name__ == "__main__":
    unittest.main()
