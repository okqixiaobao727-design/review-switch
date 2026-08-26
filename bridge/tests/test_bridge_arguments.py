#!/usr/bin/env python3
"""The Bridge's command line: what it accepts, rejects, and defaults to."""

import contextlib
import io
import pathlib
import tempfile
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


class CallerResponseParserTests(unittest.TestCase):
    """`--response`: what a resume must bring, and what no other call may.

    The rule lives on the command line because that is before preparation,
    before the owner lock, and before the round is granted: a caller that
    forgot the file loses the call and nothing else.
    """

    def setUp(self):
        self.bridge = load_bridge()
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = pathlib.Path(self.work.name)

    def response_file(self, contents='1. "the lock" — fixed in the Bridge\n'):
        """A Response where the Dispatcher writes one: outside any checkout."""
        path = self.root / "response.md"
        path.write_text(contents, encoding="utf-8")
        return str(path)

    def parse_failure(self, argv):
        """The message a rejected command line leaves on stderr."""
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                self.bridge.parse_args(argv)
        return stderr.getvalue()

    def resume_argv(self, *arguments):
        return [
            "--reviewer", "codex",
            "--base", "main",
            "--axis", "spec",
            "--resume-session", "session-ticket-50",
            *arguments,
        ]

    def test_a_resume_carries_the_response_it_was_given(self):
        path = self.response_file()

        args = self.bridge.parse_args(self.resume_argv("--response", path))

        self.assertEqual(args.response, path)

    def test_a_resume_without_a_response_is_refused_by_name(self):
        message = self.parse_failure(self.resume_argv())

        self.assertIn("--response", message)
        self.assertIn("--resume-session", message)

    def test_a_first_review_is_refused_a_response(self):
        message = self.parse_failure(
            [
                "--reviewer", "codex",
                "--base", "main",
                "--response", self.response_file(),
            ]
        )

        self.assertIn("--response", message)

    def test_a_recovery_is_refused_a_response(self):
        message = self.parse_failure(
            [
                "--reviewer", "codex",
                "--recover-session",
                "--response", self.response_file(),
            ]
        )

        self.assertIn("--response", message)

    def test_a_call_that_is_not_a_resume_carries_no_response(self):
        args = self.bridge.parse_args(["--reviewer", "codex", "--base", "main"])

        self.assertIsNone(args.response)

    def test_a_response_with_nothing_in_it_is_no_response(self):
        for contents in ("", "\n", "  \n\t\n"):
            with self.subTest(contents=contents):
                message = self.parse_failure(
                    self.resume_argv("--response", self.response_file(contents))
                )

                self.assertIn("--response", message)

    def test_a_response_file_that_is_not_there_is_refused(self):
        message = self.parse_failure(
            self.resume_argv("--response", str(self.root / "absent.md"))
        )

        self.assertIn("--response", message)

    def test_a_response_that_cannot_be_read_as_text_is_refused(self):
        """Unreadable is unreadable, whether the file or its bytes are at fault."""
        path = self.root / "binary.md"
        path.write_bytes(b"\xff\xfe\x00garbled")

        message = self.parse_failure(self.resume_argv("--response", str(path)))

        self.assertIn("--response", message)

    def test_a_health_probe_takes_no_resume_handle(self):
        """A probe answers no round, so it may not spend one.

        It is prepared for nothing and carries its own fixed text, so a probe
        allowed to resume would take the lineage's one re-review, hand the Lane
        the probe brief, and leave a receipt naming no Response at all.
        """
        for flag in ("--probe", "--browser-probe"):
            with self.subTest(flag=flag):
                message = self.parse_failure(
                    [
                        "--reviewer", "codex",
                        flag,
                        "--resume-session", "session-ticket-50",
                        "--response", self.response_file(),
                    ]
                )

                self.assertIn(flag, message)
                self.assertIn("--resume-session", message)

    def test_help_says_a_resume_requires_a_response(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            with self.assertRaises(SystemExit):
                self.bridge.parse_args(["--help"])

        described = " ".join(stdout.getvalue().split())

        self.assertIn("--response", described)
        self.assertIn("required with --resume-session", described)


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
